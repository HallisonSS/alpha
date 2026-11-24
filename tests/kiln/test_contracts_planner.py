import json

from conftest_helpers import ROUTES, STACK, contract_files
from app.kiln.contracts import integrity_violations, parse_stack, validate_contract_files
from app.kiln.llm import LLMResult
from app.kiln.models import ContractFile
from app.kiln.planner import Planner, PlanningError, parse_plan, validate_plan

NAMES = ["api", "web"]


def good_plan_json(**over):
    d = {"summary": "s",
         "agents": [{"name": "api", "objective": "o", "owned_paths": ["api/"], "depends_on": [], "acceptance": ["npm test"]},
                    {"name": "web", "objective": "o", "owned_paths": ["web/"], "depends_on": ["api"], "acceptance": ["npm test"]}],
         "contracts": [{"path": "contracts/stack.json", "content": json.dumps(STACK)},
                       {"path": "contracts/routes.json", "content": ROUTES}]}  # objeto, nao string
    d.update(over)
    return json.dumps(d)


class FakeLLM:
    def __init__(self, *outs):
        self.outs, self.calls = list(outs), 0

    def chat(self, messages, json_mode=True):
        self.calls += 1
        return LLMResult(self.outs.pop(0), 10, 10, 0.0)


def test_validate_contracts_ok_and_errors():
    assert validate_contract_files(contract_files(), NAMES) == []
    errs = validate_contract_files([ContractFile("contracts/stack.json", "{bad")], NAMES)
    assert any("JSON invalido" in e for e in errs) and any("routes.json" in e for e in errs)
    bad = dict(STACK, agents={"api": {"cwd": "api", "test": ["rm", "-rf", "/"]}})
    errs = validate_contract_files([ContractFile("contracts/stack.json", json.dumps(bad)),
                                    ContractFile("contracts/routes.json", json.dumps(ROUTES))], NAMES)
    assert any("fora de" in e for e in errs) and any("web ausente" in e for e in errs)
    errs = validate_contract_files([ContractFile("src/x.json", "{}")] + contract_files(), NAMES)
    assert any("fora de contracts/" in e for e in errs)


def test_integrity_violations():
    fz = {"contracts/a.json": "1"}
    assert integrity_violations(fz, {"contracts/a.json": "1"}) == []
    assert integrity_violations(fz, {"contracts/a.json": "2"})
    assert integrity_violations(fz, {"contracts/a.json": None})


def test_parse_stack():
    s = parse_stack(json.dumps(STACK))
    assert s.agents["api"].test == ("npm", "test") and "mongoose" in s.forbidden_packages


def test_plan_validation():
    p = parse_plan(good_plan_json())
    assert validate_plan(p, 4) == []
    p = parse_plan(good_plan_json(agents=[
        {"name": "api", "objective": "o", "owned_paths": ["api/"], "acceptance": ["x"]},
        {"name": "web", "objective": "o", "owned_paths": ["api/src"], "acceptance": ["x"]}]))
    assert any("sobreposicao" in e for e in validate_plan(p, 4))
    p = parse_plan(good_plan_json(agents=[
        {"name": "api", "objective": "o", "owned_paths": ["api/"], "depends_on": ["web"], "acceptance": ["x"]},
        {"name": "web", "objective": "o", "owned_paths": ["web/"], "depends_on": ["api"], "acceptance": ["x"]}]))
    assert any("circulares" in e for e in validate_plan(p, 4))
    p = parse_plan(good_plan_json(agents=[
        {"name": "api", "objective": "o", "owned_paths": ["contracts/"], "acceptance": ["x"]}]))
    assert any("reservado" in e for e in validate_plan(p, 4))
    assert any("maximo" in e for e in validate_plan(parse_plan(good_plan_json()), 1))


def test_planner_repairs_then_succeeds_and_renders_todo():
    llm = FakeLLM("not json", "```json\n" + good_plan_json() + "\n```")
    plan = Planner(llm, max_agents=4).plan("meta")
    assert llm.calls == 2
    assert "tasks/api.md" in plan.agents[0].owned_paths
    assert "- [ ] npm test" in plan.todo_md and "Contratos congelados" in plan.todo_md


def test_planner_gives_up():
    llm = FakeLLM("x", "y", "z")
    try:
        Planner(llm, max_agents=4).plan("meta")
        assert False, "deveria falhar"
    except PlanningError as e:
        assert e.errors
