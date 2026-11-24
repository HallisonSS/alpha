"""Testa o loop do agente (agents.py) com fakes em memoria: sem Docker/Postgres/LLM."""
import datetime as dt
import itertools
import json
from types import SimpleNamespace

from conftest_helpers import api_policy
from app.kiln.agents import AgentRunner
from app.kiln.budget import Limits
from app.kiln.llm import LLMResult
from app.kiln.sandbox import ExecResult
from app.kiln.workspace import WorkspaceError

_ids = itertools.count(1)


class FakeStore:
    def __init__(self):
        self.events, self.approvals, self.lessons, self.usage = [], {}, [], []
        self.task_state = None
        self.run = {"id": "r1", "status": "running", "created_at": dt.datetime.now(dt.timezone.utc)}

    def usage_totals(self, run_id):
        return {"tokens": sum(a + b for a, b in self.usage), "usd": 0.0}

    def get_run(self, run_id):
        return self.run

    def touch_task(self, tid): ...
    def update_task(self, tid, **f): self.task_state = f.get("state")
    def add_usage(self, run_id, task_id, kind, a, b, usd): self.usage.append((a, b))
    def add_event(self, run_id, actor, type_, data=None, task_id=None): self.events.append((actor, type_, data))
    def add_lesson(self, run_id, task_id, text): self.lessons.append(text)
    def approved_lessons(self, limit=10): return []
    def active_contracts(self, run_id): return {"contracts/routes.json": {"version": 1, "content": "{}", "sha256": "x"}}
    def get_tasks(self, run_id): return [{"agent": "api"}]
    def pending_approvals(self, run_id, kind=None): return [a for a in self.approvals.values() if a["status"] == "pending" and a["kind"] == kind]
    def count_open_contract_work(self, run_id): return 0
    def get_approval(self, aid): return self.approvals.get(aid)

    def create_approval(self, run_id, task_id, kind, request):
        aid = f"ap{next(_ids)}"
        self.approvals[aid] = {"id": aid, "kind": kind, "status": "pending", "request": request, "task_id": task_id}
        return self.approvals[aid]


class FakeSandbox:
    def __init__(self, rc=0, out="Tests: 3 passed, 3 total"):
        self.calls, self.rc, self.out = [], rc, out

    def exec(self, run_id, argv, *, workdir, network="none", timeout=300, env=None):
        self.calls.append((argv, network))
        return ExecResult(self.rc, self.out)


class FakeWS:
    def __init__(self, sb):
        self.sb, self.files = sb, {}

    def exists(self, where, rel): return rel in self.files
    def write(self, where, rel, content): self.files[rel] = content
    def delete(self, where, rel): self.files.pop(rel, None)

    def read(self, where, rel, cap=0):
        if rel not in self.files:
            raise WorkspaceError("nao e arquivo")
        return self.files[rel]

    def list_files(self, where, rel="."): return sorted(self.files)


class ScriptedLLM:
    def __init__(self, *steps):
        self.steps = list(steps)

    def chat(self, messages, json_mode=True):
        s = self.steps.pop(0)
        return LLMResult(s if isinstance(s, str) else json.dumps({"thoughts": "", "actions": s}), 100, 50, 0.0)


def make(steps, sandbox=None, store=None):
    store = store or FakeStore()
    sb = sandbox or FakeSandbox()
    ws = FakeWS(sb)
    runner = AgentRunner(SimpleNamespace(cmd_timeout=10), store, ScriptedLLM(*steps), ws)
    task = {"id": "t1", "agent": "api", "objective": "obj", "owned_paths": ["api/", "tasks/api.md"],
            "acceptance": ["npm test"], "state": store.task_state or {}}
    return runner, store, ws, sb, task


def run(runner, store, task, **lim):
    limits = Limits(**{"iterations": 10, "commands": 20, **lim})
    return runner.run(store.run, task, api_policy(), limits)


def test_happy_path_write_test_done():
    runner, store, ws, sb, task = make([
        [{"type": "write_file", "path": "api/src/a.ts", "content": "export const a = 1"},
         {"type": "run", "argv": ["npm", "test"]}],
        [{"type": "done", "summary": "feito"}]])
    out = run(runner, store, task)
    assert out.kind == "done" and ws.files["api/src/a.ts"] == "export const a = 1"
    assert sb.calls == [(["npm", "test"], "none")]
    assert store.usage  # tokens contabilizados


def test_denied_actions_are_fed_back_and_audited():
    runner, store, ws, sb, task = make([
        [{"type": "run", "argv": ["rm", "-rf", "tests/"]},
         {"type": "write_file", "path": "contracts/routes.json", "content": "{}"},
         {"type": "write_file", "path": "api/.env", "content": "A=1"}],
        [{"type": "done", "summary": "x"}]])
    out = run(runner, store, task)
    assert out.kind == "done" and not sb.calls and "contracts/routes.json" not in ws.files
    types = [e[1] for e in store.events]
    assert types.count("policy.deny") == 3
    assert {e[2]["rule"] for e in store.events} == {"rm.recursive", "path.protected", "secret.env_file"}


def test_config_change_pauses_then_resumes_after_approval():
    runner, store, ws, sb, task = make([
        [{"type": "write_file", "path": "api/tsconfig.json", "content": '{"strict": true}'}]])
    ws.files["api/tsconfig.json"] = "{}"
    out = run(runner, store, task)
    assert out.kind == "paused_approval"
    aid = next(iter(store.approvals))
    assert store.task_state["pending"][0]["approval_id"] == aid and ws.files["api/tsconfig.json"] == "{}"
    # humano aprova -> nova rodada com o estado salvo
    store.approvals[aid]["status"] = "approved"
    runner2, _, ws2, _, task2 = make([[{"type": "done", "summary": "ok"}]], store=store)
    ws2.files = ws.files
    task2["state"] = store.task_state
    out2 = run(runner2, store, task2)
    assert out2.kind == "done" and ws.files["api/tsconfig.json"] == '{"strict": true}'


def test_denied_approval_does_not_apply():
    runner, store, ws, sb, task = make([
        [{"type": "write_file", "path": "api/tsconfig.json", "content": '{"strict": false}'}]])
    ws.files["api/tsconfig.json"] = '{"strict": true}'
    run(runner, store, task)
    aid = next(iter(store.approvals))
    store.approvals[aid].update(status="denied", note="nao")
    runner2, _, ws2, _, task2 = make([[{"type": "done"}]], store=store)
    ws2.files, task2["state"] = ws.files, store.task_state
    assert run(runner2, store, task2).kind == "done"
    assert ws.files["api/tsconfig.json"] == '{"strict": true}'


def _full_contracts(store):
    from conftest_helpers import contract_files
    store.active_contracts = lambda run_id: {c.path: {"version": 1, "content": c.content, "sha256": "x"}
                                             for c in contract_files()}
    store.get_tasks = lambda run_id: [{"agent": "api"}, {"agent": "web"}]


def test_contract_proposal_valid_pauses_for_human():
    from conftest_helpers import ROUTES
    routes = dict(ROUTES, routes=ROUTES["routes"] + [{"method": "GET", "path": "/users", "owner": "api"}])
    runner, store, ws, sb, task = make([[{"type": "propose_contract_change", "path": "contracts/routes.json",
                                           "content": json.dumps(routes), "reason": "listar usuarios"}]])
    _full_contracts(store)
    out = run(runner, store, task)
    assert out.kind == "paused_approval"
    ap = next(iter(store.approvals.values()))
    assert ap["kind"] == "contract_change" and ap["request"]["path"] == "contracts/routes.json"
    assert "contracts/routes.json" not in ws.files  # nada e escrito pelo agente


def test_contract_proposal_invalid_is_denied_without_pausing():
    runner, store, ws, sb, task = make([
        [{"type": "propose_contract_change", "path": "contracts/routes.json", "content": "{nao e json", "reason": "x"},
         {"type": "propose_contract_change", "path": "api/src/a.ts", "content": "x", "reason": "x"}],
        [{"type": "done"}]])
    _full_contracts(store)
    out = run(runner, store, task)
    assert out.kind == "done" and not store.approvals


def test_invalid_json_gets_retry_message_and_loop_detected():
    runner, store, ws, sb, task = make(["isso nao e json", [{"type": "list_files", "path": "api/"}]] +
                                       [[{"type": "run", "argv": ["npm", "test"]}]] * 8)
    out = run(runner, store, task, iterations=20)
    assert out.kind == "budget" and out.detail.startswith("loop")


def test_iteration_and_token_budget():
    runner, store, ws, sb, task = make([[{"type": "list_files", "path": "api/"}]] * 5)
    out = run(runner, store, task, iterations=2)
    assert out.kind == "budget" and out.detail.startswith("iterations")
    runner, store, ws, sb, task = make([[{"type": "list_files", "path": "api/"}]] * 5)
    out = run(runner, store, task, tokens=100)
    assert out.kind == "budget" and out.detail.startswith("tokens")


def test_cancelled_run_stops_agent():
    runner, store, ws, sb, task = make([[{"type": "list_files", "path": "api/"}]] * 3)
    store.run["status"] = "cancelled"
    out = run(runner, store, task)
    assert out.kind == "budget" and out.detail.startswith("cancelled")


def test_lesson_is_recorded_not_applied():
    runner, store, ws, sb, task = make([[{"type": "propose_lesson", "text": "sempre rodar o linter"}, {"type": "done"}]])
    assert run(runner, store, task).kind == "done" and store.lessons == ["sempre rodar o linter"]
