# KILN: runbook de operacao

## 1. Subir (a partir de ~/Documentos/agent)

```bash
# 1) copie o pacote por cima do repo (nada existente e sobrescrito, exceto onde indicado no README)
# 2) variaveis: acrescente .env.kiln.example ao seu .env e preencha KILN_ADMIN_TOKEN, KILN_LLM_MODEL
# 3) dependencias: acrescente requirements-kiln.txt ao agent/requirements.txt
# 4) rede de egress e imagem do sandbox
docker network create kiln_egress
docker build -t kiln-sandbox:latest kiln-sandbox/
# 5) schema (o worker tambem aplica sozinho com KILN_AUTO_MIGRATE=true)
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < db/kiln.sql
# 6) servicos
docker compose -f docker-compose.yml -f docker-compose.kiln.yml --env-file .env up -d --build agent-api kiln-worker docker-proxy
docker compose logs -f kiln-worker     # esperado: "kiln-worker ... pronto"
```

## 2. Egress controlada (somente instalar dependencias)

Os comandos dos agentes rodam **sem rede**. So `npm ci/install`, `pip install` e `npm audit` recebem
a rede `kiln_egress`. Para limitar a 3 dominios, ponha um proxy squid nessa rede:

```
# squid.conf (container kiln-egress-proxy, redes: kiln_egress + uma rede com saida para a internet)
acl allowed dstdomain registry.npmjs.org .npmjs.org pypi.org files.pythonhosted.org
http_access allow allowed
http_access deny all
http_port 3128
```
e defina `KILN_EGRESS_PROXY=http://kiln-egress-proxy:3128`. Sem proxy, a rede `kiln_egress` fica aberta:
aceitavel no home lab, mas prefira o proxy.

## 3. Operacao diaria

```bash
T="X-Kiln-Token: $KILN_ADMIN_TOKEN"; U="X-Kiln-User: seu-nome"; API=http://localhost:8000
# criar run
curl -s -X POST $API/kiln/runs -H "$T" -H "$U" -H 'content-type: application/json' \
  -d '{"goal":"Criar o app de exemplo: api Express+PostgreSQL com /health e login JWT, web Next.js que consome a api","budget":{"usd":2}}'
# acompanhar
curl -s $API/kiln/runs/<RUN_ID> -H "$T" | jq '.run.status, .tasks[]|{agent,status,attempts}'
curl -s "$API/kiln/runs/<RUN_ID>/events?after=0" -H "$T" | jq -r '.[]|"\(.id) \(.actor) \(.type)"'
# aprovacoes pendentes (plano, contratos, configs)
curl -s $API/kiln/approvals -H "$T" | jq '.[]|{id,kind,request}'
curl -s -X POST $API/kiln/approvals/<ID>/decide -H "$T" -H "$U" -H 'content-type: application/json' \
  -d '{"decision":"approved","note":"ok"}'
# licoes propostas pelos agentes
curl -s $API/kiln/lessons -H "$T"
# cancelar
curl -s -X POST $API/kiln/runs/<RUN_ID>/cancel -H "$T"
```

**Sempre leia o plano antes de aprovar**: o `stack.json` define os comandos que o verificador executa,
as dependencias/padroes proibidos e os servicos do smoke.

## 4. Observabilidade

Prometheus (`prometheus.yml`):
```yaml
- job_name: kiln-worker
  static_configs: [{targets: ['kiln-worker:9108']}]
```
Alertas (`kiln-rules.yml`):
```yaml
groups:
- name: kiln
  rules:
  - alert: KilnBudgetExceeded
    expr: increase(kiln_budget_exceeded_total[15m]) > 0
    labels: {severity: warning}
    annotations: {summary: "KILN estourou orcamento ({{ $labels.scope }})"}
  - alert: KilnAgentLoop
    expr: increase(kiln_budget_exceeded_total{scope="loop"}[30m]) > 2
    labels: {severity: warning}
  - alert: KilnPolicyDenialSpike
    expr: sum(increase(kiln_policy_decisions_total{verdict="deny"}[10m])) > 30
    labels: {severity: info}
    annotations: {summary: "Muitas negacoes de policy: prompt ou modelo derivando?"}
  - alert: KilnGateFailing
    expr: sum by (gate) (increase(kiln_gate_results_total{result="fail"}[1h])) > 5
    labels: {severity: info}
  - alert: KilnDeadLetter
    expr: increase(kiln_events_dead_total[1h]) > 0
    labels: {severity: warning}
  - alert: KilnApprovalWaiting
    expr: time() - max(kiln_approval_oldest_pending_timestamp) > 3600
    labels: {severity: info}
```
(a ultima so existe se voce exportar esse gauge; opcional. Alternativa: painel Postgres com `kiln_approvals` pendentes.)

Loki/Promtail: os logs do `kiln-worker` sao JSON (uma linha por evento). Pipeline:
```yaml
pipeline_stages:
  - json: {expressions: {level: level, run_id: run_id, agent: agent, rule: rule}}
  - labels: {level:, agent:}
```
Paineis Grafana sugeridos: runs por status (`kiln_runs_total`), tokens e custo (`kiln_tokens_total`,
`kiln_cost_usd_total`), negacoes por regra (`kiln_policy_decisions_total{verdict="deny"}`), falhas por gate
(`kiln_gate_results_total{result="fail"}`), duracao de verificacao (`kiln_verify_seconds`), jobs em voo
(`kiln_jobs_inflight`). Datasource Postgres: views `kiln_v_run_usage` e `kiln_v_policy_denials`.

## 5. Problemas comuns

| Sintoma | Causa provavel | Acao |
|---|---|---|
| Worker nao sobe: `imagem nao encontrada` | sandbox nao buildado | `docker build -t kiln-sandbox:latest kiln-sandbox/` |
| `Permission denied` ao escrever no workspace | uid do worker != 10001 | manter `user: "10001:10001"`; `prepare()` ajusta o dono do volume |
| `npm ci` falha por rede | rede `kiln_egress` ausente ou proxy bloqueando | `docker network create kiln_egress`; liberar dominios |
| Run parada em `awaiting_plan_approval` | aprovacao pendente | decidir em `/kiln/approvals` |
| Tarefa em `blocked_contract` | mudanca de contrato pendente | decidir a aprovacao; o tick retoma |
| Tarefa em `failed` por `loop` | agente repetindo acao | ver eventos `policy.deny`; ajustar meta/contratos; relancar run |
| Verificacao sempre reprova `tests` | saida de teste nao reconhecida | usar jest/vitest/pytest/mocha/TAP ou ajustar o comando em `stack.json` |
| Evento em `kiln:dead` | handler falhou 5x | `redis-cli XRANGE kiln:dead - +`; corrigir causa; reenviar com `XADD kiln:events` |
| `docker-proxy` 403 | permissao do proxy | habilitar so a flag necessaria (CONTAINERS, POST, IMAGES, INFO) |

## 6. Manutencao

- Workspaces: `/kiln/workspaces/<run>` fica apos o fim (para auditoria). Limpe runs antigas:
  `docker compose exec kiln-worker sh -c 'find /kiln/workspaces -maxdepth 1 -mtime +14 -exec rm -rf {} +'`.
- Containers orfaos: `docker ps -a --filter label=kiln=exec`; remova os antigos.
- `kiln_events` e append-only; para retencao, particione ou use `TRUNCATE` em janela de manutencao (o trigger bloqueia `DELETE`).
- Backup: inclua as tabelas `kiln_*` no pg_dump existente.
- Worker unico por padrao. Para varios, mantenha `KILN_WORKER_ID` distinto; as transicoes atomicas e o reaper sao seguros.

## 7. Checklist de seguranca

- [ ] `KILN_ADMIN_TOKEN` longo e fora do git; `/kiln/*` nao exposto publicamente sem proxy/VPN.
- [ ] `docker-proxy` sem `EXEC`, `VOLUMES`, `BUILD`; socket montado `:ro`.
- [ ] Nenhum segredo no volume `kiln_workspaces` nem no ambiente do sandbox.
- [ ] Plano e contratos revisados por humano antes de aprovar.
- [ ] `KILN_AUTO_APPROVE_PLAN=false`.
- [ ] Orcamento por run definido (`KILN_BUDGET_USD`) e alertas ativos.
