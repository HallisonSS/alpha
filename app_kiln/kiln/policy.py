"""Politica de comandos e escrita (deny/allow/approval) para agentes KILN.

Principios:
  * allowlist de executaveis; tudo fora dela e NEGADO;
  * comandos sao argv (lista), nunca string de shell; shells sao negados;
  * escrita so dentro das pastas do agente; contratos, .env, .git, CI sao somente leitura;
  * configs de qualidade (tsconfig, eslint...) exigem aprovacao humana para alterar;
  * testes nunca sao apagados; operacoes destrutivas (DROP DATABASE, push --force,
    audit fix --force, rm -rf fora de artefatos) sao negadas.

`KilnPolicy` pode ser composta com a politica ja existente do projeto
(`base_check`): ela e consultada primeiro e pode so endurecer o veredito.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

from .scanners import (
    DESTRUCTIVE_CMD,
    find_secrets,
    glob_match,
    is_config_path,
    is_env_secret_path,
    is_test_path,
    normalize_rel,
    path_in,
)


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL = "approval"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str
    reason: str
    network: str = "none"  # "none" | "egress" (so instalacao de dependencias)

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def __str__(self) -> str:
        return f"{self.verdict.value.upper()}[{self.rule}]: {self.reason}"


def allow(rule: str = "ok", reason: str = "permitido", network: str = "none") -> Decision:
    return Decision(Verdict.ALLOW, rule, reason, network)


def deny(rule: str, reason: str) -> Decision:
    return Decision(Verdict.DENY, rule, reason)


def need_approval(rule: str, reason: str) -> Decision:
    return Decision(Verdict.APPROVAL, rule, reason)


HARD_PROTECTED = (
    "contracts/**", ".git/**", ".kiln/**", "KILN_RULES.md", ".gitignore",
    "**/.github/**", "**/node_modules/**",
)

APPROVAL_GLOBS = (
    "**/tsconfig*.json", "**/.eslintrc*", "**/eslint.config.*", "**/jest.config.*",
    "**/vitest.config.*", "**/pytest.ini", "**/.prettierrc*", "**/Dockerfile*",
    "**/docker-compose*", "**/tox.ini", "**/setup.cfg", "**/mypy.ini",
)

SHELLS = {"sh", "bash", "zsh", "dash", "ash", "fish", "csh", "ksh", "env", "sudo", "su",
          "doas", "xargs", "eval", "nohup", "exec", "busybox"}
NETWORK_TOOLS = {"curl", "wget", "nc", "ncat", "ssh", "scp", "sftp", "ftp", "telnet", "rsync"}
READ_ONLY_TOOLS = {"ls", "cat", "head", "tail", "grep", "wc", "sort", "diff", "pwd", "true",
                   "which", "echo", "find", "tree"}
MUTATING_FS_TOOLS = {"mkdir", "touch", "mv", "cp", "rm"}
DEV_TOOLS = {"tsc", "eslint", "prettier", "jest", "vitest", "pytest", "ruff", "mypy", "node",
             "python", "python3"}
DEFAULT_ALLOW_EXEC = (
    {"npm", "pip", "pip3", "git"} | READ_ONLY_TOOLS | MUTATING_FS_TOOLS | DEV_TOOLS
)
SAFE_NPM_SCRIPTS = {"build", "test", "lint", "typecheck", "type-check", "start", "dev", "format",
                    "check", "test:unit", "test:int", "test:e2e", "migrate"}
ARTEFACT_DIRS = {"node_modules", "dist", "build", ".next", "coverage", "__pycache__",
                 ".pytest_cache", ".cache", ".turbo", "out", ".venv", "venv"}
INLINE_CODE_FLAGS = {"-e", "-p", "-c", "--eval", "--print"}

GIT_ALLOWED = {"status", "diff", "log", "show", "add", "commit", "rev-parse", "ls-files",
               "blame", "shortlog", "branch", "restore"}
GIT_BAD_FLAGS = {"--force", "-f", "--force-with-lease", "--hard", "--no-verify"}


@dataclass
class PolicyConfig:
    owned_paths: list[str]
    forbidden_packages: set[str] = field(default_factory=set)
    allow_exec: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOW_EXEC))
    max_write_bytes: int = 256 * 1024
    protected: tuple[str, ...] = HARD_PROTECTED
    approval_globs: tuple[str, ...] = APPROVAL_GLOBS


BaseCheck = Callable[[list[str]], "Decision | None"]


class KilnPolicy:
    def __init__(self, cfg: PolicyConfig, base_check: BaseCheck | None = None):
        self.cfg = cfg
        self.base_check = base_check

    # ------------------------------------------------------------------ #
    # Escrita / remocao de arquivos
    # ------------------------------------------------------------------ #
    def check_read(self, path: str) -> Decision:
        p = normalize_rel(path)
        if p is None:
            return deny("path.invalid", f"caminho invalido: {path!r}")
        if p.startswith(".git/") or p == ".git":
            return deny("path.protected", ".git nao pode ser lido pelo agente")
        if is_env_secret_path(p):
            return deny("secret.read", "arquivos .env nao podem ser lidos pelo agente")
        return allow("read.ok")

    def check_write(self, path: str, content: str = "", *, exists: bool = False,
                    approved: bool = False) -> Decision:
        p = normalize_rel(path)
        if p is None:
            return deny("path.invalid", f"caminho invalido: {path!r}")
        if is_env_secret_path(p):
            return deny("secret.env_file", "agente nao grava .env (use .env.example)")
        if path_in(p, self.cfg.protected):
            return deny("path.protected", f"{p} e somente leitura (contrato/infra do KILN)")
        if not path_in(p, self.cfg.owned_paths):
            return deny("path.not_owned", f"{p} fora das pastas do agente {self.cfg.owned_paths}")
        if len(content.encode("utf-8", "replace")) > self.cfg.max_write_bytes:
            return deny("write.too_large", f"arquivo maior que {self.cfg.max_write_bytes} bytes")
        leaks = find_secrets(content)
        if leaks:
            return deny("secret.detected", "segredo literal no conteudo: " + "; ".join(leaks[:3]))
        if exists and not approved and any(glob_match(p, g) for g in self.cfg.approval_globs):
            return need_approval("config.modify", f"alterar config de qualidade/infra existente: {p}")
        return allow("write.ok")

    def check_delete(self, path: str, *, approved: bool = False) -> Decision:
        p = normalize_rel(path)
        if p is None:
            return deny("path.invalid", f"caminho invalido: {path!r}")
        if path_in(p, self.cfg.protected):
            return deny("path.protected", f"{p} e somente leitura")
        if not path_in(p, self.cfg.owned_paths):
            return deny("path.not_owned", f"{p} fora das pastas do agente")
        if is_test_path(p):
            return deny("test.delete", f"apagar teste e proibido: {p} (conserte o codigo, nao o teste)")
        if not approved and any(glob_match(p, g) for g in self.cfg.approval_globs):
            return need_approval("config.delete", f"remover config: {p}")
        return allow("delete.ok")

    # ------------------------------------------------------------------ #
    # Comandos (argv)
    # ------------------------------------------------------------------ #
    def check_command(self, argv: object, *, approved: bool = False) -> Decision:
        d = self._check_command(argv, approved)
        if approved and d.verdict is Verdict.APPROVAL:  # humano ja aprovou; DENY continua DENY
            return allow(d.rule, "aprovado por humano", d.network)
        return d

    def _check_command(self, argv: object, approved: bool) -> Decision:
        if (not isinstance(argv, (list, tuple)) or not argv
                or not all(isinstance(a, str) and "\x00" not in a for a in argv)):
            return deny("exec.invalid", "comando deve ser uma lista argv de strings")
        argv = list(argv)
        if self.base_check is not None:
            base = self.base_check(argv)
            if base is not None and base.verdict is Verdict.DENY:
                return base
        joined = " ".join(argv)
        if DESTRUCTIVE_CMD.search(joined):
            return deny("sql.destructive", "operacao destrutiva (DROP/TRUNCATE/DELETE sem WHERE/FLUSH)")
        exe = posixpath.basename(argv[0])
        if exe in SHELLS:
            return deny("exec.shell", f"'{exe}' nao permitido; use argv direto")
        if exe in NETWORK_TOOLS:
            return deny("exec.network", f"'{exe}' proibido: o gate de smoke valida a rede")
        if exe not in self.cfg.allow_exec:
            return deny("exec.not_allowed", f"executavel fora da allowlist: {exe}")

        if exe == "git":
            return self._cmd_git(argv)
        if exe == "npm":
            return self._cmd_npm(argv)
        if exe in ("pip", "pip3"):
            return self._cmd_pip(argv)
        if exe in READ_ONLY_TOOLS:
            return self._cmd_readonly(exe, argv)
        if exe in MUTATING_FS_TOOLS:
            return self._cmd_fs(exe, argv, approved)
        if exe in ("node", "python", "python3"):
            return self._cmd_interp(exe, argv, approved)
        return allow("exec.dev_tool", f"{exe} permitido")

    # -- git -------------------------------------------------------------- #
    def _cmd_git(self, argv: list[str]) -> Decision:
        rest = argv[1:]
        if any(a in ("-C", "-c", "--git-dir", "--work-tree") or a.startswith("--git-dir=")
               for a in rest[:2]):
            return deny("git.global_opts", "opcoes globais do git (-C/-c) nao permitidas")
        sub = next((a for a in rest if not a.startswith("-")), "")
        if not sub:
            return deny("git.no_subcommand", "subcomando git ausente")
        if sub not in GIT_ALLOWED:
            return deny("git.forbidden", f"git {sub} e exclusivo do orquestrador")
        flags = [a for a in rest if a.startswith("-")]
        for f in flags:
            if f in GIT_BAD_FLAGS or f.startswith("--force"):
                return deny("git.unsafe_flag", f"flag proibida: {f}")
            if f.startswith("--source") and sub == "restore":
                return deny("git.unsafe_flag", "git restore --source nao permitido")
        if sub == "commit" and "-n" in flags:
            return deny("git.unsafe_flag", "git commit -n (no-verify) proibido")
        if sub == "branch" and any(a in flags for a in ("-d", "-D", "-m", "-M", "-f", "--delete")):
            return deny("git.unsafe_flag", "alterar branches e exclusivo do orquestrador")
        return allow("git.ok")

    # -- npm -------------------------------------------------------------- #
    def _cmd_npm(self, argv: list[str]) -> Decision:
        rest = argv[1:]
        flags = [a for a in rest if a.startswith("-")]
        if any(f in ("--force", "-f") or f.startswith("--force=") for f in flags):
            return deny("npm.force", "npm --force proibido (ex.: audit fix --force)")
        if "--unsafe-perm" in flags:
            return deny("npm.unsafe", "--unsafe-perm proibido")
        positional = [a for a in rest if not a.startswith("-")]
        if not positional:
            return deny("npm.no_subcommand", "subcomando npm ausente")
        sub, args = positional[0], positional[1:]
        if sub in ("ci", "install", "i", "add"):
            bad = self._forbidden_pkg(args)
            if bad:
                return deny("stack.forbidden_package", f"dependencia proibida pelo contrato de stack: {bad}")
            return allow("npm.install", "instalacao de dependencias", network="egress")
        if sub in ("test", "t", "tst", "start", "ls", "list", "rebuild", "dedupe", "prune"):
            return allow("npm.ok")
        if sub in ("run", "run-script"):
            script = args[0] if args else ""
            if script in SAFE_NPM_SCRIPTS:
                return allow("npm.run")
            return need_approval("npm.script", f"script npm fora da lista segura: {script or '(vazio)'}")
        if sub == "audit":
            return allow("npm.audit", "audit", network="egress")
        if sub in ("uninstall", "remove", "rm", "un", "r"):
            return need_approval("npm.uninstall", "remover dependencia exige aprovacao")
        if sub in ("exec", "x"):
            return need_approval("npm.exec", "npm exec baixa e executa codigo remoto")
        return deny("npm.subcommand", f"npm {sub} nao permitido")

    def _cmd_pip(self, argv: list[str]) -> Decision:
        rest = [a for a in argv[1:] if not a.startswith("-")]
        flags = [a for a in argv[1:] if a.startswith("-")]
        if "--break-system-packages" in flags or "--force-reinstall" in flags:
            return deny("pip.unsafe", "flag pip proibida")
        if not rest or rest[0] != "install":
            return deny("pip.subcommand", "somente 'pip install' e permitido")
        bad = self._forbidden_pkg(rest[1:])
        if bad:
            return deny("stack.forbidden_package", f"dependencia proibida pelo contrato de stack: {bad}")
        return allow("pip.install", "instalacao de dependencias", network="egress")

    def _forbidden_pkg(self, args: Iterable[str]) -> str:
        forb = {p.lower() for p in self.cfg.forbidden_packages}
        for a in args:
            if a.startswith("-"):
                continue
            name = a.lower()
            if name.startswith("@"):  # @scope/pkg@1.2.3
                name = "@" + name[1:].split("@", 1)[0]
            else:
                name = re.split(r"[@=<>~!\[ ]", name, maxsplit=1)[0]
            if name in forb:
                return name
        return ""

    # -- leitura ---------------------------------------------------------- #
    def _cmd_readonly(self, exe: str, argv: list[str]) -> Decision:
        if exe == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint")
                                 for a in argv):
            return deny("find.mutating", "find -delete/-exec nao permitido")
        for a in argv[1:]:
            if a.startswith("-"):
                continue
            p = normalize_rel(a) if not a.startswith("/") else None
            if p is None and (a.startswith("/") or ".." in a.split("/")):
                return deny("path.invalid", f"caminho fora do workspace: {a}")
            if p and is_env_secret_path(p):
                return deny("secret.read", "leitura de .env proibida")
        return allow("readonly.ok")

    # -- mutacao de arquivos ----------------------------------------------- #
    def _cmd_fs(self, exe: str, argv: list[str], approved: bool) -> Decision:
        flags = [a for a in argv[1:] if a.startswith("-")]
        paths = [a for a in argv[1:] if not a.startswith("-")]
        if not paths:
            return deny("fs.no_target", f"{exe} sem alvo")
        recursive = any(f == "--recursive" or (not f.startswith("--") and ("r" in f or "R" in f))
                        for f in flags)
        for raw in paths:
            p = normalize_rel(raw)
            if p is None:
                return deny("path.invalid", f"caminho invalido: {raw}")
            if exe == "rm":
                if recursive:
                    parts = p.split("/")
                    if not (set(parts) & ARTEFACT_DIRS):
                        return deny("rm.recursive",
                                    f"rm -r so e permitido em artefatos de build ({sorted(ARTEFACT_DIRS)[:4]}...): {p}")
                    continue
                d = self.check_delete(p, approved=approved)
                if d.verdict is not Verdict.ALLOW:
                    return d
            else:
                d = self.check_write(p, "", exists=True, approved=approved)
                if d.verdict is not Verdict.ALLOW:
                    return d
        if exe == "mv":
            for raw in paths[:-1]:
                d = self.check_delete(raw, approved=approved)
                if d.verdict is not Verdict.ALLOW:
                    return d
        return allow("fs.ok")

    # -- interpretadores ---------------------------------------------------- #
    def _cmd_interp(self, exe: str, argv: list[str], approved: bool) -> Decision:
        if any(a in INLINE_CODE_FLAGS for a in argv[1:3]) and not approved:
            return need_approval("exec.inline_code", f"{exe} com codigo inline exige aprovacao")
        return allow("interp.ok")


def build_policy(owned_paths: list[str], forbidden_packages: Iterable[str] = (),
                 base_check: BaseCheck | None = None) -> KilnPolicy:
    owned = list(owned_paths)
    return KilnPolicy(PolicyConfig(owned_paths=owned, forbidden_packages=set(forbidden_packages)),
                      base_check=base_check)
