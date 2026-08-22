#!/usr/bin/env python3
"""Run all non-browser unittest suites in one deterministic process."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _tests(item)
        else:
            yield item


def _is_browser(test) -> bool:
    module = test.__class__.__module__
    leaf = module.rsplit(".", 1)[-1]
    return module == "browser" or module.startswith("browser.") or ".browser." in module or leaf.endswith("_browser")


discovered = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
suite = unittest.TestSuite(test for test in _tests(discovered) if not _is_browser(test))
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.skipped:
    print(f"standard gate failed: {len(result.skipped)} skipped test(s)", file=sys.stderr)
exit_code = 0 if result.wasSuccessful() and not result.skipped else 1
# Some integration tests intentionally exercise abandoned Job executors. Their
# worker threads may outlive the assertion result, so terminate this dedicated
# test process after flushing instead of letting thread shutdown hang the CI
# lane indefinitely.
sys.stdout.flush()
sys.stderr.flush()
os._exit(exit_code)
