"""Workspace de uma run: repo base + um git worktree por agente.

Layout no volume:  /work/<run>/repo            (branch main; so o orquestrador faz merge)
                   /work/<run>/wt/<agente>     (worktree, branch kiln/<agente>)
                   /work/<run>/verify/<nome>   (clone descartavel do verificador)
Operacoes git rodam em containers de sandbox (sem rede). Arquivos sao lidos e
escritos direto no volume pelo worker, com protecao contra symlink e '..'.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .sandbox import ExecResult, Sandbox

GITIGNORE = """node_modules/
dist/
build/
.next/
coverage/
__pycache__/
*.pyc
.pytest_cache/
.venv/
.env
.env.*
!.env.example
*.log
"""
GIT_PREFIX = ["git", "-c", "safe.directory=*", "-c", "user.name=kiln", "-c", "user.email=kiln@localhost",
              "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
MAX_READ = 40_000
MAX_DIFF = 2_000_000


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(self, settings, sandbox: Sandbox, run_id: str):
        self.s, self.sb, self.run_id = settings, sandbox, str(run_id)
        self.root = Path(settings.workspace_root) / self.run_id

    # ---------------------------- caminhos ----------------------------- #
    def wt_path(self, agent: str) -> Path:
        return self.root / "wt" / agent

    def _where(self, where: str) -> str:
        if where == "repo":
            return f"{self.run_id}/repo"
        if where.startswith("verify:"):
            return f"{self.run_id}/verify/{where[7:]}"
        return f"{self.run_id}/wt/{where}"

    def fs_path(self, where: str) -> Path:
        return Path(self.s.workspace_root) / self._where(where)

    def git(self, where: str, *args: str, timeout: int = 120) -> ExecResult:
        return self.sb.exec(self.run_id, [*GIT_PREFIX, *args], workdir=self._where(where),
                            network="none", timeout=timeout)

    def _safe(self, where: str, rel: str) -> Path:
        base = self.fs_path(where).resolve()
        cur = base
        for part in Path(rel).parts:
            cur = cur / part
            if cur.is_symlink():
                raise WorkspaceError(f"symlink bloqueado: {rel}")
        target = (base / rel).resolve()
        if target != base and base not in target.parents:
            raise WorkspaceError(f"caminho fora do workspace: {rel}")
        return target

    # ----------------------------- repo -------------------------------- #
    def init_repo(self, files: dict[str, str]) -> str:
        repo = self.root / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        (self.root / "wt").mkdir(exist_ok=True)
        (self.root / "verify").mkdir(exist_ok=True)
        for rel, content in {".gitignore": GITIGNORE, **files}.items():
            p = self._safe("repo", rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        for args in (["init", "-q", "-b", "main", "."], ["add", "-A"], ["commit", "-q", "-m", "kiln: baseline"]):
            r = self.git("repo", *args)
            if not r.ok:
                raise WorkspaceError(f"git {args[0]} falhou: {r.output[-300:]}")
        return self.git("repo", "rev-parse", "HEAD").stdout.strip()

    def ensure_worktree(self, agent: str) -> None:
        if (self.wt_path(agent) / ".git").exists():
            return
        r = self.git("repo", "worktree", "add", "-q", "-b", f"kiln/{agent}", f"../wt/{agent}", "main")
        if not r.ok and "already exists" in r.output:  # branch sobrou de um crash
            r = self.git("repo", "worktree", "add", "-q", f"../wt/{agent}", f"kiln/{agent}")
        if not r.ok:
            raise WorkspaceError(f"worktree {agent}: {r.output[-300:]}")

    def commit_all(self, agent: str, message: str) -> bool:
        if not self.git(agent, "add", "-A").ok:
            raise WorkspaceError("git add falhou")
        if self.git(agent, "diff", "--cached", "--quiet").rc == 0:
            return False
        r = self.git(agent, "commit", "-q", "-m", message)
        if not r.ok:
            raise WorkspaceError(f"git commit falhou: {r.output[-300:]}")
        return True

    def sync_from_main(self, agent: str) -> tuple[bool, str]:
        """Traz o main (contratos novos + codigo ja mergeado) para a branch do agente."""
        self.commit_all(agent, "kiln: wip")
        r = self.git(agent, "merge", "-q", "--no-edit", "main")
        if r.ok:
            return True, ""
        self.git(agent, "merge", "--abort")
        return False, r.output[-300:]

    def diff(self, agent: str) -> str:
        r = self.git(agent, "diff", "--no-color", "--no-ext-diff", "-M", "main...HEAD")
        return r.stdout[:MAX_DIFF]

    def merge_into_main(self, agent: str) -> tuple[bool, str]:
        r = self.git("repo", "merge", "-q", "--no-ff", "--no-edit", "-m", f"kiln: merge {agent}", f"kiln/{agent}")
        if r.ok:
            return True, ""
        self.git("repo", "merge", "--abort")  # nunca force, nunca estado sujo
        return False, r.output[-400:]

    def head_sha(self, where: str) -> str:
        return self.git(where, "rev-parse", "HEAD").stdout.strip()

    def fresh_clone(self, name: str, branch: str) -> str:
        """Clone limpo e descartavel (so conteudo commitado) para o verificador. Retorna `where`."""
        dest = self.root / "verify" / name
        shutil.rmtree(dest, ignore_errors=True)
        r = self.sb.exec(self.run_id, [*GIT_PREFIX, "clone", "-q", "--no-hardlinks", "--branch", branch,
                                       f"/work/{self.run_id}/repo", f"/work/{self.run_id}/verify/{name}"],
                         workdir=self.run_id, network="none", timeout=180)
        if not r.ok:
            raise WorkspaceError(f"clone de verificacao falhou: {r.output[-300:]}")
        return f"verify:{name}"

    def drop_clone(self, name: str) -> None:
        shutil.rmtree(self.root / "verify" / name, ignore_errors=True)

    # ------------------------- arquivos do agente ---------------------- #
    def exists(self, where: str, rel: str) -> bool:
        try:
            return self._safe(where, rel).exists()
        except WorkspaceError:
            return False

    def read(self, where: str, rel: str, cap: int = MAX_READ) -> str:
        p = self._safe(where, rel)
        if not p.is_file():
            raise WorkspaceError(f"nao e arquivo: {rel}")
        data = p.read_text(encoding="utf-8", errors="replace")
        return data if len(data) <= cap else data[:cap] + f"\n...[truncado, {len(data)} bytes]"

    def write(self, where: str, rel: str, content: str) -> None:
        p = self._safe(where, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def delete(self, where: str, rel: str) -> None:
        p = self._safe(where, rel)
        if p.is_dir():
            raise WorkspaceError("delete_file so remove arquivos")
        p.unlink(missing_ok=True)

    def list_files(self, where: str, rel: str = ".", limit: int = 200) -> list[str]:
        base = self._safe(where, rel) if rel not in (".", "") else self.fs_path(where).resolve()
        root = self.fs_path(where).resolve()
        out: list[str] = []
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in (".git", "node_modules", "__pycache__", ".next", "dist")]
            for fn in fns:
                out.append(str(Path(dp, fn).relative_to(root)))
                if len(out) >= limit:
                    return sorted(out)
        return sorted(out)

    def contract_hashes(self, where: str) -> dict[str, str | None]:
        from .contracts import sha256_text

        out: dict[str, str | None] = {}
        base = self.fs_path(where) / "contracts"
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    out[str(p.relative_to(self.fs_path(where)))] = sha256_text(p.read_text(encoding="utf-8", errors="replace"))
        return out

    def write_contracts(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            self.write("repo", rel, content)
        self.git("repo", "add", "-A")
        self.git("repo", "commit", "-q", "-m", "kiln: contract update")
