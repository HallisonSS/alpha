"""Loop de um agente: LLM propoe acoes -> policy decide -> sandbox executa.

O agente NAO decide que terminou: `done` apenas o encaminha ao verificador.
Pausas (aprovacao humana / contrato em mudanca) salvam o estado em kiln_tasks.state
e a tarefa continua depois, sem perder o contexto.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import observability as obs
from .actions import ActionParseError, parse_actions
from .budget import BudgetExceeded, BudgetTracker, LoopDetector
from .contracts import validate_contract_files
from .models import Action, ApprovalKind, ContractFile, RunStatus
from .policy import Decision, KilnPolicy, Verdict
from .workspace import Workspace, WorkspaceError

log = logging.getLogger("kiln.agent")
MAX_RESULT_CHARS = 5000
MAX_MESSAGES = 26

SYSTEM = """Voce e o agente "{agent}" do KILN. Objetivo: {objective}
Voce SO pode alterar: {owned}. Todo o resto e somente leitura (contratos em contracts/ sao lei).
Responda SEMPRE com um unico JSON:
{{"thoughts":"curto","actions":[ ... ate 8 acoes ... ]}}
Acoes:
 {{"type":"write_file","path":"api/src/x.ts","content":"arquivo COMPLETO"}}
 {{"type":"delete_file","path":"api/src/old.ts"}}
 {{"type":"read_file","path":"..."}}  {{"type":"list_files","path":"api/"}}
 {{"type":"run","argv":["npm","test"]}}   (argv, nunca shell; sem curl/rede)
 {{"type":"propose_contract_change","path":"contracts/routes.json","content":"...","reason":"..."}}  (para tudo e exige aprovacao humana)
 {{"type":"propose_lesson","text":"regra curta para nao repetir um erro"}}
 {{"type":"done","summary":"o que foi feito"}}
REGRAS INEGOCIAVEIS:
1. Nunca apague, pule (skip/only) ou enfraqueca testes. Se um teste falha, conserte o CODIGO.
2. Nunca desligue regras de lint/tipos nem afrouxe tsconfig. Nunca use @ts-ignore/eslint-disable.
3. Nunca troque banco/stack definidos em contracts/stack.json. Nunca grave segredos: use process.env.
4. Mudou contrato necessario? Use propose_contract_change e aguarde. Nao renomeie tipos compartilhados por conta propria.
5. Plan first: escreva/atualize tasks/{agent}.md (checklist) antes de codar tarefas com 3+ passos.
6. Prove que funciona: rode build e testes antes de done. Escreva testes novos para o que criar.
7. Causa raiz, mudanca minima, sem gambiarras. Pergunte-se: "um engenheiro senior aprovaria isto?"
8. Seu `done` NAO conclui nada: o verificador independente roda build, testes e smoke.
{lessons}
CRITERIOS DE ACEITACAO:
{acceptance}
CONTRATOS CONGELADOS:
{contracts}
"""


@dataclass
class Outcome:
    kind: str  # done | paused_approval | paused_contract | failed | budget
    detail: str = ""


def _clip(s: str, n: int = MAX_RESULT_CHARS) -> str:
    return s if len(s) <= n else s[: n // 2] + "\n...[cortado]...\n" + s[-n // 2:]


class AgentRunner:
    def __init__(self, settings, store, llm, ws: Workspace, notify=None):
        self.s, self.store, self.llm, self.ws = settings, store, llm, ws
        self.notify = notify or (lambda *a, **k: None)

    # ----------------------------------------------------------------- #
    def run(self, run: dict, task: dict, policy: KilnPolicy, limits) -> Outcome:
        run_id, task_id, agent = str(run["id"]), str(task["id"]), task["agent"]
        state = dict(task.get("state") or {})
        started = run["created_at"].timestamp()
        tracker = BudgetTracker(
            limits, lambda: self.store.usage_totals(run_id), started,
            iterations=int(state.get("iterations", 0)), commands=int(state.get("commands", 0)),
            cancelled_fn=lambda: (self.store.get_run(run_id) or {}).get("status") in RunStatus.TERMINAL)
        loop = LoopDetector()
        loop.seed(state.get("recent", []))
        messages = state.get("messages") or self._initial_messages(run, task, state)
        pending = [Action.from_dict(a) for a in state.pop("pending", [])]
        state["feedback"] = ""

        def save(extra: dict | None = None) -> None:
            state.update({"messages": self._trim(messages), "iterations": tracker.iterations,
                          "commands": tracker.commands, "recent": list(loop.recent), **(extra or {})})
            self.store.update_task(task_id, state=state)

        try:
            while True:
                tracker.check()
                self.store.touch_task(task_id)
                if pending:
                    actions, pending = pending, []
                else:
                    if self._contract_frozen_for_others(run_id, task_id):
                        save()
                        return Outcome("paused_contract", "mudanca de contrato em analise")
                    tracker.tick_iteration()
                    t0 = time.time()
                    res = self.llm.chat(messages)
                    obs.STEP_SECONDS.labels(agent).observe(time.time() - t0)
                    self._charge(run_id, task_id, agent, res)
                    messages.append({"role": "assistant", "content": res.text})
                    try:
                        actions = parse_actions(res.text)
                    except ActionParseError as e:
                        messages.append({"role": "user", "content": f"Resposta invalida: {e}. Reenvie o JSON correto."})
                        continue
                results: list[str] = []
                for i, act in enumerate(actions):
                    loop.observe(act.fingerprint())
                    out = self._execute(act, run, task, policy, tracker, state)
                    if out.kind == "pause":
                        rest = [a.to_dict() for a in actions[i + 1:]]
                        messages.append({"role": "user", "content": "\n".join(results) or "(aguardando aprovacao)"})
                        save({"pending": [out.action.to_dict(), *rest]})
                        return Outcome("paused_approval", out.text)
                    results.append(f"[{i}] {act.type}: {out.text}")
                    if out.kind == "done":
                        messages.append({"role": "user", "content": "\n".join(results)})
                        save()
                        return Outcome("done", out.text)
                messages.append({"role": "user", "content": _clip("RESULTADOS:\n" + "\n".join(results), 12000)})
                save()
        except BudgetExceeded as e:
            obs.BUDGET.labels(e.scope).inc()
            save()
            return Outcome("budget", f"{e.scope}: {e.detail}")

    # ----------------------------------------------------------------- #
    @dataclass
    class _Out:
        kind: str  # ok | done | pause
        text: str = ""
        action: Action | None = None

    def _execute(self, act: Action, run, task, policy: KilnPolicy, tracker: BudgetTracker, state: dict):
        run_id, task_id, agent = str(run["id"]), str(task["id"]), task["agent"]
        approved = False
        if act.approval_id:  # retomada apos decisao humana
            ap = self.store.get_approval(act.approval_id)
            if ap is None or ap["status"] == "pending":
                return self._Out("pause", "aprovacao ainda pendente", act)
            if ap["status"] == "denied":
                self.store.add_event(run_id, "human", "approval.denied_applied", {"action": act.type, "note": ap.get("note")}, task_id)
                return self._Out("ok", f"NEGADO por humano: {ap.get('note') or 'sem nota'}")
            approved = True
            if act.type == "propose_contract_change":
                return self._Out("ok", "contrato aprovado e aplicado; sincronize e siga com a nova versao")
        try:
            if act.type == "done":
                return self._Out("done", act.summary or "agente declarou conclusao")
            if act.type == "propose_lesson":
                self.store.add_lesson(run_id, task_id, act.text)
                return self._Out("ok", "licao registrada (aguarda aprovacao humana)")
            if act.type in ("read_file", "list_files"):
                d = policy.check_read(act.path)
                self._audit(run_id, task_id, act, d)
                if d.verdict is not Verdict.ALLOW:
                    return self._Out("ok", str(d))
                if act.type == "read_file":
                    return self._Out("ok", _clip(self.ws.read(agent, act.path)))
                return self._Out("ok", "\n".join(self.ws.list_files(agent, act.path)) or "(vazio)")
            if act.type == "write_file":
                d = policy.check_write(act.path, act.content, exists=self.ws.exists(agent, act.path), approved=approved)
            elif act.type == "delete_file":
                d = policy.check_delete(act.path, approved=approved)
            elif act.type == "run":
                tracker.tick_command()
                d = policy.check_command(act.argv, approved=approved)
            elif act.type == "propose_contract_change":
                return self._propose_contract(act, run, task)
            else:
                return self._Out("ok", f"acao desconhecida: {act.type}")
            self._audit(run_id, task_id, act, d)
            if d.verdict is Verdict.DENY:
                return self._Out("ok", str(d))
            if d.verdict is Verdict.APPROVAL:
                ap = self.store.create_approval(run_id, task_id, ApprovalKind.CONFIG_CHANGE if act.type != "run" else ApprovalKind.COMMAND,
                                                {"action": act.to_dict(), "rule": d.rule, "reason": d.reason, "agent": agent})
                obs.APPROVALS.labels(ap["kind"], "created").inc()
                self.notify(run_id, str(ap["id"]), ap["kind"], f"{agent}: {d.reason}")
                act.approval_id = str(ap["id"])
                return self._Out("pause", d.reason, act)
            return self._apply(act, run_id, task_id, agent, d)
        except WorkspaceError as e:
            return self._Out("ok", f"ERRO: {e}")

    def _apply(self, act: Action, run_id: str, task_id: str, agent: str, d: Decision):
        if act.type == "write_file":
            self.ws.write(agent, act.path, act.content)
            return self._Out("ok", f"escrito {act.path} ({len(act.content)} bytes)")
        if act.type == "delete_file":
            self.ws.delete(agent, act.path)
            return self._Out("ok", f"removido {act.path}")
        t0 = time.time()
        r = self.ws.sb.exec(run_id, act.argv, workdir=f"{run_id}/wt/{agent}", network=d.network,
                            timeout=self.s.cmd_timeout)
        self.store.add_event(run_id, agent, "command", {"argv": act.argv, "rc": r.rc, "timed_out": r.timed_out,
                                                         "seconds": round(time.time() - t0, 1),
                                                         "output_tail": r.output[-1500:]}, task_id)
        flag = " (TIMEOUT)" if r.timed_out else ""
        return self._Out("ok", f"rc={r.rc}{flag}\n{_clip(r.output)}")

    def _propose_contract(self, act: Action, run, task):
        run_id, task_id = str(run["id"]), str(task["id"])
        if not act.path.startswith("contracts/") or ".." in act.path:
            return self._Out("ok", "DENY[contract.path]: so e possivel propor mudancas em contracts/")
        merged = {p: c["content"] for p, c in self.store.active_contracts(run_id).items()}
        merged[act.path] = act.content
        names = [t["agent"] for t in self.store.get_tasks(run_id)]
        errs = validate_contract_files([ContractFile(p, c) for p, c in merged.items()], names)
        if errs:
            return self._Out("ok", "DENY[contract.invalid]: " + "; ".join(errs[:5]))
        ap = self.store.create_approval(run_id, task_id, ApprovalKind.CONTRACT_CHANGE,
                                        {"path": act.path, "content": act.content, "reason": act.reason,
                                         "agent": task["agent"], "action": act.to_dict()})
        obs.APPROVALS.labels(ApprovalKind.CONTRACT_CHANGE, "created").inc()
        self.store.add_event(run_id, task["agent"], "contract.proposed", {"path": act.path, "reason": act.reason}, task_id)
        self.notify(run_id, str(ap["id"]), ApprovalKind.CONTRACT_CHANGE, f"{task['agent']} propoe mudar {act.path}: {act.reason}")
        act.approval_id = str(ap["id"])
        return self._Out("pause", f"contrato {act.path} em aprovacao", act)

    # ----------------------------------------------------------------- #
    def _audit(self, run_id: str, task_id: str, act: Action, d: Decision) -> None:
        obs.POLICY.labels(d.verdict.value, d.rule).inc()
        if d.verdict is not Verdict.ALLOW:
            self.store.add_event(run_id, "policy", f"policy.{d.verdict.value}",
                                 {"rule": d.rule, "reason": d.reason, "action": act.type,
                                  "target": act.path or act.argv}, task_id)

    def _charge(self, run_id, task_id, agent, res) -> None:
        self.store.add_usage(run_id, task_id, "agent", res.tokens_in, res.tokens_out, res.cost_usd)
        obs.TOKENS.labels(agent, "in").inc(res.tokens_in)
        obs.TOKENS.labels(agent, "out").inc(res.tokens_out)
        obs.COST.labels(agent).inc(res.cost_usd)

    def _contract_frozen_for_others(self, run_id: str, task_id: str) -> bool:
        pend = self.store.pending_approvals(run_id, ApprovalKind.CONTRACT_CHANGE)
        return any(str(p.get("task_id")) != task_id for p in pend) or \
            (not pend and self.store.count_open_contract_work(run_id) > 0)

    def _initial_messages(self, run, task, state) -> list[dict]:
        contracts = self.store.active_contracts(str(run["id"]))
        ctext = "\n".join(f"--- {p} (v{c['version']}) ---\n{_clip(c['content'], 6000)}" for p, c in contracts.items())
        lessons = self.store.approved_lessons()
        sysmsg = SYSTEM.format(
            agent=task["agent"], objective=task["objective"], owned=", ".join(task["owned_paths"]),
            lessons=("LICOES APROVADAS:\n" + "\n".join(f"- {x}" for x in lessons)) if lessons else "",
            acceptance="\n".join(f"- {a}" for a in task["acceptance"]) or "- (nenhum)", contracts=ctext)
        user = "Comece. Liste os arquivos relevantes, planeje e implemente."
        if state.get("feedback"):
            user = ("O verificador REPROVOU sua entrega anterior. Corrija a CAUSA RAIZ (sem mexer em testes/configs "
                    "para passar):\n" + state["feedback"])
        return [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}]

    @staticmethod
    def _trim(messages: list[dict]) -> list[dict]:
        if len(messages) <= MAX_MESSAGES:
            return messages
        return messages[:2] + [{"role": "user", "content": "[historico antigo resumido/omitido]"}] + messages[-(MAX_MESSAGES - 3):]
