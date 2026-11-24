import json

from conftest_helpers import STACK, good_ctx, mkdiff
from app.kiln.gates import (CmdOutcome, SmokeOutcome, evaluate_gates, parse_test_counts)


def failed_names(ctx):
    return {r.name for r in evaluate_gates(ctx).failures}


def test_parse_test_counts():
    assert parse_test_counts("Tests:       12 failed, 3 passed, 15 total").total == 15
    c = parse_test_counts("Tests:       2 skipped, 5 passed, 7 total")
    assert c.total == 5 and c.skipped == 2
    assert parse_test_counts(" Tests  2 failed | 3 passed (5)").failed == 2
    assert parse_test_counts("==== 1 failed, 3 passed, 2 skipped in 0.52s ====").total == 4
    assert parse_test_counts("3 passed in 0.02s").total == 3
    assert parse_test_counts("# tests 4\n# pass 3\n# fail 1\n").failed == 1
    assert parse_test_counts("  5 passing (20ms)\n  1 failing").total == 6
    assert parse_test_counts("No tests found, exiting with code 1").total == 0
    assert parse_test_counts("") is None and parse_test_counts("hello") is None


def test_happy_path_passes_everything():
    rep = evaluate_gates(good_ctx())
    assert rep.passed, rep.summary()
    assert rep.tests_after == 12


def test_tests_must_not_decrease_or_be_empty():
    assert "tests" in failed_names(good_ctx(test=CmdOutcome(True, 0, "Tests: 8 passed, 8 total"), tests_before=12))
    assert "tests" in failed_names(good_ctx(test=CmdOutcome(True, 0, "Tests: 0 total")))
    assert "tests" in failed_names(good_ctx(test=CmdOutcome(True, 0, "something unknown")))
    assert "tests" in failed_names(good_ctx(test=CmdOutcome(True, 1, "Tests: 1 failed, 2 passed, 3 total")))
    assert "tests" in failed_names(good_ctx(test_configured=False))


def test_build_install_smoke_failures():
    assert "build" in failed_names(good_ctx(build=CmdOutcome(True, 1, "error TS2339")))
    assert "install" in failed_names(good_ctx(install=CmdOutcome(True, 1, "ERESOLVE")))
    assert "smoke" in failed_names(good_ctx(smoke=SmokeOutcome(True, False, "curl: (7) Failed to connect")))
    assert "smoke" in failed_names(good_ctx(smoke=SmokeOutcome(False, False, "")))


def test_ownership_gate():
    d = mkdiff("web/src/a.ts", added=["x"])
    assert "ownership" in failed_names(good_ctx(diff=d))
    assert "ownership" not in failed_names(good_ctx(diff=mkdiff("api/src/a.ts", added=["x"])))


def test_contract_frozen_gate():
    cur = {"contracts/stack.json": "deadbeef", "contracts/routes.json": good_ctx().frozen_contracts["contracts/routes.json"]}
    assert "contracts_frozen" in failed_names(good_ctx(current_contracts=cur))
    cur2 = dict(good_ctx().current_contracts)
    cur2["contracts/stack.json"] = None
    assert "contracts_frozen" in failed_names(good_ctx(current_contracts=cur2))
    cur3 = dict(good_ctx().current_contracts)
    cur3["contracts/new.json"] = "abc"
    assert "contracts_frozen" in failed_names(good_ctx(current_contracts=cur3))


def test_stack_lock_gate():
    deps = {"api/package.json": json.dumps({"dependencies": {"express": "4", "mongoose": "8"}})}
    assert "stack_lock" in failed_names(good_ctx(dependency_files=deps))
    assert "stack_lock" in failed_names(good_ctx(dependency_files={"api/requirements.txt": "fastapi\nmongodb==1\n"}))
    d = mkdiff("api/src/db.ts", added=['const url = "mongodb://127.0.0.1:27017/app"'])
    assert "stack_lock" in failed_names(good_ctx(diff=d))


def test_secret_and_destructive_gates():
    d = mkdiff("api/src/c.ts", added=['const JWT_SECRET = "a8f3k2m9x1q7w5z0p4v6"'])
    assert "secrets" in failed_names(good_ctx(diff=d))
    assert "secrets" in failed_names(good_ctx(diff=mkdiff("api/.env", status="added", added=["A=1"])))
    d2 = mkdiff("api/scripts/reset.sh", added=["psql -c 'DROP DATABASE app'"])
    assert "destructive" in failed_names(good_ctx(diff=d2))


def test_test_integrity_gate():
    d = mkdiff("api/tests/a.test.ts", status="deleted", removed=["it('a', () => {})"])
    assert "test_integrity" in failed_names(good_ctx(diff=d))
    d = mkdiff("api/tests/a.test.ts", added=["it.skip('a', () => {})"])
    assert "test_integrity" in failed_names(good_ctx(diff=d))
    d = mkdiff("api/tests/a.test.ts", added=["it('b', () => {})"], removed=["it('a', () => {})", "it('c', () => {})"])
    assert "test_integrity" in failed_names(good_ctx(diff=d))
    d = mkdiff("api/tests/a.test.ts", added=["it('b', () => {})", "it('d', () => {})"], removed=["it('a', () => {})"])
    assert "test_integrity" not in failed_names(good_ctx(diff=d))


def test_suppression_and_config_gates():
    d = mkdiff("api/src/a.ts", added=["// @ts-ignore", "const x = y as any"])
    assert "suppressions" in failed_names(good_ctx(diff=d))
    assert "suppressions" not in failed_names(good_ctx(diff=d, max_new_suppressions=2))
    d = mkdiff("api/.eslintrc.json", added=['"no-undef": "off"'])
    assert "config_integrity" in failed_names(good_ctx(diff=d))
    assert "config_integrity" not in failed_names(good_ctx(diff=d, approved_paths={"api/.eslintrc.json"}))
    d = mkdiff("api/tsconfig.json", status="added", added=['{"strict": true}'])
    assert "config_integrity" not in failed_names(good_ctx(diff=d))


def test_broken_gate_fails_closed():
    def boom(ctx):
        raise RuntimeError("x")
    rep = evaluate_gates(good_ctx(), gates=(boom,))
    assert not rep.passed and "erro interno" in rep.results[0].detail
