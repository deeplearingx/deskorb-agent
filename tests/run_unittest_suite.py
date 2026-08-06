"""Run the repository test suite while isolating same-name external packages.

Some managed Python installations inject another workspace containing a
``tests`` package into ``sys.path``.  Tests that import ``tests.*`` must resolve
to this repository, or the result is an environment import failure rather than
a product failure.  CI and local hard gates use this wrapper instead of raw
``unittest discover``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def _is_external_tests_package(entry: str) -> bool:
    if not entry:
        return False
    try:
        candidate = Path(entry).resolve() / "tests"
        return candidate != TESTS.resolve() and (candidate / "__init__.py").is_file()
    except (OSError, RuntimeError, TypeError):
        return False


def main() -> int:
    sys.path[:] = [entry for entry in sys.path if not _is_external_tests_package(entry)]
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    # ``tests`` intentionally has no __init__.py; discovering from its path
    # keeps the repository a plain test directory while root-first imports
    # still resolve its namespace for modules that use ``tests.*``.
    suite = unittest.defaultTestLoader.discover(str(TESTS), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
