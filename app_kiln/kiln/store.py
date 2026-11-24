"""Acesso ao Postgres (psycopg 3). Uma conexao curta por operacao: simples e thread-safe."""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterable

from .contracts import sha256_text
from .models import AgentSpec, RunStatus

RUN_COLS = {"status", "plan", "budget", "config", "error"}
TASK_COLS = {"status", "attempts", "branch", "state", "result", "baseline_tests", "error"}
JSON_COLS = {"plan", "budget", "config", "state", "result"}


class Store:
    def __init__(self, settings):
        self.dsn = settings.db_dsn

    # ------------------------------------------------------------------ #
    @contextmanager
    def _conn(self):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True) as conn:
            yield conn

    def _q(self, sql: str, params: Iterable | None = None, *, one=False, many=False):
        with self._conn() as c:
            cur = c.execute(sql, params or ())
            if one:
                return cur.fetchone()
            if many:
                return cur.fetchall()
            return cur.rowcount

    @staticmethod
    def _j(v: Any):
        from psycopg.types.json import Jsonb

        return Jsonb(v)

    def _set_clause(self, fields: dict, allowed: set) -> tuple[str, list]:
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"colunas nao permitidas: {bad}")
        parts, vals = [], []
        for k, v in fields.items():
            parts.append(f"{k} = %s")
            vals.append(self._j(v) if k in JSON_COLS else v)
        return ", ".join(parts + ["updated_at = now()"]), vals

    def apply_schema(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            sql = f.read()
        with self._conn() as c:
            c.execute(sql)

    @contextmanager
    def run_lock(self, run_id: str):
        """Lock consultivo por run (serializa merges)."""
        import psycopg

        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (str(run_id),))
            try:
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (str(run_id),))

    # ------------------------------ runs ------------------------------- #
    def create_run(self, goal: str, budget: dict, config: dict, created_by: str) -> dict:
        return self._q(
            "INSERT INTO kiln_runs (goal, budget, config, created_by) VALUES (%s,%s,%s,%s) RETURNING *",
            (goal, self._j(budget), self._j(config), created_by), one=True)

    def get_run(self, run_id: str) -> dict | None:
        return self._q("SELECT * FROM kiln_runs WHERE id = %s", (run_id,), one=True)

    def list_runs(self, limit: int = 50) -> list[dict]:
        return self._q("SELECT * FROM kiln_runs ORDER BY created_at DESC LIMIT %s", (limit,), many=True)

    def active_run_ids(self) -> list[str]:
        rows = self._q("SELECT id FROM kiln_runs WHERE status NOT IN %s", (tuple(RunStatus.TERMINAL),), many=True)
        return [str(r["id"]) for r in rows]

    def transition_run(self, run_id: str, expected: str | Iterable[str], new: str, **fields) -> bool:
        exp = [expected] if isinstance(expected, str) else list(expected)
        sets, vals = self._set_clause({"status": new, **fields}, RUN_COLS)
        extra = ", finished_at = now()" if new in RunStatus.TERMINAL else ""
        n = self._q(f"UPDATE kiln_runs SET {sets}{extra} WHERE id = %s AND status = ANY(%s::text[])", (*vals, run_id, exp))
        return n == 1

    def update_run(self, run_id: str, **fields) -> None:
        sets, vals = self._set_clause(fields, RUN_COLS)
        self._q(f"UPDATE kiln_runs SET {sets} WHERE id = %s", (*vals, run_id))

    def reap_stale_runs(self, stale_seconds: int) -> list[dict]:
        """Runs presas em fases curtas (planning/starting/integrating) por worker morto."""
        return self._q("UPDATE kiln_runs SET status = 'failed', error = 'worker morreu durante ' || status, "
                       "finished_at = now(), updated_at = now() WHERE status IN ('planning','starting','integrating') "
                       "AND updated_at < now() - make_interval(secs => %s) RETURNING id", (stale_seconds,), many=True)

    # ------------------------------ tasks ------------------------------ #
    def create_tasks(self, run_id: str, specs: list[AgentSpec]) -> None:
        with self._conn() as c:
            with c.transaction():
                for s in specs:
                    c.execute(
                        "INSERT INTO kiln_tasks (run_id, agent, objective, owned_paths, depends_on, acceptance, branch) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id, agent) DO NOTHING",
                        (run_id, s.name, s.objective, s.owned_paths, s.depends_on, s.acceptance, f"kiln/{s.name}"))

    def get_tasks(self, run_id: str) -> list[dict]:
        return self._q("SELECT * FROM kiln_tasks WHERE run_id = %s ORDER BY created_at, agent", (run_id,), many=True)

    def get_task(self, task_id: str) -> dict | None:
        return self._q("SELECT * FROM kiln_tasks WHERE id = %s", (task_id,), one=True)

    def transition_task(self, task_id: str, expected: str | Iterable[str], new: str, **fields) -> bool:
        exp = [expected] if isinstance(expected, str) else list(expected)
        sets, vals = self._set_clause({"status": new, **fields}, TASK_COLS)
        n = self._q(f"UPDATE kiln_tasks SET {sets} WHERE id = %s AND status = ANY(%s::text[])", (*vals, task_id, exp))
        return n == 1

    def update_task(self, task_id: str, **fields) -> None:
        sets, vals = self._set_clause(fields, TASK_COLS)
        self._q(f"UPDATE kiln_tasks SET {sets} WHERE id = %s", (*vals, task_id))

    def touch_task(self, task_id: str) -> None:
        self._q("UPDATE kiln_tasks SET updated_at = now() WHERE id = %s", (task_id,))

    def reap_stale_tasks(self, stale_seconds: int, exclude_ids: Iterable[str] = ()) -> list[dict]:
        """Tarefas 'running/verifying' sem batimento: voltam para a fila (job morreu)."""
        ex = [str(x) for x in exclude_ids]
        rows = self._q(
            "UPDATE kiln_tasks SET status = CASE status WHEN 'verifying' THEN 'agent_done' ELSE 'resume' END, "
            "updated_at = now() WHERE status IN ('running','verifying') "
            "AND updated_at < now() - make_interval(secs => %s) AND NOT (id::text = ANY(%s::text[])) RETURNING id, run_id, agent",
            (stale_seconds, ex), many=True)
        return rows

    # ----------------------------- contracts --------------------------- #
    def save_contracts(self, run_id: str, files: dict[str, str]) -> None:
        with self._conn() as c:
            with c.transaction():
                for path, content in files.items():
                    c.execute(
                        "INSERT INTO kiln_contracts (run_id, path, version, sha256, content, approved_by) "
                        "VALUES (%s,%s,1,%s,%s,'plan') ON CONFLICT DO NOTHING",
                        (run_id, path, sha256_text(content), content))

    def active_contracts(self, run_id: str) -> dict[str, dict]:
        rows = self._q("SELECT path, version, sha256, content FROM kiln_contracts WHERE run_id = %s AND active",
                       (run_id,), many=True)
        return {r["path"]: r for r in rows}

    def supersede_contract(self, run_id: str, path: str, content: str, approved_by: str) -> int:
        with self._conn() as c:
            with c.transaction():
                row = c.execute("SELECT COALESCE(MAX(version),0) AS v FROM kiln_contracts WHERE run_id=%s AND path=%s",
                                (run_id, path)).fetchone()
                v = row["v"] + 1
                c.execute("UPDATE kiln_contracts SET active = false WHERE run_id=%s AND path=%s", (run_id, path))
                c.execute("INSERT INTO kiln_contracts (run_id, path, version, sha256, content, approved_by) "
                          "VALUES (%s,%s,%s,%s,%s,%s)", (run_id, path, v, sha256_text(content), content, approved_by))
        return v

    # ----------------------------- approvals --------------------------- #
    def create_approval(self, run_id: str, task_id: str | None, kind: str, request: dict) -> dict:
        return self._q("INSERT INTO kiln_approvals (run_id, task_id, kind, request) VALUES (%s,%s,%s,%s) RETURNING *",
                       (run_id, task_id, kind, self._j(request)), one=True)

    def get_approval(self, approval_id: str) -> dict | None:
        return self._q("SELECT * FROM kiln_approvals WHERE id = %s", (approval_id,), one=True)

    def list_approvals(self, status: str | None = None, run_id: str | None = None, limit: int = 100) -> list[dict]:
        return self._q("SELECT * FROM kiln_approvals WHERE (%s::text IS NULL OR status = %s) "
                       "AND (%s::uuid IS NULL OR run_id = %s) ORDER BY created_at DESC LIMIT %s",
                       (status, status, run_id, run_id, limit), many=True)

    def decide_approval(self, approval_id: str, approved: bool, by: str, note: str = "") -> dict | None:
        return self._q("UPDATE kiln_approvals SET status = %s, decided_by = %s, note = %s, decided_at = now() "
                       "WHERE id = %s AND status = 'pending' RETURNING *",
                       ("approved" if approved else "denied", by, note, approval_id), one=True)

    def latest_approval(self, run_id: str, kind: str) -> dict | None:
        return self._q("SELECT * FROM kiln_approvals WHERE run_id = %s AND kind = %s ORDER BY created_at DESC LIMIT 1",
                       (run_id, kind), one=True)

    def pending_approvals(self, run_id: str, kind: str | None = None) -> list[dict]:
        return self._q("SELECT * FROM kiln_approvals WHERE run_id = %s AND status = 'pending' "
                       "AND (%s::text IS NULL OR kind = %s)", (run_id, kind, kind), many=True)

    def claim_unapplied_contract_changes(self, run_id: str) -> list[dict]:
        """Marca como aplicadas (atomico) as mudancas de contrato aprovadas; devolve as reivindicadas."""
        return self._q("UPDATE kiln_approvals SET applied_at = now() WHERE run_id = %s AND kind = 'contract_change' "
                       "AND status = 'approved' AND applied_at IS NULL RETURNING *", (run_id,), many=True)

    def count_open_contract_work(self, run_id: str) -> int:
        row = self._q("SELECT count(*) AS n FROM kiln_approvals WHERE run_id = %s AND kind = 'contract_change' "
                      "AND (status = 'pending' OR (status = 'approved' AND applied_at IS NULL))", (run_id,), one=True)
        return int(row["n"])

    # ------------------------------ eventos ---------------------------- #
    def add_event(self, run_id: str, actor: str, type_: str, data: dict | None = None,
                  task_id: str | None = None) -> None:
        self._q("INSERT INTO kiln_events (run_id, task_id, actor, type, data) VALUES (%s,%s,%s,%s,%s)",
                (run_id, task_id, actor, type_, self._j(data or {})))

    def list_events(self, run_id: str, after_id: int = 0, limit: int = 200) -> list[dict]:
        return self._q("SELECT * FROM kiln_events WHERE run_id = %s AND id > %s ORDER BY id LIMIT %s",
                       (run_id, after_id, limit), many=True)

    # ------------------------------- uso ------------------------------- #
    def add_usage(self, run_id: str, task_id: str | None, kind: str, tin: int, tout: int, usd: float) -> None:
        self._q("INSERT INTO kiln_usage (run_id, task_id, kind, tokens_in, tokens_out, cost_usd) VALUES (%s,%s,%s,%s,%s,%s)",
                (run_id, task_id, kind, tin, tout, usd))

    def usage_totals(self, run_id: str) -> dict:
        r = self._q("SELECT COALESCE(SUM(tokens_in + tokens_out),0) AS tokens, COALESCE(SUM(cost_usd),0) AS usd "
                    "FROM kiln_usage WHERE run_id = %s", (run_id,), one=True)
        return {"tokens": int(r["tokens"]), "usd": float(r["usd"])}

    # ------------------------------ licoes ----------------------------- #
    def add_lesson(self, run_id: str, task_id: str | None, text: str) -> None:
        self._q("INSERT INTO kiln_lessons (run_id, task_id, text) VALUES (%s,%s,%s)", (run_id, task_id, text))

    def list_lessons(self, status: str | None = None, limit: int = 100) -> list[dict]:
        return self._q("SELECT * FROM kiln_lessons WHERE (%s::text IS NULL OR status = %s) "
                       "ORDER BY created_at DESC LIMIT %s", (status, status, limit), many=True)

    def decide_lesson(self, lesson_id: str, approved: bool, by: str) -> dict | None:
        return self._q("UPDATE kiln_lessons SET status = %s, decided_by = %s, decided_at = now() "
                       "WHERE id = %s AND status = 'proposed' RETURNING *",
                       ("approved" if approved else "rejected", by, lesson_id), one=True)

    def approved_lessons(self, limit: int = 10) -> list[str]:
        rows = self._q("SELECT text FROM kiln_lessons WHERE status = 'approved' ORDER BY decided_at DESC LIMIT %s",
                       (limit,), many=True)
        return [r["text"] for r in rows]
