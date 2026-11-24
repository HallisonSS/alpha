"""Contratos congelados: stack, rotas, schema e tipos.

O planner propoe, um humano aprova (plano), e a partir dai os arquivos em
`contracts/` ficam somente leitura. Mudar um contrato exige aprovacao e gera
nova versao (kiln_contracts). O gate `contracts_frozen` compara hashes.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .models import ContractFile
from .scanners import normalize_rel

STACK_PATH = "contracts/stack.json"
ROUTES_PATH = "contracts/routes.json"
REQUIRED_CONTRACTS = (STACK_PATH, ROUTES_PATH)
ALLOWED_VERIFY_EXES = {"npm", "node", "python", "python3", "pytest", "pip"}
MAX_CONTRACT_BYTES = 128 * 1024
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AgentCommands:
    cwd: str = "."
    install: tuple[str, ...] = ()
    build: tuple[str, ...] = ()
    test: tuple[str, ...] = ()
    start: tuple[str, ...] = ()
    health_url: str = ""
    checks: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Stack:
    database: str = ""
    require_tests: bool = True
    forbidden_packages: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    agents: dict[str, AgentCommands] = field(default_factory=dict)
    integration_services: tuple[dict, ...] = ()
    integration_checks: tuple[dict, ...] = ()


def _argv(v: Any) -> tuple[str, ...]:
    return tuple(v) if isinstance(v, list) else ()


def parse_stack(content: str) -> Stack:
    d = json.loads(content)
    agents: dict[str, AgentCommands] = {}
    for name, c in (d.get("agents") or {}).items():
        agents[name] = AgentCommands(
            cwd=c.get("cwd", "."),
            install=_argv(c.get("install")),
            build=_argv(c.get("build")),
            test=_argv(c.get("test")),
            start=_argv(c.get("start")),
            health_url=c.get("health_url", ""),
            checks=tuple(c.get("checks", [])),
        )
    integ = d.get("integration") or {}
    return Stack(
        database=d.get("database", ""),
        require_tests=bool(d.get("require_tests", True)),
        forbidden_packages=tuple(d.get("forbidden_packages", [])),
        forbidden_patterns=tuple(d.get("forbidden_patterns", [])),
        agents=agents,
        integration_services=tuple(integ.get("services", [])),
        integration_checks=tuple(integ.get("checks", [])),
    )


def _cmd_errors(where: str, argv: Any) -> list[str]:
    if argv in (None, []):
        return []
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return [f"{where}: comando deve ser lista argv de strings"]
    exe = argv[0].rsplit("/", 1)[-1]
    if exe not in ALLOWED_VERIFY_EXES:
        return [f"{where}: executavel '{exe}' fora de {sorted(ALLOWED_VERIFY_EXES)}"]
    return []


def validate_contract_files(files: list[ContractFile], agent_names: list[str]) -> list[str]:
    """Retorna lista de erros (vazia = valido)."""
    errs: list[str] = []
    by_path: dict[str, str] = {}
    for f in files:
        p = normalize_rel(f.path)
        if p is None or not p.startswith("contracts/"):
            errs.append(f"contrato fora de contracts/: {f.path!r}")
            continue
        if p in by_path:
            errs.append(f"contrato duplicado: {p}")
        if not f.content.strip():
            errs.append(f"contrato vazio: {p}")
        if len(f.content.encode("utf-8")) > MAX_CONTRACT_BYTES:
            errs.append(f"contrato grande demais: {p}")
        if p.endswith(".json"):
            try:
                json.loads(f.content)
            except ValueError as e:
                errs.append(f"{p}: JSON invalido ({e})")
        by_path[p] = f.content
    for req in REQUIRED_CONTRACTS:
        if req not in by_path:
            errs.append(f"contrato obrigatorio ausente: {req}")
    if errs:
        return errs

    stack_d = json.loads(by_path[STACK_PATH])
    if not isinstance(stack_d.get("database", ""), str):
        errs.append("stack.database deve ser string")
    agents_cfg = stack_d.get("agents")
    if not isinstance(agents_cfg, dict) or not agents_cfg:
        errs.append("stack.agents deve mapear cada agente para seus comandos")
        agents_cfg = {}
    for name, c in agents_cfg.items():
        if name not in agent_names:
            errs.append(f"stack.agents.{name}: agente inexistente no plano")
            continue
        if not isinstance(c, dict):
            errs.append(f"stack.agents.{name}: objeto esperado")
            continue
        for key in ("install", "build", "test", "start"):
            errs += _cmd_errors(f"stack.agents.{name}.{key}", c.get(key))
        if stack_d.get("require_tests", True) and not c.get("test"):
            errs.append(f"stack.agents.{name}.test ausente (require_tests=true)")
    for name in agent_names:
        if name not in agents_cfg:
            errs.append(f"stack.agents.{name} ausente: todo agente precisa de comandos de verificacao")
    for key in ("forbidden_packages", "forbidden_patterns"):
        v = stack_d.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errs.append(f"stack.{key} deve ser lista de strings")
    for pat in stack_d.get("forbidden_patterns", []) if isinstance(stack_d.get("forbidden_patterns", []), list) else []:
        try:
            re.compile(pat)
        except re.error as e:
            errs.append(f"stack.forbidden_patterns invalido {pat!r}: {e}")

    routes_d = json.loads(by_path[ROUTES_PATH])
    routes = routes_d.get("routes") if isinstance(routes_d, dict) else None
    if not isinstance(routes, list) or not routes:
        errs.append("routes.routes deve ser lista nao vazia")
    else:
        for i, r in enumerate(routes):
            if not isinstance(r, dict) or not all(k in r for k in ("method", "path", "owner")):
                errs.append(f"routes[{i}] precisa de method, path e owner")
            elif r["owner"] not in agent_names:
                errs.append(f"routes[{i}].owner '{r['owner']}' nao e um agente do plano")
    return errs


def snapshot(files: dict[str, str]) -> dict[str, str]:
    """path -> sha256 do conteudo."""
    return {p: sha256_text(c) for p, c in files.items()}


def integrity_violations(frozen: dict[str, str], current: dict[str, str | None]) -> list[str]:
    out: list[str] = []
    for path, sha in frozen.items():
        cur = current.get(path)
        if cur is None:
            out.append(f"{path}: contrato removido")
        elif cur != sha:
            out.append(f"{path}: contrato alterado sem aprovacao")
    for path in current:
        if path.startswith("contracts/") and path not in frozen and current[path] is not None:
            out.append(f"{path}: contrato novo nao aprovado")
    return out


def valid_agent_name(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and name not in {"integration", "repo", "main"}
