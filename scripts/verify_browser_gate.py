#!/usr/bin/env python3
"""Run every browser test and make any skip fail the P8 gate."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

suite = unittest.defaultTestLoader.discover("tests", pattern="*browser*.py")
suite.addTests(unittest.defaultTestLoader.discover("tests/browser", pattern="test_*.py"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.skipped:
    print(f"P8 browser gate failed: {len(result.skipped)} skipped test(s)", file=sys.stderr)
sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
