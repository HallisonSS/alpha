#!/usr/bin/env python3
"""Smoke runner (stdlib) executado DENTRO do sandbox.

Sobe os servicos do spec, espera ficarem saudaveis, roda os checks HTTP e
imprime uma linha JSON final: {"ok": bool, "checks": [...], "detail": "..."}.
Spec (JSON via --spec ou stdin):
  {"services":[{"cwd":"api","cmd":["npm","start"],"env":{"PORT":"4000"}}],
   "checks":[{"url":"http://127.0.0.1:4000/health","expect":[200]}],
   "timeout": 90}
Exit code 0 = tudo ok.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


def http(url: str, method: str = "GET", timeout: float = 5.0):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(512).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # conexao recusada, timeout...
        return None, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help="JSON do spec (default: stdin)")
    ap.add_argument("--root", default=".", help="diretorio base dos servicos")
    a = ap.parse_args()
    spec = json.loads(a.spec if a.spec else sys.stdin.read())
    timeout = float(spec.get("timeout", 90))
    checks = spec.get("checks", [])
    procs, logs = [], []
    result = {"ok": False, "checks": [], "detail": ""}
    try:
        for s in spec.get("services", []):
            cwd = os.path.join(a.root, s.get("cwd", "."))
            env = dict(os.environ, **{str(k): str(v) for k, v in (s.get("env") or {}).items()})
            p = subprocess.Popen(s["cmd"], cwd=cwd, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
            procs.append(p)
        deadline = time.time() + timeout
        pending = list(checks)
        while pending and time.time() < deadline:
            dead = [p for p in procs if p.poll() is not None]
            if dead:
                result["detail"] = "servico encerrou antes dos checks (rc=%s)" % dead[0].returncode
                break
            pending = [c for c in pending if http(c["url"], c.get("method", "GET"))[0] not in c.get("expect", [200])]
            if pending:
                time.sleep(1.0)
        for c in checks:
            status, body = http(c["url"], c.get("method", "GET"))
            result["checks"].append({"url": c["url"], "status": status, "expect": c.get("expect", [200]),
                                     "ok": status in c.get("expect", [200]), "body": body[:120]})
        result["ok"] = bool(checks) and all(c["ok"] for c in result["checks"])
        if not checks:
            result["detail"] = "spec sem checks"
        elif not result["ok"] and not result["detail"]:
            bad = [c for c in result["checks"] if not c["ok"]]
            result["detail"] = "; ".join(f"{c['url']} -> {c['status']} {c['body'][:60]}" for c in bad)
    except Exception as e:
        result["detail"] = f"erro no smoke: {e!r}"
    finally:
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:
                    pass
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
