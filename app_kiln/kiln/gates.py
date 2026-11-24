"""Gates objetivos: a definicao de "pronto".

Cada gate e uma funcao pura GateContext -> GateResult. Nada aqui chama LLM:
"pronto" nao e opiniao do agente, e o resultado destes gates.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from .contracts import integrity_violations
from .scanners import (
    CONFIG_WEAKENING,
    DESTRUCTIVE_CODE,
    SKIP_PATTERNS,
    SUPPRESSION_PATTERNS,
    TEST_DECL,
    find_secrets,
    glob_match,
    is_config_path,
    is_env_secret_path,
    is_test_path,
    parse_unified_diff,
    path_in,
)


@dataclass
class CmdOutcome:
    ran: bool = False
    rc: int | None = None
    output: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.ran and self.rc == 0 and not self.timed_out


@dataclass
class SmokeOutcome:
    ran: bool = False
    ok: bool = False
    detail: str = ""


@dataclass
class TestCounts:
    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:  # pulados NAO contam como executados
        return self.passed + self.failed


@dataclass
class GateContext:
    agent: str
    owned_paths: list[str]
    diff: str = ""
    install: CmdOutcome = field(default_factory=CmdOutcome)
    build: CmdOutcome = field(default_factory=CmdOutcome)
    test: CmdOutcome = field(default_factory=CmdOutcome)
    smoke: SmokeOutcome = field(default_factory=SmokeOutcome)
    build_configured: bool = False
    test_configured: bool = False
    smoke_configured: bool = False
    require_tests: bool = True
    tests_before: int = 0
    frozen_contracts: dict[str, str] = field(default_factory=dict)
    current_contracts: dict[str, str | None] = field(default_factory=dict)
    approved_paths: set[str] = field(default_factory=set)
    dependency_files: dict[str, str] = field(default_factory=dict)
    forbidden_packages: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    max_new_suppressions: int = 0
    extra_owned: tuple[str, ...] = ()


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail,
                "evidence": self.evidence[:10]}


@dataclass
class GateReport:
    results: list[GateResult]
    tests_after: int | None = None

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        if self.passed:
            return "todos os gates passaram"
        return "\n".join(f"- [{r.name}] {r.detail}" + "".join(f"\n    * {e}" for e in r.evidence[:5])
                         for r in self.failures)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "tests_after": self.tests_after,
                "results": [r.to_dict() for r in self.results]}


# --------------------------------------------------------------------------- #
# Contagem de testes
# --------------------------------------------------------------------------- #

_JEST = re.compile(r"^\s*Tests:\s+(?P<body>.+?)\s*$", re.M)
_VITEST = re.compile(r"^\s*Tests\s+(?P<body>\d.*?)\s*$", re.M)
_PYTEST = re.compile(r"^=+\s*(?P<body>.+?)\s+in\s+[\d.]+s.*?=*\s*$", re.M)
_PYTEST_PLAIN = re.compile(r"^(?P<body>(?:\d+\s+\w+(?:,\s*)?)+)\s+in\s+[\d.]+s", re.M)
_ITEM = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|todo|pending)", re.I)


def _count_items(body: str) -> TestCounts:
    c = TestCounts()
    for n, kind in _ITEM.findall(body):
        n, kind = int(n), kind.lower()
        if kind in ("passed", "xpassed"):
            c.passed += n
        elif kind in ("failed", "error", "errors"):
            c.failed += n
        else:
            c.skipped += n
    return c


def parse_test_counts(output: str) -> TestCounts | None:
    """Reconhece jest, vitest, pytest, node:test (TAP) e mocha. None = nao reconhecido."""
    if not output:
        return None
    out = output
    m = list(_JEST.finditer(out))
    if m:
        return _count_items(m[-1].group("body"))
    m = list(_VITEST.finditer(out))
    if m:
        return _count_items(m[-1].group("body"))
    m = list(_PYTEST.finditer(out)) or list(_PYTEST_PLAIN.finditer(out))
    if m:
        return _count_items(m[-1].group("body"))
    tap = {k: re.findall(rf"^#\s*{k}\s+(\d+)", out, re.M) for k in ("pass", "fail", "skipped")}
    if tap["pass"] or tap["fail"]:
        return TestCounts(int(tap["pass"][-1]) if tap["pass"] else 0,
                          int(tap["fail"][-1]) if tap["fail"] else 0,
                          int(tap["skipped"][-1]) if tap["skipped"] else 0)
    passing = re.findall(r"(\d+)\s+passing", out)
    failing = re.findall(r"(\d+)\s+failing", out)
    if passing or failing:
        return TestCounts(int(passing[-1]) if passing else 0, int(failing[-1]) if failing else 0)
    if re.search(r"no tests (?:ran|found)|no test files found", out, re.I):
        return TestCounts()
    return None


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


def _tail(text: str, n: int = 3) -> list[str]:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-n:]


def g_install(ctx: GateContext) -> GateResult:
    if not ctx.install.ran:
        return GateResult("install", True, "sem passo de instalacao")
    ok = ctx.install.ok
    return GateResult("install", ok, "ok" if ok else "instalacao falhou", [] if ok else _tail(ctx.install.output))


def g_build(ctx: GateContext) -> GateResult:
    if not ctx.build_configured:
        return GateResult("build", True, "build nao configurado")
    ok = ctx.build.ok
    return GateResult("build", ok, "build ok" if ok else f"build falhou (rc={ctx.build.rc})",
                      [] if ok else _tail(ctx.build.output))


def g_tests(ctx: GateContext) -> GateResult:
    name = "tests"
    if not ctx.test_configured:
        if ctx.require_tests:
            return GateResult(name, False, "nenhum comando de teste configurado (require_tests=true)")
        return GateResult(name, True, "testes nao exigidos")
    if not ctx.test.ok:
        return GateResult(name, False, f"testes falharam (rc={ctx.test.rc})", _tail(ctx.test.output))
    counts = parse_test_counts(ctx.test.output)
    total = counts.total if counts else 0
    if counts is None and ctx.require_tests:
        return GateResult(name, False, "saida de teste nao reconhecida: nao e possivel contar testes",
                          _tail(ctx.test.output))
    if counts and counts.failed:
        return GateResult(name, False, f"{counts.failed} teste(s) falhando")
    if ctx.require_tests and total == 0:
        return GateResult(name, False, "0 testes executados (suite vazia nao e prova de nada)")
    if total < ctx.tests_before:
        return GateResult(name, False, f"numero de testes caiu de {ctx.tests_before} para {total}")
    return GateResult(name, True, f"{total} testes passando (antes: {ctx.tests_before})")


def tests_total(ctx: GateContext) -> int | None:
    c = parse_test_counts(ctx.test.output) if ctx.test.ran else None
    return c.total if c else None


def g_test_integrity(ctx: GateContext) -> GateResult:
    files = parse_unified_diff(ctx.diff)
    deleted = [f.path for f in files if f.status == "deleted" and is_test_path(f.path)]
    renamed_away = [f.old_path for f in files if f.status == "renamed" and f.old_path
                    and is_test_path(f.old_path) and not is_test_path(f.path)]
    skips: list[str] = []
    removed_decl = added_decl = 0
    for f in files:
        if not is_test_path(f.path) and f.status != "deleted":
            continue
        for ln in f.added:
            if any(p.search(ln) for p in SKIP_PATTERNS):
                skips.append(f"{f.path}: {ln.strip()[:80]}")
            if TEST_DECL.search(ln):
                added_decl += 1
        for ln in f.removed:
            if TEST_DECL.search(ln):
                removed_decl += 1
    problems = [f"teste removido: {p}" for p in deleted + renamed_away]
    problems += [f"teste pulado/focado: {s}" for s in skips]
    if removed_decl > added_decl and not deleted:
        problems.append(f"remocao liquida de {removed_decl - added_decl} caso(s) de teste")
    if problems:
        return GateResult("test_integrity", False, "testes foram removidos, pulados ou enfraquecidos", problems)
    return GateResult("test_integrity", True, "nenhum teste removido/pulado")


def g_suppressions(ctx: GateContext) -> GateResult:
    hits: list[str] = []
    for f in parse_unified_diff(ctx.diff):
        if f.status == "deleted":
            continue
        for ln in f.added:
            if any(p.search(ln) for p in SUPPRESSION_PATTERNS):
                hits.append(f"{f.path}: {ln.strip()[:80]}")
    if len(hits) > ctx.max_new_suppressions:
        return GateResult("suppressions", False,
                          f"{len(hits)} supressao(oes) nova(s) de lint/tipos (limite {ctx.max_new_suppressions})", hits)
    return GateResult("suppressions", True, "sem supressoes novas")


def g_config_integrity(ctx: GateContext) -> GateResult:
    problems: list[str] = []
    for f in parse_unified_diff(ctx.diff):
        if not is_config_path(f.path):
            continue
        approved = f.path in ctx.approved_paths
        if f.status == "added":
            continue  # criar config nova e permitido
        if not approved:
            problems.append(f"{f.path}: config alterada sem aprovacao")
            continue
        for ln in f.added:
            if any(p.search(ln) for p in CONFIG_WEAKENING):
                pass  # aprovado por humano: nao reprova
    if problems:
        return GateResult("config_integrity", False, "configs de qualidade/infra alteradas", problems)
    return GateResult("config_integrity", True, "configs intactas")


def g_contracts(ctx: GateContext) -> GateResult:
    v = integrity_violations(ctx.frozen_contracts, ctx.current_contracts)
    if v:
        return GateResult("contracts_frozen", False, "contratos divergem do congelado", v)
    return GateResult("contracts_frozen", True, "contratos identicos aos congelados")


def _deps_in(path: str, content: str) -> set[str]:
    base = path.rsplit("/", 1)[-1]
    names: set[str] = set()
    if base == "package.json":
        try:
            d = json.loads(content)
        except ValueError:
            return names
        for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            names |= {k.lower() for k in (d.get(key) or {})}
    elif base.startswith("requirements") and base.endswith(".txt"):
        for ln in content.splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ln and not ln.startswith("-"):
                names.add(re.split(r"[=<>~!\[ ;]", ln, maxsplit=1)[0].lower())
    elif base == "pyproject.toml":
        names |= {m.lower() for m in re.findall(r'^\s*"?([A-Za-z0-9_.\-]+)"?\s*[=<>~!]', content, re.M)}
    return names


def g_stack_lock(ctx: GateContext) -> GateResult:
    forb = {p.lower() for p in ctx.forbidden_packages}
    bad: list[str] = []
    for path, content in ctx.dependency_files.items():
        hit = _deps_in(path, content) & forb
        bad += [f"{path}: dependencia proibida '{h}'" for h in sorted(hit)]
    pats = [re.compile(p) for p in ctx.forbidden_patterns]
    for f in parse_unified_diff(ctx.diff):
        for ln in f.added:
            for p in pats:
                if p.search(ln):
                    bad.append(f"{f.path}: padrao proibido /{p.pattern}/ -> {ln.strip()[:60]}")
    if bad:
        return GateResult("stack_lock", False, "stack divergiu do contrato (ex.: troca de banco)", bad)
    return GateResult("stack_lock", True, "stack fiel ao contrato")


def g_secrets(ctx: GateContext) -> GateResult:
    hits: list[str] = []
    for f in parse_unified_diff(ctx.diff):
        if f.status == "deleted":
            continue
        if is_env_secret_path(f.path):
            hits.append(f"{f.path}: arquivo .env no diff")
            continue
        for s in find_secrets("\n".join(f.added)):
            hits.append(f"{f.path}: {s}")
    if hits:
        return GateResult("secrets", False, "segredo literal no diff", hits)
    return GateResult("secrets", True, "nenhum segredo no diff")


def g_destructive(ctx: GateContext) -> GateResult:
    hits: list[str] = []
    for f in parse_unified_diff(ctx.diff):
        for ln in f.added:
            if DESTRUCTIVE_CODE.search(ln):
                hits.append(f"{f.path}: {ln.strip()[:80]}")
    if hits:
        return GateResult("destructive", False, "operacao destrutiva introduzida no codigo", hits)
    return GateResult("destructive", True, "nada destrutivo no diff")


def g_ownership(ctx: GateContext) -> GateResult:
    owned = list(ctx.owned_paths) + list(ctx.extra_owned)
    out = [f.path for f in parse_unified_diff(ctx.diff) if not path_in(f.path, owned)]
    out += [f.old_path for f in parse_unified_diff(ctx.diff)
            if f.old_path and not path_in(f.old_path, owned)]
    if out:
        return GateResult("ownership", False, "arquivos alterados fora das pastas do agente", sorted(set(out)))
    return GateResult("ownership", True, "alteracoes dentro das pastas do agente")


def g_smoke(ctx: GateContext) -> GateResult:
    if not ctx.smoke_configured:
        return GateResult("smoke", True, "smoke nao configurado para este agente")
    if not ctx.smoke.ran:
        return GateResult("smoke", False, "smoke nao executou")
    if not ctx.smoke.ok:
        return GateResult("smoke", False, "smoke test falhou (servico nao respondeu como esperado)",
                          _tail(ctx.smoke.detail, 4))
    return GateResult("smoke", True, "servico respondeu aos checks")


DEFAULT_GATES: tuple[Callable[[GateContext], GateResult], ...] = (
    g_install, g_build, g_tests, g_test_integrity, g_suppressions, g_config_integrity,
    g_contracts, g_stack_lock, g_secrets, g_destructive, g_ownership, g_smoke,
)


def evaluate_gates(ctx: GateContext, gates=DEFAULT_GATES) -> GateReport:
    results: list[GateResult] = []
    for g in gates:
        try:
            results.append(g(ctx))
        except Exception as e:  # um gate quebrado REPROVA (fail closed)
            results.append(GateResult(g.__name__.removeprefix("g_"), False, f"erro interno do gate: {e!r}"))
    return GateReport(results=results, tests_after=tests_total(ctx))
