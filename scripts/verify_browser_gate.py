#!/usr/bin/env python3
"""Run every browser test in isolation and make any skip fail the P8 gate."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_module(module: str) -> int:
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print(f"P8 browser gate failed: {len(result.skipped)} skipped test(s)", file=sys.stderr)
    return 0 if result.wasSuccessful() and not result.skipped else 1


def browser_modules() -> list[str]:
    top_level = [f"tests.{path.stem}" for path in sorted((ROOT / "tests").glob("*browser*.py"))]
    dedicated = [
        f"tests.browser.{path.stem}"
        for path in sorted((ROOT / "tests" / "browser").glob("test_*.py"))
    ]
    return top_level + dedicated


if len(sys.argv) == 3 and sys.argv[1] == "--module":
    sys.exit(run_module(sys.argv[2]))
if len(sys.argv) != 1:
    print("usage: verify_browser_gate.py [--module MODULE]", file=sys.stderr)
    sys.exit(2)

# Importing the entire suite up front leaves every FastAPI server, Playwright
# runtime and module patch in one interpreter.  That made the quality gate
# order-dependent even though the same journeys passed in isolation.  A fresh
# interpreter per module gives each browser/server lifecycle an honest boundary
# while retaining the strict no-skip policy in ``run_module``.
failed = []
for module in browser_modules():
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--module", module],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode:
        failed.append(module)

if failed:
    print(f"P8 browser gate failed modules: {', '.join(failed)}", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
