"""Workspace com git REAL (sandbox local via subprocess). Valida init, worktree, sync, diff,
merge, clone limpo e protecoes de caminho, sem Docker."""
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

from conftest_helpers import contract_files
from app.kiln.gates import evaluate_gates
from app.kiln.sandbox import ExecResult, Sandbox
from app.kiln.scanners import parse_unified_diff
from app.kiln.workspace import Workspace, WorkspaceError


class LocalSandbox(Sandbox):
    def __init__(self, root):
        self.root = root

    def exec(self, run_id, argv, *, workdir, network="none", timeout=300, env=None):
        argv = [a.replace("/work/", self.root + "/") if isinstance(a, str) else a for a in argv]
        r = subprocess.run(argv, cwd=os.path.join(self.root, workdir), capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
        return ExecResult(r.returncode, r.stdout, r.stderr)


def make_ws():
    root = tempfile.mkdtemp(prefix="kiln-ws-")
    ws = Workspace(SimpleNamespace(workspace_root=root), LocalSandbox(root), "run1")
    files = {c.path: c.content for c in contract_files()}
    files.update({"tasks/todo.md": "# todo", "tasks/api.md": "# api"})
    ws.init_repo(files)
    return ws, root


def test_full_git_flow():
    ws, root = make_ws()
    try:
        ws.ensure_worktree("api")
        ws.ensure_worktree("web")
        ws.write("api", "api/src/a.ts", "export const a = 1\n")
        ws.write("api", "api/node_modules/x/index.js", "ignored")  # .gitignore
        assert ws.commit_all("api", "feat") is True
        assert ws.commit_all("api", "nada") is False
        diff = ws.diff("api")
        paths = [f.path for f in parse_unified_diff(diff)]
        assert paths == ["api/src/a.ts"]
        # ownership gate deveria passar para essa diff
        from conftest_helpers import good_ctx
        assert evaluate_gates(good_ctx(diff=diff)).passed
        # outro agente mexe so no seu espaco; merge sem conflito
        ws.write("web", "web/src/b.ts", "export const b = 2\n")
        ws.commit_all("web", "feat web")
        assert ws.merge_into_main("api") == (True, "")
        assert ws.merge_into_main("web") == (True, "")
        assert ws.exists("repo", "api/src/a.ts") and ws.exists("repo", "web/src/b.ts")
        # diff do api continua so com as mudancas dele apos sync com main (tres pontos)
        ok, err = ws.sync_from_main("api")
        assert ok, err
        assert [f.path for f in parse_unified_diff(ws.diff("api"))] == []  # ja mergeado: nada pendente
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_contract_update_propagates_via_sync_and_clone_is_clean():
    ws, root = make_ws()
    try:
        ws.ensure_worktree("api")
        ws.write("api", "api/x.ts", "x")
        ws.commit_all("api", "x")
        new = '{"routes": [{"method": "GET", "path": "/health", "owner": "api"}, {"method": "GET", "path": "/u", "owner": "api"}]}'
        ws.write_contracts({"contracts/routes.json": new})
        assert ws.contract_hashes("api")["contracts/routes.json"] != ws.contract_hashes("repo")["contracts/routes.json"]
        ok, err = ws.sync_from_main("api")
        assert ok, err
        assert ws.contract_hashes("api") == ws.contract_hashes("repo")
        assert [f.path for f in parse_unified_diff(ws.diff("api"))] == ["api/x.ts"]  # contrato nao vira "alteracao do agente"
        where = ws.fresh_clone("api-1", "kiln/api")
        assert ws.exists(where, "api/x.ts") and not ws.exists(where, "api/node_modules")
        ws.drop_clone("api-1")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_merge_conflict_is_aborted_not_forced():
    ws, root = make_ws()
    try:
        ws.ensure_worktree("api")
        ws.ensure_worktree("web")
        ws.write("api", "shared.txt", "api\n")
        ws.write("web", "shared.txt", "web\n")
        ws.commit_all("api", "a")
        ws.commit_all("web", "w")
        assert ws.merge_into_main("api")[0]
        ok, msg = ws.merge_into_main("web")
        assert not ok
        assert ws.git("repo", "status", "--porcelain").stdout.strip() == ""  # repo limpo (merge abortado)
        assert ws.read("repo", "shared.txt") == "api\n"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_path_guards():
    ws, root = make_ws()
    try:
        ws.ensure_worktree("api")
        for bad in ("../../etc/x", "/etc/passwd"):
            try:
                ws.write("api", bad, "x")
                assert False, bad
            except WorkspaceError:
                pass
        os.symlink("/etc", os.path.join(root, "run1", "wt", "api", "link"))
        for op in (lambda: ws.write("api", "link/passwd", "x"), lambda: ws.read("api", "link/passwd")):
            try:
                op()
                assert False, "symlink deveria ser bloqueado"
            except WorkspaceError:
                pass
        assert ws.exists("api", "link/passwd") is False
    finally:
        shutil.rmtree(root, ignore_errors=True)
