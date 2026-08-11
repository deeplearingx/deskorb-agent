"""Real Windows desktop end-to-end probe with an isolated Tk fixture.

The model is deterministic, but AgentRuntime, approval, DesktopTools,
snapshot-baseline verification, task journal, and the actual Windows mouse and
keyboard input path are real.  No existing application, document, account, or
clipboard is touched.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import tkinter as tk
import ctypes
from ctypes import wintypes
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import patch

os.environ.setdefault("DESKORB_AGENT_PLAYWRIGHT_MCP", "0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from desktop_uia import DesktopUIA


def ensure_fixture_foreground(root: tk.Tk, hwnd: int) -> bool:
    """Keep the isolated fixture foreground while the real input path runs.

    A hosted Windows desktop can have another app reclaim focus between Tk's
    ``focus_force`` and the first AgentRuntime observation.  The fixture is
    disposable, so a temporary topmost flag plus an explicit Win32 focus call
    makes the E2E result deterministic without weakening production focus
    safety (which still rejects unexpected foreground changes).
    """
    if not hwnd:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    root.deiconify()
    root.lift()
    root.focus_force()
    root.update()
    target_root = int(user32.GetAncestor(wintypes.HWND(hwnd), 2) or hwnd)
    target = wintypes.HWND(target_root)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.ShowWindow(target, 9)  # SW_RESTORE
    user32.BringWindowToTop(target)
    current = wintypes.HWND(user32.GetForegroundWindow() or 0)
    current_pid = wintypes.DWORD()
    target_pid = wintypes.DWORD()
    current_thread = int(user32.GetWindowThreadProcessId(current, ctypes.byref(current_pid)) or 0)
    target_thread = int(user32.GetWindowThreadProcessId(target, ctypes.byref(target_pid)) or 0)
    attached = bool(current_thread and target_thread and current_thread != target_thread and
                    user32.AttachThreadInput(current_thread, target_thread, True))
    try:
        user32.SetForegroundWindow(target)
        user32.SetActiveWindow(target)
        user32.SetFocus(target)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, target_thread, False)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        root.update()
        if int(user32.GetForegroundWindow() or 0) == int(hwnd):
            return True
        time.sleep(0.01)
    return int(user32.GetForegroundWindow() or 0) == target_root


def drain(events: Queue) -> list[tuple[str, object]]:
    values: list[tuple[str, object]] = []
    while True:
        try:
            values.append(events.get_nowait())
        except Empty:
            return values


def approval_token(events: list[tuple[str, object]]) -> str | None:
    for kind, value in events:
        if kind == "approval":
            match = re.search(r"确认\s+([A-F0-9]{6,})", str(value))
            if match:
                return match.group(1)
    return None


class ScriptedDesktopModel:
    """Emit bounded tool calls while reading only structural observations."""

    def __init__(self, root: tk.Tk, entry: tk.Entry, target_hwnd: int):
        self.root = root
        self.entry = entry
        self.target_hwnd = int(target_hwnd or 0)
        self.stage = 0
        self.baseline_id = ""
        self.focus_reacquire_failures = 0

    def _pump(self) -> None:
        self.root.update_idletasks()
        self.root.update()

    def _refocus_input(self) -> None:
        """Keep the disposable fixture focused between real input actions.

        The production runtime must reject a changed foreground window.  This
        test-only fixture can safely reacquire its own HWND before the next
        scripted action so unrelated desktop activity cannot make the smoke
        test flaky.
        """
        if not ensure_fixture_foreground(self.root, self.target_hwnd):
            self.focus_reacquire_failures += 1
        self.entry.focus_force()
        self.root.update()

    @staticmethod
    def _last_snapshot(payload: dict) -> str:
        text = json.dumps(payload.get("input") or [], ensure_ascii=False)
        matches = re.findall(r'\\?"snapshot_id\\?"\s*:\s*\\?"([A-F0-9]+)', text)
        return matches[-1] if matches else ""

    def __call__(self, payload: dict, _api_key: str) -> dict:
        self._pump()
        snapshot_id = self._last_snapshot(payload)
        if self.stage == 0:
            call = {"application": "notepad"}
            name = "desktop_capture_state"
        elif self.stage == 1:
            self._refocus_input()
            call = {
                "snapshot_id": snapshot_id,
                "x": self.entry.winfo_rootx() + self.entry.winfo_width() // 2,
                "y": self.entry.winfo_rooty() + self.entry.winfo_height() // 2,
                "button": "left", "count": 1, "risk_level": "normal",
                "risk_reason": "focus the isolated test field",
            }
            name = "desktop_click"
        elif self.stage == 2:
            self._refocus_input()
            self.baseline_id = snapshot_id
            call = {
                "snapshot_id": snapshot_id, "text": "DESKORB_DESKTOP_E2E",
                "risk_level": "normal", "risk_reason": "fill the isolated test field",
            }
            name = "desktop_type"
        elif self.stage == 3:
            call = {"snapshot_id": self.baseline_id or snapshot_id}
            name = "desktop_verify_state"
        else:
            return {"output_text": "已输入并通过桌面状态基线验证。", "output": []}
        self.stage += 1
        return {"output": [{"type": "function_call", "call_id": f"desktop-{self.stage}",
                            "name": name, "arguments": json.dumps(call, ensure_ascii=False)}]}


def main() -> int:
    root = tk.Tk()
    root.title("DeskOrb Desktop E2E Fixture")
    root.geometry("520x160+100+100")
    tk.Label(root, text="Isolated DeskOrb desktop fixture").pack(pady=(18, 8))
    entry = tk.Entry(root, font=("Segoe UI", 14), width=36)
    entry.pack(padx=24, pady=8)
    root.update_idletasks()
    root.deiconify()
    root.lift()
    root.focus_force()
    entry.focus_force()
    root.update()
    target_child_hwnd = int(root.winfo_id() or 0)
    user32_target = ctypes.WinDLL("user32", use_last_error=True)
    user32_target.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32_target.GetAncestor.restype = wintypes.HWND
    target_hwnd = int(user32_target.GetAncestor(wintypes.HWND(target_child_hwnd), 2)
                      or target_child_hwnd)
    foreground_ready = ensure_fixture_foreground(root, target_hwnd)
    user32_diag = ctypes.WinDLL("user32", use_last_error=True)
    user32_diag.GetForegroundWindow.restype = wintypes.HWND
    foreground_hwnd = int(user32_diag.GetForegroundWindow() or 0)
    uia = DesktopUIA()
    uia_probe = uia.observe_active_window(target_hwnd, max_elements=80)
    uia_recommendations = uia_probe.get("recommended_actions") if isinstance(uia_probe, dict) else []
    uia_recommendations = uia_recommendations if isinstance(uia_recommendations, list) else []
    uia_set_value = next((item for item in uia_recommendations
                          if isinstance(item, dict) and "set_value" in (item.get("actions") or ())), None)
    uia_value_readback = False
    if uia_set_value:
        uia_result = uia.set_value(str(uia_set_value.get("control_id") or ""), target_hwnd,
                                   "DESKORB_UIA_PREFLIGHT")
        uia_value_readback = bool(uia_result.get("ok") and uia_result.get("verified"))
        root.update()
        entry.delete(0, tk.END)
        root.update()
    events: Queue = Queue()
    bridge = None
    try:
        with tempfile.TemporaryDirectory(prefix="deskorb-desktop-e2e-") as directory:
            runtime = AgentRuntime(events, "fixture-model", "https://example.test/v1",
                                   working_dir=Path(directory))
            bridge = runtime.mcp
            runtime.mcp = None
            runtime.set_desktop_target_window(target_hwnd)
            scripted = ScriptedDesktopModel(root, entry, target_hwnd)
            runtime._request = scripted
            tool_results: list[dict[str, object]] = []
            original_record = runtime._record_tool_result

            def record_tool_result(name, arguments, result):
                if isinstance(result, dict):
                    tool_results.append({
                        "tool": str(name)[:48], "ok": bool(result.get("ok")),
                        "snapshot_id": str(arguments.get("snapshot_id") or "")[:16],
                        "screen_changed": result.get("screen_changed"),
                        "active_window_changed": result.get("active_window_changed"),
                        "error": str(result.get("error") or "")[:120],
                    })
                return original_record(name, arguments, result)

            runtime._record_tool_result = record_tool_result
            with patch("agent_runtime.get_api_key", return_value="fixture-key"):
                runtime.run_turn("在当前隔离测试窗口输入 DESKORB_DESKTOP_E2E 并验证桌面变化", [])
                first = drain(events)
                token = approval_token(first)
                if token:
                    runtime.run_turn("确认 " + token, [])
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    root.update()
                    if entry.get() == "DESKORB_DESKTOP_E2E":
                        break
                    time.sleep(0.03)
            all_events = [*first, *drain(events)]
            progress = [value for kind, value in all_events
                        if kind == "task_progress" and isinstance(value, dict) and value.get("terminal")]
            final = progress[-1] if progress else {}
            result = {
                "ok": bool(token and entry.get() == "DESKORB_DESKTOP_E2E"
                          and foreground_ready
                          and final.get("terminal") == "completed"
                          and final.get("verified")),
                "approval_used": bool(token),
                "value_exact": entry.get() == "DESKORB_DESKTOP_E2E",
                "value_chars": len(entry.get()),
                "value_nonempty": bool(entry.get()),
                "terminal": final.get("terminal"),
                "verified": bool(final.get("verified")),
                "script_stage": scripted.stage,
                "focus_reacquire_failures": scripted.focus_reacquire_failures,
                "evidence_steps": final.get("evidence_steps"),
                "failed_steps": final.get("failed_steps"),
                "tool_results": tool_results,
                "uia_available": bool(uia.available),
                "foreground_ready": foreground_ready,
                "target_hwnd": target_hwnd,
                "target_child_hwnd": target_child_hwnd,
                "foreground_hwnd": foreground_hwnd,
                "uia_recommended_actions": len(uia_recommendations),
                "uia_set_value_available": bool(uia_set_value),
                "uia_value_readback": uia_value_readback,
            }
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                          "error": str(exc)[:500]}, ensure_ascii=False))
        return 3
    finally:
        if bridge is not None:
            bridge.close()
        root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
