"""Metricas Prometheus (no-op se prometheus_client faltar) e logs JSON para o Loki/Promtail."""
from __future__ import annotations

import json
import logging
import time

try:  # opcional: o nucleo roda sem a lib
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
except Exception:  # pragma: no cover
    Counter = Gauge = Histogram = None
    start_http_server = None


class _Noop:
    def labels(self, *a, **k):
        return self

    def inc(self, *a, **k): ...
    def set(self, *a, **k): ...
    def observe(self, *a, **k): ...


def _mk(kind, name, doc, labels=(), **kw):
    if kind is None:
        return _Noop()
    try:
        return kind(name, doc, labels, **kw)
    except ValueError:  # ja registrada (reimport em testes)
        return _Noop()


RUNS = _mk(Counter, "kiln_runs_total", "Runs por status final", ["status"])
TASKS = _mk(Counter, "kiln_tasks_total", "Transicoes de tarefa", ["agent", "status"])
POLICY = _mk(Counter, "kiln_policy_decisions_total", "Decisoes da policy", ["verdict", "rule"])
GATES = _mk(Counter, "kiln_gate_results_total", "Resultado por gate", ["gate", "result"])
TOKENS = _mk(Counter, "kiln_tokens_total", "Tokens consumidos", ["agent", "direction"])
COST = _mk(Counter, "kiln_cost_usd_total", "Custo estimado em USD", ["agent"])
BUDGET = _mk(Counter, "kiln_budget_exceeded_total", "Orcamento estourado", ["scope"])
APPROVALS = _mk(Counter, "kiln_approvals_total", "Aprovacoes criadas/decididas", ["kind", "state"])
STEP_SECONDS = _mk(Histogram, "kiln_agent_step_seconds", "Duracao de um passo do agente", ["agent"],
                   buckets=(1, 5, 15, 30, 60, 120, 300, 600))
VERIFY_SECONDS = _mk(Histogram, "kiln_verify_seconds", "Duracao da verificacao", ["agent"],
                     buckets=(10, 30, 60, 120, 300, 600, 1200))
INFLIGHT = _mk(Gauge, "kiln_jobs_inflight", "Jobs em execucao neste worker")
EVENTS_DEAD = _mk(Counter, "kiln_events_dead_total", "Eventos movidos para dead-letter")


def start_metrics_server(port: int) -> None:
    if start_http_server is not None and port > 0:
        start_http_server(port)


class JsonFormatter(logging.Formatter):
    """Uma linha JSON por log: o Promtail extrai run_id/agent/rule como labels/campos."""

    EXTRA = ("run_id", "task_id", "agent", "rule", "verdict", "gate")

    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
             "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        for k in self.EXTRA:
            if hasattr(record, k):
                d[k] = getattr(record, k)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
