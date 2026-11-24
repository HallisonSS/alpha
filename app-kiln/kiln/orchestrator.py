"""Orquestrador: maquina de estados nivelada (level-triggered) + pool de jobs.

`advance(run_id)` olha o ESTADO no Postgres e agenda o proximo passo; eventos do
Redis so "acordam" o orquestrador. Perder um evento nao trava nada (tick periodico).
Transicoes sao atomicas (UPDATE ... WHERE status = esperado), entao nenhum job roda duas vezes.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import observability as obs
from .budget import BudgetExceeded, BudgetTracker, Limits
from .contracts import STACK_PATH, parse_stack, validate_contract_files
from .agents import AgentRunner
from .models import ApprovalKind, Plan, RunStatus, TaskStatus
from .planner import Planner, PlanningError
from .policy import build_policy
from .verifier import Verifier
from .workspace import Workspace, WorkspaceError

log = logging.getLogger("kiln.orchestrator")

RULES_MD = """# KILN_RULES (somente leitura)
- Contratos em `contracts/` sao lei; mudar exige propose_contract_change + aprovacao humana.
- Cada agente so escreve nas suas pastas. Merge e feito apenas pelo orquestrador.
- Nunca apagar/pular testes, nunca afrouxar lint/tsconfig, nunca gravar segredos, nunca trocar a stack.
- "Pronto" = gates do verificador (build, testes, smoke, contratos, stack, segredos), nao opiniao do agente.
"""
LESSONS_MD = "# tasks/lessons.md\n\nLicoes aprovadas por humanos (o KILN as injeta nos prompts).\n"


class Orchestrator:
    def __init__(self, settings, store, bus, llm, sandbox):
        self.s, self.store, self.bus, self.llm, self.sandbox = settings, store, bus, llm, sandbox
        self.verifier = Verifier(settings, llm)
        self.pool = ThreadPoolExecutor(max_workers=settings.max_parallel, thread_name_prefix="kiln-job")
        self.slots = threading.BoundedSemaphore(settings.max_parallel)
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    # ----------------------------- entrada ------------------------------ #
    def handle(self, fields: dict) -> None:
        run_id = fields.get("run_id")
        if run_id:
            self.advance(run_id)

    def tick(self) -> None:
        """Rede de seguranca: reavalia runs ativas e recupera jobs mortos."""
        with self._lock:
            live = set(self._inflight)
        for row in self.store.reap_stale_tasks(self.s.stale_seconds, live):
            log.warning("tarefa %s/%s sem batimento: recolocada na fila", row["run_id"], row["agent"])
            self.store.add_event(str(row["run_id"]), "reaper", "task.reaped", {"agent": row["agent"]}, str(row["id"]))
        for row in self.store.reap_stale_runs(self.s.stale_seconds):
            log.error("run %s presa em fase curta: marcada como failed", row["id"])
        for run_id in self.store.active_run_ids():
            self.advance(run_id)

    # ----------------------------- advance ------------------------------ #
    def advance(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if not run or run["status"] in RunStatus.TERMINAL:
            return
        st = run["status"]
        if st == RunStatus.CREATED:
            self._submit(self._job_plan, run_id,
                         claim=lambda: self.store.transition_run(run_id, RunStatus.CREATED, RunStatus.PLANNING))
        elif st == RunStatus.AWAITING_PLAN:
            ap = self.store.latest_approval(run_id, ApprovalKind.PLAN)
            if ap and ap["status"] == "approved":
                self._submit(self._job_start, run_id,
                             claim=lambda: self.store.transition_run(run_id, RunStatus.AWAITING_PLAN, RunStatus.STARTING))
            elif ap and ap["status"] == "denied":
                self._finish_run(run_id, RunStatus.AWAITING_PLAN, RunStatus.FAILED, "plano negado: " + (ap.get("note") or ""))
        elif st == RunStatus.RUNNING:
            self._advance_running(run)

    def _advance_running(self, run: dict) -> None:
        run_id = str(run["id"])
        for ap in self.store.claim_unapplied_contract_changes(run_id):
            self._apply_contract_change(run_id, ap)
        tasks = self.store.get_tasks(run_id)
        failed = [t for t in tasks if t["status"] == TaskStatus.FAILED]
        if failed:
            f = failed[0]
            self._finish_run(run_id, RunStatus.RUNNING, RunStatus.FAILED, f"agente {f['agent']} falhou: {f.get('error')}")
            return
        open_contract = self.store.count_open_contract_work(run_id)
        for t in tasks:
            tid = str(t["id"])
            if t["status"] == TaskStatus.AWAITING_APPROVAL:
                pend = (t.get("state") or {}).get("pending") or []
                aid = pend[0].get("approval_id") if pend else None
                ap = self.store.get_approval(aid) if aid else None
                if ap is None or ap["status"] != "pending":
                    self.store.transition_task(tid, TaskStatus.AWAITING_APPROVAL, TaskStatus.RESUME)
            elif t["status"] == TaskStatus.BLOCKED_CONTRACT and open_contract == 0:
                self.store.transition_task(tid, TaskStatus.BLOCKED_CONTRACT, TaskStatus.RESUME)
        tasks = self.store.get_tasks(run_id)
        merged = {t["agent"] for t in tasks if t["status"] == TaskStatus.MERGED}
        for t in tasks:
            tid = str(t["id"])
            if t["status"] in (TaskStatus.PENDING, TaskStatus.RESUME) and set(t["depends_on"]) <= merged:
                self._submit(self._job_agent, run_id, tid, task_id=tid, claim=lambda tid=tid, t=t:
                             self.store.transition_task(tid, t["status"], TaskStatus.RUNNING))
            elif t["status"] == TaskStatus.AGENT_DONE:
                self._submit(self._job_verify, run_id, tid, task_id=tid, claim=lambda tid=tid:
                             self.store.transition_task(tid, TaskStatus.AGENT_DONE, TaskStatus.VERIFYING))
        if tasks and all(t["status"] == TaskStatus.MERGED for t in tasks):
            self._submit(self._job_integrate, run_id,
                         claim=lambda: self.store.transition_run(run_id, RunStatus.RUNNING, RunStatus.INTEGRATING))

    # ------------------------------ pool -------------------------------- #
    def _submit(self, fn, run_id: str, *args, task_id: str | None = None, claim=None) -> bool:
        if not self.slots.acquire(blocking=False):
            return False  # sem vaga: o evento job.done vai reavaliar
        if claim is not None and not claim():
            self.slots.release()
            return False
        if task_id:
            with self._lock:
                self._inflight.add(task_id)
        self.pool.submit(self._wrap, fn, run_id, args, task_id)
        return True

    def _wrap(self, fn, run_id: str, args: tuple, task_id: str | None) -> None:
        obs.INFLIGHT.inc()
        try:
            fn(run_id, *args)
        except Exception as e:
            log.exception("job %s quebrou", fn.__name__, extra={"run_id": run_id})
            try:
                self.store.add_event(run_id, "worker", "job.crash", {"job": fn.__name__, "error": repr(e)[:500]}, task_id)
                if task_id is None:  # job de run: nao deixa a run presa
                    self.store.transition_run(run_id, [RunStatus.PLANNING, RunStatus.STARTING, RunStatus.INTEGRATING],
                                              RunStatus.FAILED, error=f"{fn.__name__}: {e!r}"[:500])
            except Exception:
                log.exception("falha ao registrar crash")
        finally:
            obs.INFLIGHT.dec()
            if task_id:
                with self._lock:
                    self._inflight.discard(task_id)
            self.slots.release()
            try:
                self.bus.publish("job.done", run_id)
            except Exception:
                log.exception("falha ao publicar job.done")

    # ------------------------------ helpers ----------------------------- #
    def _ws(self, run_id: str) -> Workspace:
        return Workspace(self.s, self.sandbox, run_id)

    def _notify(self, run_id: str, approval_id: str, kind: str, summary: str) -> None:
        try:
            self.bus.notify_approval(run_id, approval_id, kind, summary)
            self.bus.publish("approval.requested", run_id, approval_id=approval_id)
        except Exception:
            log.exception("falha ao notificar aprovacao")

    def _finish_run(self, run_id: str, expected, status: str, error: str = "") -> None:
        if self.store.transition_run(run_id, expected, status, error=error[:1000] or None):
            obs.RUNS.labels(status).inc()
            self.store.add_event(run_id, "orchestrator", f"run.{status}", {"error": error[:1000]})
            log.info("run %s -> %s", run_id, status, extra={"run_id": run_id})

    def _task_event(self, run_id: str, task: dict, type_: str, data: dict) -> None:
        obs.TASKS.labels(task["agent"], type_).inc()
        self.store.add_event(run_id, task["agent"], type_, data, str(task["id"]))

    def _stack(self, run_id: str):
        contracts = self.store.active_contracts(run_id)
        return parse_stack(contracts[STACK_PATH]["content"]), {p: c["sha256"] for p, c in contracts.items()}

    # ------------------------------ jobs -------------------------------- #
    def _job_plan(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        limits = Limits.from_settings(self.s, run["budget"])
        tracker = BudgetTracker(limits, lambda: self.store.usage_totals(run_id), run["created_at"].timestamp())

        def charge(res, kind):
            self.store.add_usage(run_id, None, kind, res.tokens_in, res.tokens_out, res.cost_usd)
            obs.TOKENS.labels(kind, "in").inc(res.tokens_in)
            obs.TOKENS.labels(kind, "out").inc(res.tokens_out)
            tracker.check()

        try:
            plan = Planner(self.llm, self.s.max_agents, charge).plan(run["goal"], self.store.approved_lessons())
        except PlanningError as e:
            return self._finish_run(run_id, RunStatus.PLANNING, RunStatus.FAILED, f"planner: {e}")
        except BudgetExceeded as e:
            obs.BUDGET.labels(e.scope).inc()
            return self._finish_run(run_id, RunStatus.PLANNING, RunStatus.BUDGET, str(e))
        self.store.update_run(run_id, plan=plan.to_dict())
        ap = self.store.create_approval(run_id, None, ApprovalKind.PLAN, {
            "summary": plan.summary, "todo_md": plan.todo_md,
            "agents": [a.to_dict() for a in plan.agents],
            "contracts": [{"path": c.path, "content": c.content} for c in plan.contracts]})
        obs.APPROVALS.labels(ApprovalKind.PLAN, "created").inc()
        self.store.add_event(run_id, "planner", "plan.created", {"agents": [a.name for a in plan.agents]})
        if not self.store.transition_run(run_id, RunStatus.PLANNING, RunStatus.AWAITING_PLAN):
            return
        if self.s.auto_approve_plan:
            self.store.decide_approval(str(ap["id"]), True, "auto", "KILN_AUTO_APPROVE_PLAN")
        else:
            self._notify(run_id, str(ap["id"]), ApprovalKind.PLAN, f"Plano aguardando aprovacao: {plan.summary[:200]}")

    def _job_start(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        plan = Plan.from_dict(run["plan"])
        errs = validate_contract_files(plan.contracts, [a.name for a in plan.agents])
        if errs:  # defesa em profundidade: o plano ja foi validado
            return self._finish_run(run_id, RunStatus.STARTING, RunStatus.FAILED, "contratos invalidos: " + "; ".join(errs[:3]))
        self.store.create_tasks(run_id, plan.agents)
        files = {c.path: c.content for c in plan.contracts}
        self.store.save_contracts(run_id, files)
        repo_files = dict(files)
        repo_files.update({"KILN_RULES.md": RULES_MD, "tasks/todo.md": plan.todo_md, "tasks/lessons.md": LESSONS_MD})
        for a in plan.agents:
            repo_files[f"tasks/{a.name}.md"] = f"# {a.name}\n\nObjetivo: {a.objective}\n\n" + \
                "\n".join(f"- [ ] {c}" for c in a.acceptance) + "\n"
        try:
            self._ws(run_id).init_repo(repo_files)
        except WorkspaceError as e:
            return self._finish_run(run_id, RunStatus.STARTING, RunStatus.FAILED, f"workspace: {e}")
        self.store.add_event(run_id, "orchestrator", "run.started", {"agents": [a.name for a in plan.agents]})
        self.store.transition_run(run_id, RunStatus.STARTING, RunStatus.RUNNING)

    def _job_agent(self, run_id: str, task_id: str) -> None:
        run, task = self.store.get_run(run_id), self.store.get_task(task_id)
        agent, ws = task["agent"], self._ws(run_id)
        self._task_event(run_id, task, "task.started", {"attempt": task["attempts"] + 1})
        try:
            ws.ensure_worktree(agent)
            ok, err = ws.sync_from_main(agent)
            if not ok:
                raise WorkspaceError(f"sync com main falhou: {err}")
            stack, _ = self._stack(run_id)
            policy = build_policy(task["owned_paths"], stack.forbidden_packages)
            out = AgentRunner(self.s, self.store, self.llm, ws, notify=self._notify).run(
                run, task, policy, Limits.from_settings(self.s, run["budget"]))
        except WorkspaceError as e:
            self.store.transition_task(task_id, TaskStatus.RUNNING, TaskStatus.FAILED, error=str(e)[:500])
            return self._task_event(run_id, task, "task.failed", {"error": str(e)})
        if out.kind == "done":
            try:
                ws.commit_all(agent, f"kiln({agent}): {out.detail[:60]}")
            except WorkspaceError as e:
                self.store.transition_task(task_id, TaskStatus.RUNNING, TaskStatus.FAILED, error=str(e)[:500])
                return self._task_event(run_id, task, "task.failed", {"error": str(e)})
            self.store.transition_task(task_id, TaskStatus.RUNNING, TaskStatus.AGENT_DONE)
            self._task_event(run_id, task, "task.agent_done", {"summary": out.detail})
        elif out.kind == "paused_approval":
            self.store.transition_task(task_id, TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL)
            self._task_event(run_id, task, "task.awaiting_approval", {"detail": out.detail})
        elif out.kind == "paused_contract":
            self.store.transition_task(task_id, TaskStatus.RUNNING, TaskStatus.BLOCKED_CONTRACT)
            self._task_event(run_id, task, "task.blocked_contract", {})
        else:  # budget / loop / iteracoes / comandos / cancelada
            self.store.transition_task(task_id, TaskStatus.RUNNING, TaskStatus.FAILED, error=out.detail[:500])
            self._task_event(run_id, task, "task.failed", {"error": out.detail})
            scope = out.detail.split(":", 1)[0]
            if scope in ("tokens", "usd", "time"):
                self._finish_run(run_id, RunStatus.RUNNING, RunStatus.BUDGET, out.detail)

    def _job_verify(self, run_id: str, task_id: str) -> None:
        run, task = self.store.get_run(run_id), self.store.get_task(task_id)
        agent, ws = task["agent"], self._ws(run_id)
        try:
            stack, frozen = self._stack(run_id)
            approved = {(a["request"].get("action") or {}).get("path", "")
                        for a in self.store.list_approvals("approved", run_id)
                        if a["kind"] == ApprovalKind.CONFIG_CHANGE and str(a.get("task_id")) == task_id}
            rep = self.verifier.verify_task(run, task, stack, frozen, {p for p in approved if p}, ws)
        except Exception as e:
            log.exception("verificacao quebrou", extra={"run_id": run_id, "agent": agent})
            rep = None
            err = f"verificador quebrou: {e!r}"[:500]
        if rep is None:
            return self._retry_or_fail(run_id, task, err, {})
        self.store.add_event(run_id, "verifier", "verify.result", rep.to_dict(), task_id)
        if not rep.passed:
            return self._retry_or_fail(run_id, task, rep.gates.summary(), rep.to_dict())
        with self.store.run_lock(run_id):
            ok, msg = ws.merge_into_main(agent)
        if not ok:
            self.store.transition_task(task_id, TaskStatus.VERIFYING, TaskStatus.FAILED, error=f"merge: {msg}"[:500])
            return self._task_event(run_id, task, "task.failed", {"error": f"merge: {msg}"})
        self.store.transition_task(task_id, TaskStatus.VERIFYING, TaskStatus.MERGED,
                                   baseline_tests=int(rep.gates.tests_after or 0), result=rep.to_dict(), error=None)
        self._task_event(run_id, task, "task.merged", {"tests": rep.gates.tests_after})

    def _retry_or_fail(self, run_id: str, task: dict, summary: str, result: dict) -> None:
        tid, attempts = str(task["id"]), task["attempts"] + 1
        if attempts >= self.s.max_task_attempts:
            self.store.transition_task(tid, TaskStatus.VERIFYING, TaskStatus.FAILED, attempts=attempts,
                                       error=summary[:500], result=result)
            self._task_event(run_id, task, "task.failed", {"attempts": attempts, "summary": summary[:500]})
        else:  # contexto reiniciado com o feedback objetivo dos gates
            self.store.transition_task(tid, TaskStatus.VERIFYING, TaskStatus.PENDING, attempts=attempts,
                                       state={"feedback": summary}, result=result)
            self._task_event(run_id, task, "task.retry", {"attempts": attempts, "summary": summary[:500]})

    def _job_integrate(self, run_id: str) -> None:
        run, tasks, ws = self.store.get_run(run_id), self.store.get_tasks(run_id), self._ws(run_id)
        stack, frozen = self._stack(run_id)
        rep = self.verifier.verify_integration(run, tasks, stack, frozen, ws)
        self.store.add_event(run_id, "verifier", "integration.result", rep.to_dict())
        if rep.passed:
            self._finish_run(run_id, RunStatus.INTEGRATING, RunStatus.SUCCEEDED)
        else:
            self._finish_run(run_id, RunStatus.INTEGRATING, RunStatus.FAILED, "integracao reprovada: " + rep.gates.summary())

    def _apply_contract_change(self, run_id: str, ap: dict) -> None:
        req = ap["request"]
        v = self.store.supersede_contract(run_id, req["path"], req["content"], ap.get("decided_by") or "human")
        self._ws(run_id).write_contracts({req["path"]: req["content"]})
        self.store.add_event(run_id, "orchestrator", "contract.applied",
                             {"path": req["path"], "version": v, "approval": str(ap["id"])})
        obs.APPROVALS.labels(ApprovalKind.CONTRACT_CHANGE, "applied").inc()
