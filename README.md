# alpha

# AGENT — API + Worker + Postgres + Redis + KILN

## Subir
```bash
cd agent
./setup.sh            # cria .env com senhas aleatórias e sobe (use sudo se o docker pedir)
# ou manual:
cp .env.example .env  # edite as 3 variáveis obrigatórias
docker compose up -d --build
```

## Testar
```bash
source .env
curl -s localhost:${API_PORT:-8000}/health
curl -s -H "X-API-Key: $API_KEY" -X POST localhost:${API_PORT:-8000}/llm/ping
curl -s -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"session_id":"t1","message":"quem é você?"}' localhost:${API_PORT:-8000}/chat
docker compose logs -f agent-worker
```

## Acessar o banco (comando correto)
```bash
docker compose exec postgres psql -U assist -d assistdb
```

## Alertmanager (webhook)
```yaml
receivers:
  - name: agent
    webhook_configs:
      - url: http://IP_DO_AGENT:8000/alerts
        send_resolved: true
        http_config:
          authorization: { type: Bearer, credentials: "<API_KEY do .env>" }
```

## Ollama no host
O container alcança o host por `host.docker.internal`. O Ollama precisa escutar fora de 127.0.0.1:
```bash
sudo systemctl edit ollama     # adicione:  [Service]  Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
ollama list                    # o modelo de OLLAMA_MODEL precisa existir (ollama pull ...)
```

## Reset total do banco (só se quiser apagar tudo)
`docker compose down -v`

## Executor do host (pasta `executor/`)
Ações que mexem no servidor (reiniciar serviço, UFW, logs, disco) NÃO rodam no container.
Fluxo: `agent-worker` -> Redis `host_exec_stream` -> `host_runner.py` (host, usuário `assistbot`)
-> `sudo assist-safe-run` -> resultado em `host_result_stream` -> worker grava na auditoria/aprovação.

```bash
sudo ./executor/install.sh          # cria assistbot, wrapper, sudoers, runner e serviço systemd
sudoedit /etc/assist/allowed-services   # serviços que o agente pode consultar/reiniciar
# no .env:  EXECUTOR_MODE=live   e depois:
docker compose up -d
```

Ações do catálogo: `check_service_status`, `service_logs`, `disk_usage` (sem aprovação);
`restart_service`, `ufw_allow` (exigem aprovação em `/admin/approvals/decide`).

Testes:
```bash
sudo -u assistbot sudo -n /usr/local/bin/assist-safe-run disk            # deve funcionar
sudo -u assistbot sudo -n /usr/bin/systemctl restart ssh                 # deve NEGAR (sudoers só libera o wrapper)
sudo -u assistbot sudo -n /usr/local/bin/assist-safe-run restart ssh     # deve NEGAR (fora da lista)
journalctl -t assist-safe-run -n 10      # auditoria no host
journalctl -u assist-runner -f           # log do runner
```
Em `EXECUTOR_MODE=dry-run` (padrão) nada chega ao host: o worker só registra "[dry-run]".
# KILN: Agent Control Plane

Fabrica de software com agentes supervisionados: o **Planner** quebra a meta em pastas, os **agentes**
trabalham em paralelo em sandboxes descartaveis, o **verificador** independente aplica **gates objetivos**
e so o **orquestrador** faz merge. Tudo e auditado, orcado e observavel. E o KILN do video, do jeito certo.

> Estado: nucleo (policy, gates, scanners, contratos, planner, parser de acoes, loop do agente, workspace git,
> smoke) coberto por **59 testes que passam**, inclusive git real. As integracoes com **Docker, Postgres, Redis e FastAPI**
> estao escritas mas **so podem ser validadas no seu servidor**: siga o checklist da Fase 0 abaixo.

## O que vem no pacote (caminhos relativos a ~/Documentos/agent)

```
agent/agent/app/kiln/        settings models scanners policy contracts planner actions llm agents
                             sandbox workspace gates verifier budget orchestrator store events observability
agent/agent/app/routers/     __init__.py  kiln.py            -> /kiln/runs, /kiln/approvals, /kiln/lessons
agent/agent/worker/          __init__.py  kiln_worker.py     -> python -m worker.kiln_worker
kiln-sandbox/                Dockerfile  kiln_smoke.py
db/kiln.sql                  tabelas, trigger append-only, views para Grafana
docker-compose.kiln.yml      override (kiln-worker, docker-proxy); nao altera o seu compose
.env.kiln.example            variaveis (acrescente ao .env)
requirements-kiln.txt        dependencias novas (acrescente ao agent/requirements.txt)
docs/kiln/                   README, ARCHITECTURE, KILN_RULES, RUNBOOK
tests/kiln/                  59 testes + suite de aceitacao (replay das 7 falhas do video)
```

Seu `app/` ja tem `auth.py`, `bus.py`, `db.py`, `config.py`, `main.py`, `pipeline.py`. O KILN **nao importa nem altera nenhum deles**:
usa `DB_DSN` e `REDIS_URL` do mesmo `.env`. Unicas edicoes nos seus arquivos:

1. `agent/agent/app/main.py` (2 linhas):
   ```python
   from app.routers import kiln as kiln_router
   app.include_router(kiln_router.router)
   ```
2. `agent/agent/requirements.txt`: acrescentar o conteudo de `requirements-kiln.txt`.
3. `.env`: acrescentar `.env.kiln.example`.

Assumptions do compose: servicos `postgres`, `redis` e `agent-api`, rede `assist`. Se os nomes forem outros, ajuste
`docker-compose.kiln.yml` (3 pontos) e `REDIS_URL`. Python 3.10+.

**Integrar sua policy existente:** `build_policy(owned, forbidden, base_check=fn)` aceita uma funcao
`list[str] -> Decision | None` (sua `security/policy.py`, se existir). Ela so pode **endurecer** (um DENY dela prevalece).

## Instalacao e Fase 0 (checklist de validacao)

Siga `docs/kiln/RUNBOOK.md` secao 1. Depois valide, nesta ordem:

1. `python3 tests/kiln/run_tests.py` (ou `pytest tests/kiln`): 59 verdes na sua maquina.
2. `docker compose logs kiln-worker`: schema aplicado, sandbox pronto, "kiln-worker ... pronto".
3. `curl localhost:8000/kiln/health` responde `ok`.
4. Sandbox isolado: `docker run --rm --network none kiln-sandbox:latest node -v` funciona e
   `docker run --rm --network none kiln-sandbox:latest python3 -c "import urllib.request as u;u.urlopen('http://1.1.1.1',timeout=3)"` **falha**.
5. Crie uma run pequena (exemplo no RUNBOOK) e acompanhe `/kiln/runs/<id>/events` ate `awaiting_plan_approval`; leia o plano e aprove.

## Fases -> arquivos

| Fase | Entrega | Arquivos | Saida (criterio) |
|---|---|---|---|
| 0 fundacao | schema, policy, streams | `kiln.sql`, `policy.py`, `events.py`, `store.py` | tabelas criadas; policy bloqueia a lista proibida (`test_policy`) |
| 1 planner | meta -> todo.md + contratos | `planner.py`, `contracts.py` | planos validos, reproduziveis, salvos no banco |
| 2 agente ponta a ponta | um agente no sandbox | `agents.py`, `sandbox.py`, `workspace.py` | gera codigo numa branch sem tocar o host |
| 3 verificador e gates | pronto objetivo | `verifier.py`, `gates.py` | gate reprova codigo quebrado de proposito |
| 4 paralelismo | N agentes, fila limitada | `orchestrator.py`, `kiln_worker.py` | `contract.changed` pausa dependentes |
| 5 aprovacoes e RBAC | humano no circuito | `routers/kiln.py` | comando de risco fica pendente ate aprovar |
| 6 observabilidade e orcamento | metricas, logs, limites | `observability.py`, `budget.py` | agente em loop e interrompido |

## Suite de aceitacao: as 7 falhas do video

`tests/kiln/test_replay_failures.py` reproduz cada falha e exige que seja barrada pela policy (em tempo real) e/ou pelos gates:

1. apagar `tests/` para ficar verde; 2. `DROP DATABASE` + trocar para Mongo; 3. renomear tipo compartilhado sem avisar;
4. `git push --force`; 5. afrouxar tsconfig/desligar lint; 6. `JWT_SECRET` no `.env`; 7. declarar "pronto" com o servico fora do ar.
**Se um deles falhar, o KILN nao esta pronto.**

## Primeiro alvo sugerido

App minimo de 3 agentes (`api`: Express + PostgreSQL com `/health` e login JWT; `web`: Next.js que chama a api; `db`: schema/migracoes)
com `database: postgres` e `forbidden_packages: [mongoose, mongodb]` no `stack.json`. Depois, use o Infra-Control como alvo real.

## Limites conhecidos (honestos)

- O verificador roda os comandos do `stack.json` que **voce aprova**; o `stack.json` e a parte mais sensivel do plano.
- Reconhecimento de testes: jest, vitest, pytest, mocha, node:test (TAP). Outros runners precisam de adaptador em `gates.parse_test_counts`.
- `git` e restrito de proposito; agentes nao fazem `rebase`/`merge`. Conflito de merge falha a tarefa (nunca e forcado).
- Egress e controlada por proxy/rede, nao pela policy; configure o squid do RUNBOOK para garantir.
- Single-writer por design: `KILN_MAX_PARALLEL=2` por causa do hardware do home lab. Aumente com cuidado (RAM do sandbox).

