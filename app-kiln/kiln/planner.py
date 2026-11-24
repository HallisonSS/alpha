"""Planner: meta -> plano (agentes por pasta) + contratos + todo.md.

O LLM propoe; a validacao e deterministica. Plano invalido volta ao LLM com a
lista de erros (ate 2 reparos). O todo.md e renderizado por nos, nao pelo LLM.
"""
from __future__ import annotations

import json
from typing import Callable

from .actions import ActionParseError, extract_json
from .contracts import valid_agent_name, validate_contract_files
from .llm import LLM
from .models import AgentSpec, ContractFile, Plan
from .scanners import normalize_rel

RESERVED_ROOTS = {"contracts", "tasks", ".git", ".kiln", "node_modules"}

PLANNER_SYSTEM = """Voce e o PLANNER do KILN. Transforme a meta em um plano de engenharia executavel
por agentes paralelos, cada um dono de pastas DISJUNTAS. Responda SOMENTE com JSON:
{
 "summary": "resumo em 2-3 frases",
 "agents": [{"name":"api","objective":"...","owned_paths":["api/"],"depends_on":[],"acceptance":["criterio verificavel"]}],
 "contracts": [{"path":"contracts/stack.json","content":"<json como string>"},
               {"path":"contracts/routes.json","content":"<json como string>"}]
}
REGRAS:
- Nomes de agente: minusculas (ex.: web, api, db, auth, security). Maximo {max_agents} agentes.
- owned_paths: pastas ou arquivos relativos, sem sobreposicao entre agentes. Nunca contracts/, tasks/, .git/.
- depends_on: so quando um agente precisa do CODIGO MERGEADO de outro. Prefira independencia via contratos.
- contracts/stack.json: {"database":"postgres","require_tests":true,"forbidden_packages":[...],
  "forbidden_patterns":["regex"], "agents":{"<nome>":{"cwd":"api","install":["npm","ci"],"build":["npm","run","build"],
  "test":["npm","test"],"start":["npm","start"],"health_url":"http://127.0.0.1:4000/health",
  "checks":[{"url":"http://127.0.0.1:4000/health","expect":[200]}]}},
  "integration":{"services":[{"cwd":"api","cmd":["npm","start"]}],"checks":[{"url":"http://127.0.0.1:4000/health","expect":[200]}]}}
  Todo agente precisa de comandos (argv; so npm/node/python/python3/pytest/pip). Fixe o banco e liste
  dependencias/padroes PROIBIDOS que contradigam a stack (ex.: banco postgres -> proibir mongoose/mongodb).
- contracts/routes.json: {"routes":[{"method":"GET","path":"/health","owner":"api","smoke":true}]}
- Contratos adicionais permitidos (ex.: contracts/schema.sql, contracts/types.json), sempre em contracts/.
- Criterios de aceitacao devem ser verificaveis por comando (build, teste, curl no health), nunca subjetivos.
"""


def render_todo(plan: Plan) -> str:
    lines = ["# KILN - tasks/todo.md", "", f"**Resumo:** {plan.summary}", ""]
    for a in plan.agents:
        dep = f" (depende de: {', '.join(a.depends_on)})" if a.depends_on else ""
        lines += [f"## {a.name}{dep}", f"Pastas: {', '.join(a.owned_paths)}", f"Objetivo: {a.objective}", ""]
        lines += [f"- [ ] {c}" for c in a.acceptance] or ["- [ ] (sem criterios)"]
        lines.append("")
    lines += ["## Contratos congelados", *[f"- `{c.path}`" for c in plan.contracts], ""]
    return "\n".join(lines)


def _overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _cycle(agents: list[AgentSpec]) -> bool:
    deps = {a.name: set(a.depends_on) for a in agents}
    resolved: set[str] = set()
    while deps:
        ready = [n for n, d in deps.items() if d <= resolved]
        if not ready:
            return True
        for n in ready:
            resolved.add(n)
            deps.pop(n)
    return False


def validate_plan(plan: Plan, max_agents: int) -> list[str]:
    errs: list[str] = []
    names = [a.name for a in plan.agents]
    if not plan.agents:
        return ["plano sem agentes"]
    if len(plan.agents) > max_agents:
        errs.append(f"maximo de {max_agents} agentes (veio {len(plan.agents)})")
    if len(set(names)) != len(names):
        errs.append("nomes de agente duplicados")
    for a in plan.agents:
        if not valid_agent_name(a.name):
            errs.append(f"nome de agente invalido: {a.name!r}")
        if not a.owned_paths:
            errs.append(f"{a.name}: sem owned_paths")
        if not a.acceptance:
            errs.append(f"{a.name}: sem criterios de aceitacao")
        for p in a.owned_paths:
            n = normalize_rel(p.rstrip("/") or p)
            if n is None or n.split("/")[0] in RESERVED_ROOTS:
                errs.append(f"{a.name}: owned_path invalido/reservado: {p!r}")
        for d in a.depends_on:
            if d not in names or d == a.name:
                errs.append(f"{a.name}: dependencia invalida: {d!r}")
    for i, a in enumerate(plan.agents):
        for b in plan.agents[i + 1:]:
            for pa in a.owned_paths:
                for pb in b.owned_paths:
                    if _overlap(pa, pb):
                        errs.append(f"sobreposicao de pastas: {a.name}:{pa} x {b.name}:{pb}")
    if not errs and _cycle(plan.agents):
        errs.append("dependencias circulares entre agentes")
    errs += validate_contract_files(plan.contracts, names)
    return errs


def finalize_plan(plan: Plan) -> Plan:
    """Adiciona o arquivo de progresso de cada agente (tasks/<nome>.md) e renderiza o todo."""
    for a in plan.agents:
        prog = f"tasks/{a.name}.md"
        if prog not in a.owned_paths:
            a.owned_paths.append(prog)
    plan.todo_md = render_todo(plan)
    return plan


def parse_plan(text: str) -> Plan:
    d = extract_json(text)
    agents = []
    for a in d.get("agents", []):
        if not isinstance(a, dict) or "name" not in a:
            raise ActionParseError("agents[] precisa de objetos com 'name'")
        agents.append(AgentSpec.from_dict(a))
    contracts = []
    for c in d.get("contracts", []):
        if not isinstance(c, dict) or "path" not in c or "content" not in c:
            raise ActionParseError("contracts[] precisa de 'path' e 'content'")
        content = c["content"]
        if not isinstance(content, str):
            content = json.dumps(content, indent=2, ensure_ascii=False)
        contracts.append(ContractFile(c["path"], content))
    return Plan(summary=str(d.get("summary", "")), agents=agents, contracts=contracts)


class PlanningError(RuntimeError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


class Planner:
    def __init__(self, llm: LLM, max_agents: int = 4, charge: Callable | None = None,
                 max_repairs: int = 2):
        self.llm, self.max_agents, self.charge, self.max_repairs = llm, max_agents, charge, max_repairs

    def plan(self, goal: str, lessons: list[str] | None = None) -> Plan:
        system = PLANNER_SYSTEM.replace("{max_agents}", str(self.max_agents))
        if lessons:
            system += "\nLICOES APROVADAS (respeite):\n" + "\n".join(f"- {x}" for x in lessons[:10])
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": f"META:\n{goal}"}]
        errors: list[str] = []
        for _ in range(self.max_repairs + 1):
            res = self.llm.chat(messages)
            if self.charge:
                self.charge(res, "planner")
            try:
                plan = parse_plan(res.text)
                errors = validate_plan(plan, self.max_agents)
            except ActionParseError as e:
                plan, errors = None, [str(e)]
            if not errors and plan is not None:
                return finalize_plan(plan)
            messages += [{"role": "assistant", "content": res.text},
                         {"role": "user", "content": "Plano REPROVADO. Corrija e responda o JSON completo:\n- "
                          + "\n- ".join(errors[:12])}]
        raise PlanningError(errors)
