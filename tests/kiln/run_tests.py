"""Runner minimo (sem pytest): python tests/kiln/run_tests.py
Com pytest instalado, basta `pytest tests/kiln`."""
import importlib
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

failed = passed = 0
for fn in sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py")):
    mod = importlib.import_module(fn[:-3])
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        try:
            getattr(mod, name)()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAIL {fn}::{name}")
            traceback.print_exc()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
