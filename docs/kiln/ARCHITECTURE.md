# KILN: arquitetura

## Visao geral

```
 usuario/WhatsApp/painel
        |  POST /kiln/runs, /kiln/approvals/{id}/decide
        v
  agent-api (FastAPI)  --XADD-->  Redis  kiln:events  (grupo kiln_workers)
        |                           |                \--> kiln:approvals (saida: notificacoes)
        |                           v
        |                   kiln-worker (orquestrador, level-triggered)
        |   Postgres <------ kiln_runs / tasks / contracts / events / approvals / usage / lessons
        |                           |
        |            pool de jobs (KILN_MAX_PARALLEL=2)
        |    plan | start | agent-loop | verify(+merge) | integrate
        |                           |
        |                    docker-proxy (so containers)
        |                           v
        |      sandbox descartavel: sem rede, sem caps, uid 10001, volume kiln_workspaces
        |      /work/<run>/repo  (main)   /work/<run>/wt/<agente> (branch kiln/<agente>)
        v
  Prometheus (:9108) / Loki (logs JSON) / Grafana
```

Principios: (1) o agente nunca roda no host; (2) o LLM propoe, a policy decide, o sandbox executa;
(3) "pronto" = gates deterministicos; (4) so o orquestrador faz merge; (5) tudo e auditado (append-only);
(6) fail closed: erro interno de gate/verificador reprova.

## Mapa de arquivos

| Arquivo | Papel |
|---|---|
| `app/kiln/settings.py` | configuracao por env (um `.env`) |
| `app/kiln/models.py` | status, `AgentSpec`, `Plan`, `Action` |
| `app/kiln/scanners.py` | caminhos, diff unificado, segredos, skips, supressoes (puro) |
| `app/kiln/policy.py` | allow/deny/approval de comandos e escrita (estende `base_check` do projeto) |
| `app/kiln/contracts.py` | validacao, hash e integridade dos contratos; `Stack` |
| `app/kiln/planner.py` | meta -> plano + contratos + `todo.md` (com reparo ate 2x) |
| `app/kiln/actions.py` | parser estrito do JSON de acoes do LLM |
| `app/kiln/agents.py` | loop do agente, pausas, auditoria, budget |
| `app/kiln/sandbox.py` / `workspace.py` | Docker descartavel; git worktrees e protecoes de caminho |
| `app/kiln/gates.py` | os 12 gates e `evaluate_gates` |
| `app/kiln/verifier.py` | clone limpo, install/build/test/smoke, revisao LLM consultiva |
| `app/kiln/budget.py` | tokens, USD, tempo, iteracoes, comandos, deteccao de loop |
| `app/kiln/orchestrator.py` | maquina de estados e pool |
| `app/kiln/store.py` / `events.py` | Postgres e Redis Streams |
| `app/kiln/observability.py` | metricas Prometheus e logs JSON |
| `app/routers/kiln.py` | API `/kiln/*` |
| `worker/kiln_worker.py` | processo do worker |
| `kiln-sandbox/` | imagem do sandbox e `kiln_smoke.py` |
| `db/kiln.sql` | schema idempotente |

## Maquinas de estado

Run: `created -> planning -> awaiting_plan_approval -> starting -> running -> integrating -> succeeded`
com saidas `failed`, `cancelled`, `budget_exceeded`.

Tarefa: `pending -> running -> agent_done -> verifying -> merged`
- `running -> awaiting_approval -> resume -> running` (config/comando que exige humano)
- `running -> blocked_contract -> resume` (outro agente propôs mudar contrato)
- `verifying -> pending` (gates reprovaram; contexto reinicia com o feedback; ate `KILN_MAX_TASK_ATTEMPTS`)
- `* -> failed` (budget, loop, merge, tentativas esgotadas)

Toda transicao e `UPDATE ... WHERE status = esperado`: nenhum job roda duas vezes e eventos duplicados sao inofensivos.
`advance()` olha o estado, nao a mensagem: perder um evento nao trava (tick a cada `KILN_TICK_SECONDS`).

## Fluxo de uma run

1. `POST /kiln/runs {goal}` -> evento `run.created`.
2. **Planner** gera agentes (pastas disjuntas), criterios verificaveis e contratos (`stack.json`, `routes.json`...).
   Validacao deterministica (sobreposicao, ciclos, comandos permitidos). Um humano **aprova o plano** (kind `plan`).
3. Orquestrador cria o repo (`main` com contratos, `tasks/todo.md`, `KILN_RULES.md`) e as tarefas.
4. Cada agente sem dependencia pendente roda em paralelo (limite do pool) na sua worktree.
5. O agente emite acoes; a policy decide; o sandbox executa; tudo vai para `kiln_events`.
6. `done` -> commit -> **verificador** em clone limpo: install (egress), build, test, smoke; gates.
7. Reprovou: feedback objetivo e nova tentativa. Passou: merge `--no-ff` em `main` sob lock; baseline de testes sobe.
8. Todas mescladas -> **integracao** (build/test de todos + smoke dos servicos juntos) -> `succeeded`.

## Contratos e mudancas

`contracts/` e congelado (hash em `kiln_contracts`). Um agente chama `propose_contract_change`
(validado antes de criar a aprovacao). Enquanto houver mudanca pendente ou aprovada e nao aplicada, os
outros agentes pausam (`blocked_contract`). Ao aprovar: nova versao, `main` atualizado, e cada branch
recebe `merge main` ao retomar (`sync_from_main`). O diff e sempre `main...HEAD` (tres pontos): contratos novos
nao viram "alteracao do agente".

## Policy (resumo)

| Alvo | Regra |
|---|---|
| Executavel | allowlist; shells/`sudo`/`env`/`curl`/`ssh` negados; so argv |
| git | so `status diff log show add commit rev-parse ls-files blame shortlog branch restore`; sem `-C/-c`, `--force`, `--hard`, `-n` |
| npm | `--force` negado; `ci/install` com egress e checagem de pacotes proibidos; scripts fora da lista -> aprovacao |
| SQL/Redis | `DROP`, `TRUNCATE`, `DELETE` sem `WHERE`, `FLUSH*` negados em qualquer argv |
| rm | recursivo so em artefatos de build; teste nunca; fora da pasta do agente negado |
| Escrita | so `owned_paths`; `contracts/`, `.git`, `.env*`, `.github` negados; config existente -> aprovacao; segredo literal negado |
| Leitura | `.env*` e `.git` negados |

## Gates ("definicao de pronto")

`install`, `build`, `tests` (rc=0, 0 falhas, >0 testes, nunca menos que o baseline; pulados nao contam),
`test_integrity` (sem teste removido/pulado/focado, sem remocao liquida), `suppressions` (0 novas),
`config_integrity`, `contracts_frozen`, `stack_lock` (pacotes e padroes proibidos, ex.: troca de banco),
`secrets`, `destructive`, `ownership`, `smoke` (servico sobe e responde aos checks).

## Modelo de falhas

| Falha | Defesa |
|---|---|
| Agente apaga testes / suite vazia | policy `test.delete`/`rm.recursive` + gates `tests`, `test_integrity` |
| Troca de banco / `DROP DATABASE` | `sql.destructive`, `stack.forbidden_package`, gate `stack_lock` |
| Renomear tipo sem avisar | contratos somente leitura + proposta aprovada + `blocked_contract` |
| `git push --force` | git e do orquestrador; merge conflitante e abortado, nunca forcado |
| Afrouxar lint/tsconfig | aprovacao humana + gates `config_integrity`/`suppressions` |
| Segredo no `.env`/codigo | `secret.env_file`/`secret.detected` + gate `secrets` |
| "Pronto" com servico fora do ar | gate `smoke` + integracao |
| Loop / queima de tokens | `LoopDetector`, limites de iteracoes/comandos/tokens/USD/tempo |
| Worker morre | reaper de tarefas/runs, reentrega de eventos, dead-letter |
| Verificador quebra | fail closed + retry com feedback |
| Prompt injection no repo | agente so age via acoes validadas; sandbox sem rede e sem segredos |

## Orcamento

Por run: `KILN_BUDGET_TOKENS`, `KILN_BUDGET_USD` (com `KILN_PRICE_*`), `KILN_BUDGET_SECONDS`;
por tarefa: iteracoes e comandos. Sobrescreva por run em `POST /kiln/runs {"budget": {...}}`.
Estouro de tokens/USD/tempo encerra a run em `budget_exceeded`; loop/iteracoes/comandos falham so a tarefa.
