"""Verificador independente: so le e testa.

Trabalha num clone limpo (apenas o que foi commitado), nunca na worktree do
agente. Roda install/build/test/smoke no sandbox, monta o GateContext e
avalia os gates. Opcionalmente pede uma revisao consultiva ao LLM (nunca
aprova sozinho: uma objecao vira alerta no relatorio).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from . import observability as obs
from .actions import ActionParseError, extract_json
from .contracts import AgentCommands, Stack
from .gates import (CmdOutcome, GateContext, GateReport, SmokeOutcome, evaluate_gates,
                    parse_test_counts)
from .workspace import Workspace

log = logging.getLogger("kiln.verifier")
SMOKE_BIN = ["python3", "/opt/kiln/kiln_smoke.py"]


@dataclass
class VerifyReport:
    gates: GateReport
    review: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.gates.passed

    def to_dict(self) -> dict:
        return {"passed": self.passed, "gates": self.gates.to_dict(), "review": self.review,
                "seconds": round(self.seconds, 1)}


class Verifier:
    def __init__(self, settings, llm=None):
        self.s, self.llm = settings, llm

    # ------------------------------------------------------------------ #
    def _run(self, ws: Workspace, where: str, argv, cwd: str, network="none", timeout=None) -> CmdOutcome:
        if not argv:
            return CmdOutcome()
        wd = ws._where(where) + (f"/{cwd}" if cwd not in ("", ".") else "")
        r = ws.sb.exec(ws.run_id, list(argv), workdir=wd, network=network, timeout=timeout or self.s.cmd_timeout)
        return CmdOutcome(True, r.rc, r.output[-20_000:], r.timed_out)

    def _smoke(self, ws: Workspace, where: str, services: list[dict], checks: list[dict]) -> SmokeOutcome:
        spec = {"services": services, "checks": checks, "timeout": 90}
        r = ws.sb.exec(ws.run_id, [*SMOKE_BIN, "--root", ".", "--spec", json.dumps(spec)],
                       workdir=ws._where(where), network="none", timeout=180)
        line = (r.stdout.strip().splitlines() or [""])[-1]
        try:
            d = json.loads(line)
            return SmokeOutcome(True, bool(d.get("ok")) and r.rc == 0, d.get("detail") or line)
        except ValueError:
            return SmokeOutcome(True, False, f"saida do smoke ilegivel (rc={r.rc}): {r.output[-300:]}")

    @staticmethod
    def _read_deps(ws: Workspace, where: str, cwd: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in ("package.json", "requirements.txt", "pyproject.toml"):
            rel = name if cwd in ("", ".") else f"{cwd}/{name}"
            if ws.exists(where, rel):
                out[rel] = ws.read(where, rel, cap=200_000)
        return out

    @staticmethod
    def _smoke_spec(cfg: AgentCommands) -> tuple[list[dict], list[dict]]:
        checks = list(cfg.checks) or ([{"url": cfg.health_url, "expect": [200]}] if cfg.health_url else [])
        services = [{"cwd": cfg.cwd, "cmd": list(cfg.start)}] if cfg.start else []
        return services, checks

    # ------------------------------------------------------------------ #
    def verify_task(self, run: dict, task: dict, stack: Stack, frozen: dict[str, str],
                    approved_paths: set[str], ws: Workspace) -> VerifyReport:
        t0 = time.time()
        agent = task["agent"]
        cfg = stack.agents.get(agent, AgentCommands())
        diff = ws.diff(agent)
        name = f"{agent}-{int(t0)}"
        where = ws.fresh_clone(name, f"kiln/{agent}")
        try:
            install = self._run(ws, where, cfg.install, cfg.cwd, network="egress", timeout=600)
            ok_install = (not install.ran) or install.ok
            build = self._run(ws, where, cfg.build, cfg.cwd) if ok_install else CmdOutcome()
            test = self._run(ws, where, cfg.test, cfg.cwd) if ok_install else CmdOutcome()
            services, checks = self._smoke_spec(cfg)
            smoke_configured = bool(cfg.start and checks)
            build_ok = (not cfg.build) or build.ok
            smoke = (self._smoke(ws, where, services, checks)
                     if (smoke_configured and ok_install and build_ok) else SmokeOutcome())
            ctx = GateContext(
                agent=agent, owned_paths=list(task["owned_paths"]), diff=diff,
                install=install, build=build, test=test, smoke=smoke,
                build_configured=bool(cfg.build), test_configured=bool(cfg.test), smoke_configured=smoke_configured,
                require_tests=stack.require_tests, tests_before=int(task.get("baseline_tests") or 0),
                frozen_contracts=frozen, current_contracts=ws.contract_hashes(where),
                approved_paths=approved_paths, dependency_files=self._read_deps(ws, where, cfg.cwd),
                forbidden_packages=stack.forbidden_packages, forbidden_patterns=stack.forbidden_patterns)
        finally:
            ws.drop_clone(name)
        report = evaluate_gates(ctx)
        for r in report.results:
            obs.GATES.labels(r.name, "pass" if r.passed else "fail").inc()
        review = self._llm_review(task, diff, report) if (self.s.llm_review and self.llm and report.passed) else {}
        dt = time.time() - t0
        obs.VERIFY_SECONDS.labels(agent).observe(dt)
        return VerifyReport(report, review, dt)

    def verify_integration(self, run: dict, tasks: list[dict], stack: Stack, frozen: dict[str, str],
                           ws: Workspace) -> VerifyReport:
        """Gates na arvore mergeada (main): build/test de cada agente + smoke dos servicos juntos."""
        t0 = time.time()
        name = f"integration-{int(t0)}"
        where = ws.fresh_clone(name, "main")
        try:
            installs, builds, tests = [], [], []
            for t in tasks:
                cfg = stack.agents.get(t["agent"], AgentCommands())
                installs.append(self._run(ws, where, cfg.install, cfg.cwd, network="egress", timeout=600))
                builds.append(self._run(ws, where, cfg.build, cfg.cwd))
                tests.append(self._run(ws, where, cfg.test, cfg.cwd))
            checks = list(stack.integration_checks)
            services = list(stack.integration_services)
            smoke = self._smoke(ws, where, services, checks) if (services and checks) else SmokeOutcome()

            def merge(outs: list[CmdOutcome]) -> CmdOutcome:
                ran = [o for o in outs if o.ran]
                if not ran:
                    return CmdOutcome()
                return CmdOutcome(True, 0 if all(o.ok for o in ran) else 1, "\n".join(o.output for o in ran),
                                  any(o.timed_out for o in ran))

            t_out = merge(tests)
            counts = [parse_test_counts(o.output) for o in tests if o.ran]
            if counts and all(counts):
                t_out.output = f"Tests: {sum(c.failed for c in counts)} failed, {sum(c.passed for c in counts)} passed, {sum(c.total for c in counts)} total"
            deps: dict[str, str] = {}
            for t in tasks:
                deps.update(self._read_deps(ws, where, stack.agents.get(t["agent"], AgentCommands()).cwd))
            ctx = GateContext(
                agent="integration", owned_paths=["**"], diff="",
                install=merge(installs), build=merge(builds), test=t_out, smoke=smoke,
                build_configured=any(o.ran for o in builds), test_configured=any(o.ran for o in tests),
                smoke_configured=bool(services and checks), require_tests=stack.require_tests,
                tests_before=sum(int(t.get("baseline_tests") or 0) for t in tasks),
                frozen_contracts=frozen, current_contracts=ws.contract_hashes(where),
                dependency_files=deps, forbidden_packages=stack.forbidden_packages,
                forbidden_patterns=stack.forbidden_patterns)
        finally:
            ws.drop_clone(name)
        report = evaluate_gates(ctx)
        for r in report.results:
            obs.GATES.labels(r.name, "pass" if r.passed else "fail").inc()
        return VerifyReport(report, {}, time.time() - t0)

    # ------------------------------------------------------------------ #
    def _llm_review(self, task: dict, diff: str, report: GateReport) -> dict:
        """Revisao consultiva (staff engineer). Nao aprova nada; so levanta alertas."""
        try:
            res = self.llm.chat([
                {"role": "system", "content": 'Voce e um revisor senior. Responda JSON: {"approve":bool,"concerns":["..."]}. '
                 "Procure gambiarras, testes triviais, tratamento de erro ausente, seguranca e solucoes complicadas demais."},
                {"role": "user", "content": f"Objetivo: {task['objective']}\nDIFF:\n{diff[:30000]}"}])
            d = extract_json(res.text)
            return {"approve": bool(d.get("approve")), "concerns": [str(c)[:300] for c in d.get("concerns", [])][:8]}
        except (ActionParseError, Exception) as e:  # revisao nunca derruba a verificacao
            log.warning("llm_review falhou: %s", e)
            return {}
