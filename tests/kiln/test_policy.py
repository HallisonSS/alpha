from conftest_helpers import api_policy
from app.kiln.policy import Verdict

P = api_policy()


def v(argv):
    return P.check_command(argv).verdict


def test_allows_normal_dev_flow():
    assert v(["npm", "test"]) is Verdict.ALLOW
    assert v(["npm", "run", "build"]) is Verdict.ALLOW
    assert v(["git", "add", "-A"]) is Verdict.ALLOW
    assert v(["git", "commit", "-m", "feat: x"]) is Verdict.ALLOW
    assert P.check_command(["npm", "ci"]).network == "egress"
    assert P.check_command(["npm", "test"]).network == "none"


def test_denies_shells_and_strings():
    assert v(["bash", "-c", "rm -rf /"]) is Verdict.DENY
    assert v(["sudo", "ls"]) is Verdict.DENY
    assert v("npm test") is Verdict.DENY
    assert v([]) is Verdict.DENY


def test_denies_force_and_history_rewrites():
    assert v(["git", "push", "--force"]) is Verdict.DENY
    assert v(["git", "push"]) is Verdict.DENY
    assert v(["git", "pull"]) is Verdict.DENY
    assert v(["git", "reset", "--hard"]) is Verdict.DENY
    assert v(["git", "commit", "-n", "-m", "x"]) is Verdict.DENY
    assert v(["git", "-c", "core.hooksPath=x", "status"]) is Verdict.DENY
    assert v(["npm", "audit", "fix", "--force"]) is Verdict.DENY


def test_denies_destructive_sql_anywhere():
    assert v(["psql", "-c", "DROP DATABASE app"]) is Verdict.DENY
    assert v(["node", "-e", "db.query('DROP TABLE users')"]) is Verdict.DENY
    assert v(["python3", "x.py", "TRUNCATE TABLE users"]) is Verdict.DENY


def test_rm_rules():
    assert v(["rm", "-rf", "tests/"]) is Verdict.DENY
    assert v(["rm", "-rf", "api/tests"]) is Verdict.DENY
    assert v(["rm", "-rf", "api/node_modules"]) is Verdict.ALLOW
    assert v(["rm", "api/tests/a.test.ts"]) is Verdict.DENY
    assert v(["rm", "-rf", "/"]) is Verdict.DENY
    assert v(["rm", "api/src/old.ts"]) is Verdict.ALLOW
    assert v(["rm", "web/src/a.ts"]) is Verdict.DENY  # fora da pasta do agente


def test_forbidden_packages():
    d = P.check_command(["npm", "install", "mongoose"])
    assert d.verdict is Verdict.DENY and d.rule == "stack.forbidden_package"
    assert v(["npm", "i", "mongoose@8.1.0"]) is Verdict.DENY
    assert v(["pip", "install", "mongodb"]) is Verdict.DENY
    assert v(["npm", "install", "express"]) is Verdict.ALLOW


def test_network_and_secrets_read():
    assert v(["curl", "http://localhost:4000/health"]) is Verdict.DENY
    assert v(["cat", ".env"]) is Verdict.DENY
    assert v(["grep", "SECRET", "api/.env.production"]) is Verdict.DENY
    assert v(["cat", "api/.env.example"]) is Verdict.ALLOW
    assert v(["cat", "../outside"]) is Verdict.DENY


def test_approval_cases():
    assert v(["npx", "create-react-app"]) is Verdict.DENY  # npx fora da allowlist
    assert v(["npm", "run", "deploy"]) is Verdict.APPROVAL
    assert v(["node", "-e", "console.log(1)"]) is Verdict.APPROVAL
    assert v(["npm", "uninstall", "jest"]) is Verdict.APPROVAL


def test_write_rules():
    assert P.check_write("api/src/a.ts", "export const a = 1").verdict is Verdict.ALLOW
    assert P.check_write("contracts/routes.json", "{}").verdict is Verdict.DENY
    assert P.check_write("web/a.ts", "x").verdict is Verdict.DENY
    assert P.check_write("../x", "x").verdict is Verdict.DENY
    assert P.check_write("/etc/passwd", "x").verdict is Verdict.DENY
    assert P.check_write("api/.env", "A=1").verdict is Verdict.DENY
    assert P.check_write("api/.env.example", "JWT_SECRET=changeme").verdict is Verdict.ALLOW
    assert P.check_write("api/tsconfig.json", "{}", exists=False).verdict is Verdict.ALLOW
    assert P.check_write("api/tsconfig.json", "{}", exists=True).verdict is Verdict.APPROVAL
    assert P.check_write("api/tsconfig.json", "{}", exists=True, approved=True).verdict is Verdict.ALLOW
    assert P.check_write("api/big.ts", "x" * 300_000).verdict is Verdict.DENY


def test_write_blocks_literal_secret():
    d = P.check_write("api/config.ts", 'export const JWT_SECRET = "a8f3k2m9x1q7w5z0p4v6"')
    assert d.verdict is Verdict.DENY and d.rule == "secret.detected"
    ok = P.check_write("api/config.ts", "export const JWT_SECRET = process.env.JWT_SECRET")
    assert ok.verdict is Verdict.ALLOW


def test_delete_rules():
    assert P.check_delete("api/tests/a.test.ts").verdict is Verdict.DENY
    assert P.check_delete("api/src/a.spec.ts").verdict is Verdict.DENY
    assert P.check_delete("api/src/a.ts").verdict is Verdict.ALLOW
    assert P.check_delete("contracts/stack.json").verdict is Verdict.DENY


def test_base_check_can_only_tighten():
    from app.kiln.policy import Decision, build_policy, deny
    pol = build_policy(["api/"], base_check=lambda argv: deny("base.rule", "x") if argv[0] == "ls" else None)
    assert pol.check_command(["ls"]).rule == "base.rule"
    assert pol.check_command(["cat", "api/a.ts"]).verdict is Verdict.ALLOW
