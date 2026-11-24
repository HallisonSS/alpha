import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SMOKE = os.path.abspath(os.path.join(HERE, "..", "..", "kiln-sandbox", "kiln_smoke.py"))


def run(spec: dict):
    p = subprocess.run([sys.executable, SMOKE, "--spec", json.dumps(spec)], capture_output=True, text=True, timeout=60)
    return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])


def test_smoke_ok_with_real_service():
    code = ("from http.server import *\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(s):\n"
            "        s.send_response(200 if s.path=='/health' else 404); s.end_headers(); s.wfile.write(b'ok')\n"
            "    def log_message(*a): pass\n"
            "HTTPServer(('127.0.0.1', 48741), H).serve_forever()\n")
    rc, out = run({"services": [{"cmd": [sys.executable, "-c", code]}],
                   "checks": [{"url": "http://127.0.0.1:48741/health", "expect": [200]}], "timeout": 20})
    assert rc == 0 and out["ok"], out


def test_smoke_fails_when_service_down():
    rc, out = run({"services": [], "checks": [{"url": "http://127.0.0.1:48742/health", "expect": [200]}], "timeout": 2})
    assert rc == 1 and not out["ok"] and "48742" in out["detail"]


def test_smoke_fails_when_service_crashes():
    rc, out = run({"services": [{"cmd": [sys.executable, "-c", "raise SystemExit(3)"]}],
                   "checks": [{"url": "http://127.0.0.1:48743/health", "expect": [200]}], "timeout": 10})
    assert rc == 1 and "encerrou" in out["detail"]


def test_smoke_requires_checks():
    rc, out = run({"services": [], "checks": []})
    assert rc == 1 and not out["ok"]
