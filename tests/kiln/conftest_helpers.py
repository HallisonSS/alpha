"""Helpers compartilhados pelos testes (sem dependencias externas)."""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "agent", "agent"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.kiln.contracts import sha256_text  # noqa: E402
from app.kiln.gates import CmdOutcome, GateContext, SmokeOutcome  # noqa: E402
from app.kiln.models import ContractFile  # noqa: E402
from app.kiln.policy import build_policy  # noqa: E402

STACK = {
    "database": "postgres",
    "require_tests": True,
    "forbidden_packages": ["mongoose", "mongodb", "@prisma/client-mongo"],
    "forbidden_patterns": ["mongodb://", "provider\\s*=\\s*\"mongodb\""],
    "agents": {
        "api": {"cwd": "api", "install": ["npm", "ci"], "build": ["npm", "run", "build"],
                "test": ["npm", "test"], "start": ["npm", "start"],
                "health_url": "http://127.0.0.1:4000/health",
                "checks": [{"url": "http://127.0.0.1:4000/health", "expect": [200]}]},
        "web": {"cwd": "web", "install": ["npm", "ci"], "build": ["npm", "run", "build"],
                "test": ["npm", "test"]},
    },
}
ROUTES = {"routes": [{"method": "GET", "path": "/health", "owner": "api", "smoke": True}]}


def contract_files() -> list[ContractFile]:
    return [ContractFile("contracts/stack.json", json.dumps(STACK)),
            ContractFile("contracts/routes.json", json.dumps(ROUTES))]


def frozen() -> dict[str, str]:
    return {c.path: sha256_text(c.content) for c in contract_files()}


def good_ctx(**kw) -> GateContext:
    base = dict(
        agent="api", owned_paths=["api/", "tasks/api.md"], diff="",
        install=CmdOutcome(True, 0, "added 10 packages"),
        build=CmdOutcome(True, 0, "built"),
        test=CmdOutcome(True, 0, "Tests:       12 passed, 12 total"),
        smoke=SmokeOutcome(True, True, "ok"),
        build_configured=True, test_configured=True, smoke_configured=True,
        tests_before=0, frozen_contracts=frozen(), current_contracts=dict(frozen()),
        forbidden_packages=tuple(STACK["forbidden_packages"]),
        forbidden_patterns=tuple(STACK["forbidden_patterns"]),
    )
    base.update(kw)
    return GateContext(**base)


def api_policy():
    return build_policy(["api/", "tasks/api.md"], STACK["forbidden_packages"])


def mkdiff(path: str, added=(), removed=(), status="modified", old=None) -> str:
    out = [f"diff --git a/{old or path} b/{path}"]
    if status == "added":
        out.append("new file mode 100644")
    if status == "deleted":
        out.append("deleted file mode 100644")
    if status == "renamed":
        out += [f"rename from {old}", f"rename to {path}"]
    out += [f"--- a/{path}", f"+++ b/{path}", "@@ -1,3 +1,3 @@"]
    out += [f"-{r}" for r in removed] + [f"+{a}" for a in added]
    return "\n".join(out) + "\n"
