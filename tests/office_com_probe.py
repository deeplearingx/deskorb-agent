"""Manual read-only smoke check for the currently focused Word or Excel window."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from office_sources import OfficeSourceError, read_active_office_snapshot
from win32utils import foreground_capture_window


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("word", "excel"))
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        snapshot = read_active_office_snapshot(args.kind, foreground_capture_window())
    except OfficeSourceError as exc:
        print(str(exc))
        return 1
    print(json.dumps({
        "kind": snapshot.kind, "name": snapshot.name, "fingerprint": snapshot.fingerprint[:12],
        "targets": len(snapshot.targets), "unsaved": snapshot.has_unsaved_changes,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
