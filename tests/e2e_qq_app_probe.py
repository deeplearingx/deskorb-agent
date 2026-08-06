"""Read-only QQ UIA application-profile acceptance probe.

The probe never focuses QQ, reads message text, clicks controls, types, or
changes account state.  It only observes top-level windows belonging to an
already-running QQ process and reports privacy-safe control counts.  Use
``--require-running`` in a self-hosted desktop gate when QQ is part of the
machine image; without it, an unavailable QQ installation is reported as a
skipped environment prerequisite.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_uia import DesktopUIA
from win32utils import window_process_name


def _qq_windows(timeout: float = 4.0) -> list[int]:
    try:
        from pywinauto import Desktop
    except ImportError:
        return []
    deadline = time.monotonic() + max(0.2, float(timeout))
    while time.monotonic() < deadline:
        handles: list[int] = []
        try:
            for window in Desktop(backend="uia").windows():
                hwnd = int(window.handle or 0)
                process = window_process_name(hwnd).lower()
                if hwnd and process in {"qq.exe", "qqnt.exe"}:
                    handles.append(hwnd)
        except Exception:
            handles = []
        if handles:
            return list(dict.fromkeys(handles))
        time.sleep(0.15)
    return []


def run(*, require_running: bool = False) -> dict[str, object]:
    if os.name != "nt":
        return {"ok": False, "error_kind": "unsupported_platform"}
    try:
        handles = _qq_windows()
    except Exception as exc:
        return {"ok": False, "error_kind": type(exc).__name__, "error": str(exc)[:160]}
    if not handles:
        return {"ok": not require_running, "skipped": True,
                "error_kind": "qq_not_running", "window_count": 0}
    observer = DesktopUIA()
    observations: list[dict[str, object]] = []
    for hwnd in handles[:8]:
        observed = observer.observe_active_window(hwnd, max_elements=120)
        if not isinstance(observed, dict) or not observed.get("ok"):
            continue
        application = observed.get("application")
        controls = observed.get("controls") if isinstance(observed.get("controls"), list) else []
        recommendations = (observed.get("recommended_actions")
                           if isinstance(observed.get("recommended_actions"), list) else [])
        high_risk = sum(1 for item in recommendations
                        if isinstance(item, dict)
                        and str(item.get("risk_level") or "").lower() == "high")
        observations.append({
            "application_id": application.get("id") if isinstance(application, dict) else None,
            "preferred_backend": application.get("preferred_backend")
            if isinstance(application, dict) else None,
            "control_count": len(controls),
            "recommended_action_count": len(recommendations),
            "high_risk_recommendation_count": high_risk,
        })
    profile_ok = any(item.get("application_id") == "qq" and item.get("control_count", 0) > 0
                     for item in observations)
    return {"ok": profile_ok, "window_count": len(handles),
            "observed_window_count": len(observations), "observations": observations}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only QQ UIA profile probe")
    parser.add_argument("--require-running", action="store_true",
                        help="Fail when QQ is not running (for a configured self-hosted VM).")
    args = parser.parse_args(argv)
    result = run(require_running=args.require_running)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
