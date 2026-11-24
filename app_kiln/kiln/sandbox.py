"""Sandbox descartavel: um container por comando, sem rede (exceto instalacao), sem privilegios.

O agente NUNCA executa no host. O worker fala com o Docker (de preferencia via
docker-socket-proxy) e monta so o volume de workspaces em /work.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("kiln.sandbox")
SANDBOX_UID = "10001:10001"
MAX_OUTPUT = 100_000


@dataclass
class ExecResult:
    rc: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()


class Sandbox:
    """Interface. Implementacoes: DockerSandbox (producao) e fakes nos testes."""

    def prepare(self) -> None: ...

    def exec(self, run_id: str, argv: list[str], *, workdir: str, network: str = "none",
             timeout: int = 300, env: dict | None = None) -> ExecResult:
        raise NotImplementedError


class DockerSandbox(Sandbox):
    def __init__(self, settings):
        import docker  # import tardio

        self.s = settings
        self.client = docker.from_env()

    def prepare(self) -> None:
        """Garante imagem e dono do volume (uid do sandbox)."""
        self.client.images.get(self.s.sandbox_image)
        self.client.containers.run(
            self.s.sandbox_image, ["chown", SANDBOX_UID, "/work"], user="0", remove=True, network_mode="none",
            volumes={self.s.workspace_volume: {"bind": "/work", "mode": "rw"}},
            cap_drop=["ALL"], cap_add=["CHOWN"], read_only=True, labels={"kiln": "prepare"})

    def exec(self, run_id, argv, *, workdir, network="none", timeout=300, env=None) -> ExecResult:
        import requests

        net = self.s.egress_network if network == "egress" else "none"
        environment = {"HOME": "/tmp", "npm_config_cache": "/tmp/.npm", "CI": "1",
                       "PATH": "/work/" + workdir + "/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                       "GIT_TERMINAL_PROMPT": "0", "KILN_RUN": str(run_id)}
        if network == "egress" and self.s.egress_proxy:  # proxy com allowlist de dominios (ver RUNBOOK)
            p = self.s.egress_proxy
            environment.update({"HTTP_PROXY": p, "HTTPS_PROXY": p, "http_proxy": p, "https_proxy": p,
                                "npm_config_proxy": p, "npm_config_https_proxy": p})
        environment.update({str(k): str(v) for k, v in (env or {}).items()})
        t0 = time.time()
        c = self.client.containers.run(
            self.s.sandbox_image, argv, detach=True, working_dir="/work/" + workdir.strip("/"),
            volumes={self.s.workspace_volume: {"bind": "/work", "mode": "rw"}},
            environment=environment, network_mode=net, user=SANDBOX_UID,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=True,
            tmpfs={"/tmp": "rw,size=512m,mode=1777", "/scratch": f"rw,size={self.s.sandbox_scratch},exec,mode=1777"},
            mem_limit=self.s.sandbox_mem, memswap_limit=self.s.sandbox_mem,
            nano_cpus=int(self.s.sandbox_cpus * 1e9), pids_limit=self.s.sandbox_pids,
            labels={"kiln": "exec", "kiln.run": str(run_id)}, init=True)
        timed_out, rc = False, -1
        try:
            try:
                rc = int(c.wait(timeout=timeout).get("StatusCode", -1))
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
                timed_out = True
                try:
                    c.kill()
                except Exception:
                    pass
            out = c.logs(stdout=True, stderr=False, tail=3000).decode("utf-8", "replace")[-MAX_OUTPUT:]
            err = c.logs(stdout=False, stderr=True, tail=1000).decode("utf-8", "replace")[-MAX_OUTPUT // 2:]
        finally:
            try:
                c.remove(force=True)
            except Exception:
                log.warning("falha ao remover container %s", getattr(c, "id", "?"))
        return ExecResult(rc=rc, stdout=out, stderr=err, timed_out=timed_out, seconds=time.time() - t0)
