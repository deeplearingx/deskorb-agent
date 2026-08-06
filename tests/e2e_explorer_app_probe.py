"""Read-only File Explorer application-profile acceptance probe.

Explorer is started on a temporary directory and closed by its unique title.
The probe never enumerates or changes a user's existing folder.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_uia import DesktopUIA


def _find_title(fragment: str, timeout: float = 8.0) -> int:
    try:
        from pywinauto import Desktop
    except ImportError:
        return 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for window in Desktop(backend="uia").windows():
                title = str(window.window_text() or "")
                if fragment.lower() in title.lower():
                    return int(window.handle or 0)
        except Exception:
            pass
        time.sleep(0.15)
    return 0


def _close(hwnd: int) -> None:
    try:
        ctypes.WinDLL("user32", use_last_error=True).PostMessageW(hwnd, 0x0010, 0, 0)
    except Exception:
        pass


def _window_process_id(hwnd: int) -> int:
    try:
        process_id = ctypes.wintypes.DWORD()
        ctypes.WinDLL("user32", use_last_error=True).GetWindowThreadProcessId(
            hwnd, ctypes.byref(process_id))
        return int(process_id.value)
    except Exception:
        return 0


def main() -> int:
    if os.name != "nt":
        print(json.dumps({"ok": False, "error_kind": "unsupported_platform"}, ensure_ascii=False))
        return 2
    process = None
    hwnd = 0
    try:
        with tempfile.TemporaryDirectory(prefix="deskorb-explorer-e2e-") as directory:
            folder = Path(directory)
            (folder / "fixture.txt").write_text("temporary fixture", encoding="utf-8")
            process = subprocess.Popen(["explorer.exe", str(folder)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            hwnd = _find_title(folder.name)
            if not hwnd:
                print(json.dumps({"ok": False, "error_kind": "window_not_found"}, ensure_ascii=False))
                return 2
            observed = DesktopUIA().observe_active_window(hwnd, max_elements=120)
            controls = observed.get("controls") if isinstance(observed, dict) else []
            controls = controls if isinstance(controls, list) else []
            recommendations = observed.get("recommended_actions") if isinstance(observed, dict) else []
            recommendations = recommendations if isinstance(recommendations, list) else []
            application = observed.get("application") if isinstance(observed, dict) else {}
            result = {
                "ok": bool(observed.get("ok") and isinstance(application, dict)
                          and application.get("id") == "file_explorer" and controls),
                "application_id": application.get("id") if isinstance(application, dict) else None,
                "preferred_backend": application.get("preferred_backend")
                if isinstance(application, dict) else None,
                "control_count": len(controls),
                "recommended_action_count": len(recommendations),
            }
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else 3
    except Exception as exc:
        print(json.dumps({"ok": False, "error_kind": type(exc).__name__,
                          "error": str(exc)[:240]}, ensure_ascii=False))
        return 4
    finally:
        if hwnd:
            _close(hwnd)
        if (process is not None and process.poll() is None
                and (not hwnd or _window_process_id(hwnd) == process.pid)):
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
