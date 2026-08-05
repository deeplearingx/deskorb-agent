"""Run deterministic DeskOrb runtime E2E cases and print a JSON summary.

Usage from the repository root:
    python tests/run_local_e2e.py

The runner uses only local fakes and temporary directories.  It never contacts
the configured model provider or an external browser.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_suite() -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_e2e_runtime_harness")
    result = unittest.TestResult()
    suite.run(result)
    return {
        "ok": result.wasSuccessful(),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
    }


if __name__ == "__main__":
    report = run_suite()
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["ok"] else 2)
