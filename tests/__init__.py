"""DeskOrb's test helpers are importable as a local package.

Keeping an explicit package marker prevents an unrelated globally installed
``tests`` package from shadowing this repository during full-suite collection.
"""

from pathlib import Path
import sys

_TEST_ROOT = str(Path(__file__).resolve().parent)
if _TEST_ROOT not in sys.path:
    sys.path.insert(0, _TEST_ROOT)
