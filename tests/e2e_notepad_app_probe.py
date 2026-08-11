"""Real Notepad application acceptance probe on an isolated temporary file.

This probe is intentionally manual/interactive-session only.  It launches a
new Notepad process for a temporary file, verifies the application profile and
UIA observation, then uses the verified keyboard fallback when the Windows
Notepad build does not expose an editable UIA value provider.  It never opens,
reads, or writes a user's existing document.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import ctypes
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop_tools import DesktopTools
from desktop_uia import DesktopUIA


def _find_window_handle(pid: int, title_fragment: str, timeout: float = 8.0) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    fragment = str(title_fragment or "").casefold()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches: list[tuple[bool, int]] = []

        @callback_type
        def visit(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = int(user32.GetWindowTextLengthW(hwnd) or 0)
            if length <= 0 or not fragment:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, len(buffer))
            if fragment not in buffer.value.casefold():
                return True
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            matches.append((int(owner_pid.value) == int(pid), int(hwnd)))
            return True

        try:
            user32.EnumWindows(visit, 0)
        except Exception:
            matches = []
        if matches:
            matches.sort(key=lambda item: not item[0])
            return matches[0][1]
        time.sleep(0.15)
    return 0


def _edit_text(hwnd: int) -> str | None:
    try:
        from pywinauto import Desktop
        window = Desktop(backend="uia").window(handle=hwnd)
        edits = [item for item in window.descendants()
                 if str(getattr(item.element_info, "control_type", "") or "").lower()
                 in {"edit", "document"}]
        for edit in edits:
            try:
                for method_name in ("get_value", "window_text"):
                    method = getattr(edit, method_name, None)
                    if callable(method):
                        value = method()
                        if value is not None:
                            return str(value)
            except Exception:
                continue
    except Exception:
        return None
    return None


def _close_window(hwnd: int) -> None:
    try:
        import ctypes
        ctypes.WinDLL("user32", use_last_error=True).PostMessageW(hwnd, 0x0010, 0, 0)
    except Exception:
        pass


def _activate_window(hwnd: int, tools: DesktopTools | None = None,
                     diagnostics: dict[str, object] | None = None) -> bool:
    """Activate an isolated test window even when the shell owns the focus."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetFocus.argtypes = [wintypes.HWND]
        target = int(user32.GetAncestor(wintypes.HWND(hwnd), 2) or hwnd)
        if diagnostics is not None:
            diagnostics["target_hwnd"] = target
        if tools is not None:
            current = int(user32.GetForegroundWindow() or 0)
            if diagnostics is not None:
                diagnostics["foreground_before"] = current
            tools.set_overlay_window(current)
            focused = tools.focus_target(target)
            if diagnostics is not None:
                diagnostics["production_focus"] = {
                    "ok": bool(focused.get("ok")),
                    "error": str(focused.get("error") or "")[:180],
                }
            if focused.get("ok"):
                return True
        try:
            from pywinauto import Desktop
            Desktop(backend="uia").window(handle=target).set_focus()
        except Exception:
            pass
        current = int(user32.GetForegroundWindow() or 0)
        current_pid = wintypes.DWORD()
        target_pid = wintypes.DWORD()
        current_thread = int(user32.GetWindowThreadProcessId(current, ctypes.byref(current_pid)) or 0)
        target_thread = int(user32.GetWindowThreadProcessId(target, ctypes.byref(target_pid)) or 0)
        attached = bool(current_thread and target_thread and current_thread != target_thread and
                        user32.AttachThreadInput(current_thread, target_thread, True))
        try:
            user32.SetForegroundWindow(wintypes.HWND(target))
            user32.SetActiveWindow(wintypes.HWND(target))
            user32.SetFocus(wintypes.HWND(target))
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, target_thread, False)
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            if int(user32.GetForegroundWindow() or 0) == target:
                return True
            time.sleep(0.02)
        return int(user32.GetForegroundWindow() or 0) == target
    except Exception:
        return False


def _window_process_id(hwnd: int) -> int:
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        process_id = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        return int(process_id.value)
    except Exception:
        return 0


def main() -> int:
    if os.name != "nt":
        print(json.dumps({"ok": False, "error_kind": "unsupported_platform"}, ensure_ascii=False))
        return 2
    process = None
    target = None
    tools = None
    overlay = None
    hwnd = 0
    try:
        with tempfile.TemporaryDirectory(prefix="deskorb-notepad-e2e-") as directory:
            target = Path(directory) / "deskorb-notepad-fixture.txt"
            target.write_text("", encoding="utf-8")
            process = subprocess.Popen(["notepad.exe", str(target)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            hwnd = _find_window_handle(process.pid, target.name)
            if not hwnd:
                print(json.dumps({"ok": False, "error_kind": "window_not_found"}, ensure_ascii=False))
                return 2
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            hwnd = int(user32.GetAncestor(wintypes.HWND(hwnd), 2) or hwnd)

            observer = DesktopUIA()
            observed = observer.observe_active_window(hwnd, max_elements=120)
            controls = observed.get("controls") if isinstance(observed, dict) else []
            controls = controls if isinstance(controls, list) else []
            recommendations = observed.get("recommended_actions") if isinstance(observed, dict) else []
            recommendations = recommendations if isinstance(recommendations, list) else []
            set_value_action = next((item for item in recommendations
                                     if isinstance(item, dict) and "set_value" in (item.get("actions") or ())), None)

            marker = "DESKORB_NOTEPAD_E2E"
            uia_value_readback = False
            keyboard_fallback = False
            activation_ok = False
            capture_ok = False
            type_error = None
            activation_diagnostics: dict[str, object] = {}
            tools = DesktopTools()
            tools.set_preferred_window(hwnd)
            overlay = tk.Tk()
            overlay.title("DeskOrb Focus Harness")
            overlay.geometry("160x48+20+20")
            overlay.attributes("-topmost", True)
            overlay.update_idletasks()
            overlay.deiconify()
            overlay.lift()
            overlay.focus_force()
            overlay.update()
            if set_value_action:
                result = observer.set_value(str(set_value_action.get("control_id") or ""), hwnd, marker)
                uia_value_readback = bool(result.get("ok") and result.get("verified"))
            if not uia_value_readback:
                activation_ok = _activate_window(hwnd, tools, activation_diagnostics)
                time.sleep(0.2)
                state = tools.capture_state(hwnd)
                capture_ok = bool(state.get("ok"))
                if state.get("ok"):
                    typed = tools.type_text(state["snapshot_id"], marker)
                    keyboard_fallback = bool(typed.get("ok"))
                    type_error = str(typed.get("error") or "")[:160]
            # Save only the temporary fixture so WM_CLOSE cannot leave a
            # confirmation dialog behind.  The file is deleted with the temp
            # directory after the probe.
            _activate_window(hwnd, tools)
            time.sleep(0.1)
            save_state = tools.capture_state(hwnd)
            if save_state.get("ok"):
                tools.hotkey(save_state["snapshot_id"], ["ctrl", "s"])
            time.sleep(0.25)
            observed_text = _edit_text(hwnd)
            result = {
                "ok": bool(observed.get("ok") and (uia_value_readback or keyboard_fallback)
                          and observed_text is not None and marker in observed_text),
                "application_id": (observed.get("application") or {}).get("id")
                if isinstance(observed.get("application"), dict) else None,
                "preferred_backend": (observed.get("application") or {}).get("preferred_backend")
                if isinstance(observed.get("application"), dict) else None,
                "control_count": len(controls),
                "recommended_action_count": len(recommendations),
                "uia_value_readback": uia_value_readback,
                "keyboard_fallback": keyboard_fallback,
                "value_observed": bool(observed_text is not None and marker in observed_text),
                "activation_ok": activation_ok,
                "activation_diagnostics": activation_diagnostics,
                "capture_ok": capture_ok,
                "type_error": type_error,
                "observed_chars": len(observed_text or ""),
            }
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else 3
    except Exception as exc:
        print(json.dumps({"ok": False, "error_kind": type(exc).__name__,
                          "error": str(exc)[:240]}, ensure_ascii=False))
        return 4
    finally:
        if tools is not None and "hwnd" in locals() and hwnd:
            try:
                tools.user32.SetForegroundWindow(hwnd)
                state = tools.capture_state(hwnd)
                if state.get("ok"):
                    tools.hotkey(state["snapshot_id"], ["ctrl", "s"])
            except Exception:
                pass
        if "hwnd" in locals() and hwnd:
            _close_window(hwnd)
        if overlay is not None:
            try:
                overlay.destroy()
            except Exception:
                pass
        if process is not None and (not hwnd or _window_process_id(hwnd) == process.pid):
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
