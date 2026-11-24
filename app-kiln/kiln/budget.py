"""Orcamento: tokens, custo, tempo, iteracoes, comandos e deteccao de loop."""
from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass


class BudgetExceeded(Exception):
    def __init__(self, scope: str, detail: str):
        super().__init__(f"{scope}: {detail}")
        self.scope = scope
        self.detail = detail


@dataclass(frozen=True)
class Limits:
    tokens: int = 2_000_000
    usd: float = 5.0
    seconds: int = 3600
    iterations: int = 25
    commands: int = 60

    @classmethod
    def from_settings(cls, s, overrides: dict | None = None) -> "Limits":
        o = overrides or {}
        return cls(
            tokens=int(o.get("tokens", s.budget_tokens)),
            usd=float(o.get("usd", s.budget_usd)),
            seconds=int(o.get("seconds", s.budget_seconds)),
            iterations=int(o.get("iterations", s.budget_iterations)),
            commands=int(o.get("commands", s.budget_commands)),
        )


class LoopDetector:
    """Detecta o agente repetindo a mesma acao (sinal de loop/queima de tokens)."""

    def __init__(self, window: int = 8, threshold: int = 4):
        self.recent: deque[str] = deque(maxlen=window)
        self.threshold = threshold

    def seed(self, items) -> None:
        self.recent.extend(items)

    def observe(self, fingerprint: str) -> None:
        self.recent.append(fingerprint)
        if Counter(self.recent)[fingerprint] >= self.threshold:
            raise BudgetExceeded("loop", f"acao repetida {self.threshold}x: {fingerprint[:80]}")


class BudgetTracker:
    """Consulta o consumo acumulado da run no banco e aplica os limites.

    `usage_fn()` -> {"tokens": int, "usd": float}   (soma da run)
    `started_at` -> epoch da run; `iterations`/`commands` sao por tarefa.
    """

    def __init__(self, limits: Limits, usage_fn, started_at: float, *,
                 iterations: int = 0, commands: int = 0, cancelled_fn=None):
        self.limits = limits
        self.usage_fn = usage_fn
        self.started_at = started_at
        self.iterations = iterations
        self.commands = commands
        self.cancelled_fn = cancelled_fn

    def check(self) -> None:
        if self.cancelled_fn and self.cancelled_fn():
            raise BudgetExceeded("cancelled", "run cancelada")
        elapsed = time.time() - self.started_at
        if elapsed > self.limits.seconds:
            raise BudgetExceeded("time", f"{int(elapsed)}s > {self.limits.seconds}s")
        u = self.usage_fn()
        if u["tokens"] > self.limits.tokens:
            raise BudgetExceeded("tokens", f"{u['tokens']} > {self.limits.tokens}")
        if self.limits.usd > 0 and u["usd"] > self.limits.usd:
            raise BudgetExceeded("usd", f"US$ {u['usd']:.2f} > US$ {self.limits.usd:.2f}")

    def tick_iteration(self) -> None:
        self.iterations += 1
        if self.iterations > self.limits.iterations:
            raise BudgetExceeded("iterations", f"{self.iterations} > {self.limits.iterations}")

    def tick_command(self) -> None:
        self.commands += 1
        if self.commands > self.limits.commands:
            raise BudgetExceeded("commands", f"{self.commands} > {self.limits.commands}")
