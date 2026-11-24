"""Tipos compartilhados (stdlib/dataclasses para o nucleo ser testavel sem deps)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class RunStatus:
    CREATED = "created"
    PLANNING = "planning"
    AWAITING_PLAN = "awaiting_plan_approval"
    STARTING = "starting"
    RUNNING = "running"
    INTEGRATING = "integrating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET = "budget_exceeded"
    TERMINAL = frozenset({"succeeded", "failed", "cancelled", "budget_exceeded"})


class TaskStatus:
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED_CONTRACT = "blocked_contract"
    RESUME = "resume"
    AGENT_DONE = "agent_done"
    VERIFYING = "verifying"
    MERGED = "merged"
    FAILED = "failed"


class ApprovalKind:
    PLAN = "plan"
    CONTRACT_CHANGE = "contract_change"
    CONFIG_CHANGE = "config_change"
    COMMAND = "command"


@dataclass
class AgentSpec:
    name: str
    objective: str
    owned_paths: list[str]
    depends_on: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentSpec":
        return cls(
            name=d["name"],
            objective=d.get("objective", ""),
            owned_paths=list(d.get("owned_paths", [])),
            depends_on=list(d.get("depends_on", [])),
            acceptance=list(d.get("acceptance", [])),
        )


@dataclass
class ContractFile:
    path: str
    content: str


@dataclass
class Plan:
    summary: str
    agents: list[AgentSpec]
    contracts: list[ContractFile]
    todo_md: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "agents": [a.to_dict() for a in self.agents],
            "contracts": [{"path": c.path, "content": c.content} for c in self.contracts],
            "todo_md": self.todo_md,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Plan":
        return cls(
            summary=d.get("summary", ""),
            agents=[AgentSpec.from_dict(a) for a in d.get("agents", [])],
            contracts=[ContractFile(c["path"], c["content"]) for c in d.get("contracts", [])],
            todo_md=d.get("todo_md", ""),
        )


ACTION_TYPES = (
    "write_file", "delete_file", "read_file", "list_files", "run",
    "propose_contract_change", "propose_lesson", "done",
)


@dataclass
class Action:
    type: str
    path: str = ""
    content: str = ""
    argv: list[str] = field(default_factory=list)
    reason: str = ""
    text: str = ""
    summary: str = ""
    approval_id: str = ""  # preenchido quando a acao aguarda aprovacao humana

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Action":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def fingerprint(self) -> str:
        return f"{self.type}|{self.path}|{' '.join(self.argv)}|{len(self.content)}"
