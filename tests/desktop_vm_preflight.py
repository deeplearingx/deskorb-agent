"""Fail-closed preflight for the self-hosted interactive Windows gate.

This is intentionally a capability check, not a desktop action.  It reports
only booleans and counts so a service session, locked desktop, missing pwsh, or
missing UIA provider cannot be mistaken for a valid release environment.
"""
from __future__ import annotations

import ctypes
import getpass
import json
import os
import shutil
import sys


def _uia_foreground_check(hwnd: int) -> tuple[bool, bool]:
    """Check one foreground HWND without enumerating the whole desktop."""
    if not hwnd:
        return False, False
    try:
        from pywinauto import Desktop

        window = Desktop(backend="uia").window(handle=int(hwnd))
        return True, bool(window.exists(timeout=2))
    except Exception:
        return False, False


def _windows_checks() -> dict[str, object]:
    user32 = ctypes.windll.user32
    foreground_hwnd = int(user32.GetForegroundWindow() or 0)
    uia_provider, interactive_uia_desktop = _uia_foreground_check(foreground_hwnd)
    checks: dict[str, object] = {
        "signed_in_user": bool(str(getpass.getuser() or "").strip()),
        "desktop_window": bool(user32.GetDesktopWindow()),
        "foreground_window": bool(foreground_hwnd),
        "screen_size": bool(int(user32.GetSystemMetrics(0) or 0) > 0
                             and int(user32.GetSystemMetrics(1) or 0) > 0),
        "powershell7": bool(shutil.which("pwsh")),
        "uia_provider": uia_provider,
        "uia_window_count": 1 if interactive_uia_desktop else 0,
        "interactive_uia_desktop": interactive_uia_desktop,
    }
    return checks


def run() -> dict[str, object]:
    if os.name != "nt":
        return {"ok": False, "error_kind": "unsupported_platform", "checks": {}}
    checks = _windows_checks()
    boolean_checks = [value for key, value in checks.items()
                      if key not in {"uia_window_count"}]
    return {"ok": all(bool(value) for value in boolean_checks), "checks": checks}


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
