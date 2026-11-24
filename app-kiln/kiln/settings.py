"""Configuracao via variaveis de ambiente (um unico .env, como no resto do projeto)."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name, str(default)))
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name, str(default)))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    return _s(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_dsn: str
    redis_url: str
    workspace_root: str
    workspace_volume: str
    sandbox_image: str
    egress_network: str
    egress_proxy: str
    sandbox_mem: str
    sandbox_cpus: float
    sandbox_pids: int
    sandbox_scratch: str
    cmd_timeout: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout: int
    llm_max_output: int
    price_in: float   # USD por 1M tokens de entrada
    price_out: float  # USD por 1M tokens de saida
    max_parallel: int
    max_agents: int
    max_task_attempts: int
    budget_tokens: int
    budget_usd: float
    budget_seconds: int
    budget_iterations: int
    budget_commands: int
    auto_approve_plan: bool
    llm_review: bool
    admin_token: str
    metrics_port: int
    worker_id: str
    stale_seconds: int
    tick_seconds: int
    stream_events: str
    stream_approvals: str
    stream_dead: str
    group: str
    schema_path: str
    auto_migrate: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_dsn=_s("DB_DSN") or _s("DATABASE_URL"),
            redis_url=_s("REDIS_URL", "redis://redis:6379/0"),
            workspace_root=_s("KILN_WORKSPACE_ROOT", "/kiln/workspaces"),
            workspace_volume=_s("KILN_WORKSPACE_VOLUME", "kiln_workspaces"),
            sandbox_image=_s("KILN_SANDBOX_IMAGE", "kiln-sandbox:latest"),
            egress_network=_s("KILN_EGRESS_NETWORK", "kiln_egress"),
            egress_proxy=_s("KILN_EGRESS_PROXY"),
            sandbox_mem=_s("KILN_SANDBOX_MEM", "1g"),
            sandbox_cpus=_f("KILN_SANDBOX_CPUS", 1.0),
            sandbox_pids=_i("KILN_SANDBOX_PIDS", 256),
            sandbox_scratch=_s("KILN_SANDBOX_SCRATCH", "1g"),
            cmd_timeout=_i("KILN_CMD_TIMEOUT", 300),
            llm_base_url=_s("KILN_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_api_key=_s("KILN_LLM_API_KEY") or _s("OPENAI_API_KEY"),
            llm_model=_s("KILN_LLM_MODEL"),
            llm_timeout=_i("KILN_LLM_TIMEOUT", 180),
            llm_max_output=_i("KILN_LLM_MAX_OUTPUT", 8000),
            price_in=_f("KILN_PRICE_IN_PER_M", 0.0),
            price_out=_f("KILN_PRICE_OUT_PER_M", 0.0),
            max_parallel=max(1, _i("KILN_MAX_PARALLEL", 2)),
            max_agents=max(1, _i("KILN_MAX_AGENTS", 4)),
            max_task_attempts=max(1, _i("KILN_MAX_TASK_ATTEMPTS", 3)),
            budget_tokens=_i("KILN_BUDGET_TOKENS", 2_000_000),
            budget_usd=_f("KILN_BUDGET_USD", 5.0),
            budget_seconds=_i("KILN_BUDGET_SECONDS", 3600),
            budget_iterations=_i("KILN_BUDGET_ITERATIONS", 25),
            budget_commands=_i("KILN_BUDGET_COMMANDS", 60),
            auto_approve_plan=_b("KILN_AUTO_APPROVE_PLAN", False),
            llm_review=_b("KILN_LLM_REVIEW", False),
            admin_token=_s("KILN_ADMIN_TOKEN"),
            metrics_port=_i("KILN_METRICS_PORT", 9108),
            worker_id=_s("KILN_WORKER_ID", socket.gethostname()),
            stale_seconds=_i("KILN_TASK_STALE_SECONDS", 900),
            tick_seconds=_i("KILN_TICK_SECONDS", 30),
            stream_events=_s("KILN_STREAM_EVENTS", "kiln:events"),
            stream_approvals=_s("KILN_STREAM_APPROVALS", "kiln:approvals"),
            stream_dead=_s("KILN_STREAM_DEAD", "kiln:dead"),
            group=_s("KILN_GROUP", "kiln_workers"),
            schema_path=_s("KILN_SCHEMA_PATH", "/kiln/kiln.sql"),
            auto_migrate=_b("KILN_AUTO_MIGRATE", True),
        )

    def require_runtime(self) -> None:
        missing = [n for n, v in (("DB_DSN", self.db_dsn), ("KILN_LLM_MODEL", self.llm_model),
                                  ("KILN_LLM_API_KEY", self.llm_api_key)) if not v]
        if missing and "localhost" not in self.llm_base_url and "11434" not in self.llm_base_url:
            raise RuntimeError("variaveis obrigatorias ausentes: " + ", ".join(missing))
        if not self.db_dsn:
            raise RuntimeError("DB_DSN ausente")
