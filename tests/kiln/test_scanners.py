from conftest_helpers import mkdiff
from app.kiln.scanners import (find_secrets, normalize_rel, parse_unified_diff, path_in,
                               path_matches, is_test_path)


def test_normalize_rel():
    assert normalize_rel("api/./src//a.ts") == "api/src/a.ts"
    for bad in ("", "/etc/passwd", "../x", "a/../../x", "C:\\x", "a\x00b", "a\\b"):
        assert normalize_rel(bad) is None


def test_path_matching():
    assert path_matches("api/src/a.ts", "api/")
    assert path_matches("api/src/a.ts", "api")
    assert path_matches("api/src/a.ts", "api/**")
    assert not path_matches("apix/a.ts", "api")
    assert path_matches("package.json", "package.json")
    assert path_in("tests/a.ts", ["**/tests/**"])
    assert is_test_path("api/src/a.test.ts") and is_test_path("tests/x.py") and is_test_path("api/test_a.py")
    assert not is_test_path("api/src/latest.ts")


def test_parse_diff():
    d = mkdiff("api/a.ts", added=["x"], removed=["y"]) + mkdiff("api/t.test.ts", status="deleted", removed=["it('a', ()=>{})"]) \
        + mkdiff("api/n.ts", status="added", added=["z"]) + mkdiff("api/r.ts", status="renamed", old="api/q.ts")
    fs = {f.path: f for f in parse_unified_diff(d)}
    assert fs["api/a.ts"].added == ["x"] and fs["api/a.ts"].removed == ["y"]
    assert fs["api/t.test.ts"].status == "deleted"
    assert fs["api/n.ts"].status == "added"
    assert fs["api/r.ts"].status == "renamed" and fs["api/r.ts"].old_path == "api/q.ts"


def test_diff_ignores_triple_dash_inside_hunk():
    d = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n--- removed sql comment\n+++ added\n"
    f = parse_unified_diff(d)[0]
    assert f.removed == ["-- removed sql comment"] and f.added == ["++ added"]


def test_secrets():
    assert find_secrets('const k = "AKIAABCDEFGHIJKLMNOP"')
    assert find_secrets("-----BEGIN RSA PRIVATE KEY-----")
    assert find_secrets('JWT_SECRET = "a8f3k2m9x1q7w5z0p4v6"')
    assert find_secrets("JWT_SECRET=a8f3k2m9x1q7w5z0p4v6abcd")
    assert not find_secrets("const password = req.body.password")
    assert not find_secrets("JWT_SECRET=${JWT_SECRET}")
    assert not find_secrets('token: process.env.TOKEN_VALUE_LONG_ENOUGH')
    assert not find_secrets("JWT_SECRET=changeme-changeme-changeme")
