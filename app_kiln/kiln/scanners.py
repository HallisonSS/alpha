"""Utilitarios puros (stdlib) usados por policy.py e gates.py.

Tudo aqui e deterministico e testavel sem Docker, Redis ou Postgres.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Caminhos
# --------------------------------------------------------------------------- #


def normalize_rel(path: str) -> str | None:
    """Normaliza um caminho relativo. Retorna None se for invalido/perigoso."""
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        return None
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None
    norm = posixpath.normpath(path)
    if norm in (".", "") or norm == ".." or norm.startswith("../"):
        return None
    return norm


def _has_glob(s: str) -> bool:
    return any(c in s for c in "*?[")


def glob_match(path: str, pattern: str) -> bool:
    # "/" + path permite que "**/tests/**" case com "tests/a.ts" (raiz).
    return any(fnmatch.fnmatchcase(c, pattern) for c in (path, "/" + path))


def path_matches(path: str, pattern: str) -> bool:
    """Casa `path` com um padrao de pasta ("api/"), arquivo ("package.json") ou glob."""
    p = pattern.strip()
    if not p:
        return False
    if p in ("**", "./**", "."):
        return True
    if p.endswith("/**"):
        base = p[:-3]
        if not _has_glob(base):
            return path == base or path.startswith(base + "/")
    elif p.endswith("/") and not _has_glob(p):
        return path.startswith(p) or path == p.rstrip("/")
    if _has_glob(p):
        return glob_match(path, p)
    return path == p or path.startswith(p + "/")


def path_in(path: str, patterns) -> bool:
    return any(path_matches(path, p) for p in patterns)


# --------------------------------------------------------------------------- #
# Diff unificado
# --------------------------------------------------------------------------- #


@dataclass
class FileDiff:
    path: str
    status: str = "modified"  # added | modified | deleted | renamed
    old_path: str | None = None
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


_HDR = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$')


def parse_unified_diff(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = _HDR.match(line)
            cur = FileDiff(path=(m.group(2) if m else line[11:]))
            files.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue
        if not in_hunk:
            if line.startswith("new file mode"):
                cur.status = "added"
            elif line.startswith("deleted file mode"):
                cur.status = "deleted"
            elif line.startswith("rename from "):
                cur.old_path, cur.status = line[12:], "renamed"
            elif line.startswith("rename to "):
                cur.path = line[10:]
            elif line.startswith("@@"):
                in_hunk = True
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("+"):
            cur.added.append(line[1:])
        elif line.startswith("-"):
            cur.removed.append(line[1:])
    return files


# --------------------------------------------------------------------------- #
# Segredos
# --------------------------------------------------------------------------- #

_PLACEHOLDER = re.compile(
    r"(?i)(change[_-]?me|example|placeholder|your[_-]|<[^>]+>|\$\{[^}]*\}|process\.env|"
    r"os\.environ|getenv|x{4,}|\*{4,}|dummy|test|todo|secret[_-]?here|redacted)"
)

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("api_key_sk", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
]
_QUOTED_ASSIGN = re.compile(
    r"""(?ix)\b[A-Za-z0-9_]*(?:secret|token|passw(?:or)?d|api[_-]?key|private[_-]?key)[A-Za-z0-9_]*
        \s*[:=]\s*["']([A-Za-z0-9/+_\-=.]{16,})["']"""
)
_ENV_ASSIGN = re.compile(
    r"^\s*(?:export\s+)?[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*"
    r"\s*=\s*([^\s'\"$<{#][^\s#]{15,})\s*$"
)


def _mask(v: str) -> str:
    return v[:4] + "..." if len(v) > 4 else "..."


def find_secrets(text: str) -> list[str]:
    """Lista de achados ('regra: trecho mascarado'). Vazia = limpo."""
    hits: list[str] = []
    for line in text.splitlines():
        for name, rx in SECRET_PATTERNS:
            m = rx.search(line)
            if m and not _PLACEHOLDER.search(m.group(0)):
                hits.append(f"{name}: {_mask(m.group(0))}")
        for name, rx in (("quoted_secret", _QUOTED_ASSIGN), ("env_secret", _ENV_ASSIGN)):
            m = rx.search(line)
            if m and not _PLACEHOLDER.search(m.group(1)):
                hits.append(f"{name}: {_mask(m.group(1))}")
    return hits


# --------------------------------------------------------------------------- #
# Testes: pular/focar/remover + supressoes + configs enfraquecidas
# --------------------------------------------------------------------------- #

TEST_FILE_GLOBS = (
    "**/tests/**", "**/test/**", "**/__tests__/**", "**/*.test.*", "**/*.spec.*",
    "**/test_*.py", "**/*_test.py", "**/*_test.go",
)

SKIP_PATTERNS = [
    re.compile(r"\b(?:it|test|describe)\.(?:skip|todo|only)\b"),
    re.compile(r"\b(?:xit|xtest|xdescribe|fit|fdescribe)\s*\("),
    re.compile(r"@pytest\.mark\.(?:skip|skipif|xfail)"),
    re.compile(r"\bpytest\.(?:skip|xfail)\s*\("),
    re.compile(r"\bunittest\.skip"),
    re.compile(r"\bt\.Skip(?:Now)?\s*\("),
]

TEST_DECL = re.compile(
    r"^\s*(?:(?:it|test)\s*\(|(?:it|test)\.each|(?:async\s+)?def\s+test_|func\s+Test\w+\s*\()"
)

SUPPRESSION_PATTERNS = [
    re.compile(r"@ts-ignore"),
    re.compile(r"@ts-nocheck"),
    re.compile(r"eslint-disable"),
    re.compile(r"#\s*type:\s*ignore"),
    re.compile(r"#\s*noqa"),
    re.compile(r"#\s*pylint:\s*disable"),
    re.compile(r"#\s*nosec"),
    re.compile(r"//\s*nolint"),
    re.compile(r"\bas\s+any\b"),
]

CONFIG_FILE_GLOBS = (
    "**/tsconfig*.json", "**/.eslintrc*", "**/eslint.config.*", "**/jest.config.*",
    "**/vitest.config.*", "**/pytest.ini", "**/.prettierrc*", "**/Dockerfile*",
    "**/docker-compose*", "**/.github/**", "**/tox.ini", "**/setup.cfg", "**/mypy.ini",
)

CONFIG_WEAKENING = [
    re.compile(r'"strict"\s*:\s*false'),
    re.compile(r'"(?:noImplicitAny|strictNullChecks|noUncheckedIndexedAccess)"\s*:\s*false'),
    re.compile(r'"noEmitOnError"\s*:\s*false'),
    re.compile(r"""['"]off['"]"""),
    re.compile(r"""['"]?[\w@/\-]+['"]?\s*:\s*(?:0|['"]0['"])\s*,?\s*$"""),
    re.compile(r"ignore_errors\s*=\s*(?:True|true)"),
]

# --------------------------------------------------------------------------- #
# Destrutivo
# --------------------------------------------------------------------------- #

# Em comandos (argv): qualquer DDL/DML devastador.
DESTRUCTIVE_CMD = re.compile(
    r"(?is)\b(drop\s+(?:database|schema|table)\b|truncate\s+(?:table\s+)?\w|"
    r"delete\s+from\s+[\w.\"]+\s*(?:;|$)|flushall|flushdb|dropdatabase)"
)
# Em codigo adicionado no diff: so o que nunca deveria entrar sem aprovacao.
DESTRUCTIVE_CODE = re.compile(
    r"(?i)(drop\s+(?:database|schema)\b|truncate\s+table\b|\bflushall\b|\bflushdb\b|"
    r"\.dropDatabase\s*\(|git\s+push\b.*--force|rm\s+-[a-z]*r[a-z]*f?\s+/(?:\s|$))"
)


def is_test_path(path: str) -> bool:
    return any(glob_match(path, g) for g in TEST_FILE_GLOBS)


def is_config_path(path: str) -> bool:
    return any(glob_match(path, g) for g in CONFIG_FILE_GLOBS)


def is_env_secret_path(path: str) -> bool:
    base = posixpath.basename(path)
    return base == ".env" or (base.startswith(".env.") and base not in (".env.example", ".env.sample", ".env.template"))
