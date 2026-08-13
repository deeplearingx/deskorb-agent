"""Real Windows desktop end-to-end probe with an isolated Tk fixture.

The model is deterministic, but AgentRuntime, approval, DesktopTools,
snapshot-baseline verification, task journal, and the actual Windows UIA input
path are real.  No existing application, document, account, or
clipboard is touched.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import ctypes
from ctypes import wintypes
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import patch

os.environ.setdefault("DESKORB_AGENT_PLAYWRIGHT_MCP", "0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from desktop_uia import DesktopUIA


class NativeEditFixture:
    """Disposable Win32 window with a real Edit control for UIA acceptance."""

    WS_OVERLAPPEDWINDOW = 0x00CF0000
    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    WS_TABSTOP = 0x00010000
    ES_AUTOHSCROLL = 0x0080
    WS_EX_CLIENTEDGE = 0x00000200
    SW_RESTORE = 9
    PM_REMOVE = 1

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._wnd_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        )
        self._wnd_proc = self._wnd_proc_type(self._dispatch)
        self._class_name = f"DeskOrbUIAFixture_{os.getpid()}"
        self._title = "DeskOrb UIA E2E Fixture"
        self._configure_api()
        self._register_class()
        instance = self.kernel32.GetModuleHandleW(None)
        self.hwnd = int(self.user32.CreateWindowExW(
            0, self._class_name, self._title, self.WS_OVERLAPPEDWINDOW | self.WS_VISIBLE,
            100, 100, 620, 180, 0, 0, instance, None,
        ) or 0)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.edit_hwnd = int(self.user32.CreateWindowExW(
            self.WS_EX_CLIENTEDGE, "EDIT", "",
            self.WS_CHILD | self.WS_VISIBLE | self.WS_TABSTOP | self.ES_AUTOHSCROLL,
            24, 60, 560, 34, self.hwnd, 100, instance, None,
        ) or 0)
        if not self.edit_hwnd:
            self.destroy()
            raise ctypes.WinError(ctypes.get_last_error())
        self.user32.ShowWindow(wintypes.HWND(self.hwnd), self.SW_RESTORE)
        self.user32.UpdateWindow(wintypes.HWND(self.hwnd))
        self.pump()

    def _configure_api(self) -> None:
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        self.user32.RegisterClassW.argtypes = [ctypes.c_void_p]
        self.user32.RegisterClassW.restype = wintypes.ATOM
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.DefWindowProcW.restype = ctypes.c_long
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.UpdateWindow.argtypes = [wintypes.HWND]
        self.user32.UpdateWindow.restype = wintypes.BOOL
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.DestroyWindow.restype = wintypes.BOOL
        self.user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        self.user32.UnregisterClassW.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.user32.TranslateMessage.argtypes = [ctypes.c_void_p]
        self.user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
        self.user32.SetFocus.argtypes = [wintypes.HWND]
        self.user32.SetFocus.restype = wintypes.HWND
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        self.user32.SetWindowTextW.restype = wintypes.BOOL

    def _register_class(self) -> None:
        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
            ]
        instance = self.kernel32.GetModuleHandleW(None)
        registration = WNDCLASSW(0, ctypes.cast(self._wnd_proc, ctypes.c_void_p), 0, 0,
                                 instance, 0, 0, 0, None, self._class_name)
        if not self.user32.RegisterClassW(ctypes.byref(registration)):
            error = ctypes.get_last_error()
            if error != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                raise ctypes.WinError(error)

    def _dispatch(self, hwnd, message, wparam, lparam):
        return self.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def deiconify(self) -> None:
        self.user32.ShowWindow(wintypes.HWND(self.hwnd), self.SW_RESTORE)

    def lift(self) -> None:
        self.user32.BringWindowToTop(wintypes.HWND(self.hwnd))

    def focus_force(self) -> None:
        self.user32.SetFocus(wintypes.HWND(self.hwnd))

    def update_idletasks(self) -> None:
        self.pump()

    def update(self) -> None:
        self.pump()

    def pump(self) -> None:
        message = ctypes.create_string_buffer(48)
        while self.user32.PeekMessageW(message, 0, 0, 0, self.PM_REMOVE):
            self.user32.TranslateMessage(message)
            self.user32.DispatchMessageW(message)

    def focus(self) -> None:
        self.user32.SetFocus(wintypes.HWND(self.edit_hwnd))
        self.pump()

    def get(self) -> str:
        self.pump()
        length = int(self.user32.GetWindowTextLengthW(wintypes.HWND(self.edit_hwnd)) or 0)
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(wintypes.HWND(self.edit_hwnd), buffer, len(buffer))
        return buffer.value

    def delete(self, _start=0, _end=None) -> None:
        self.user32.SetWindowTextW(wintypes.HWND(self.edit_hwnd), "")
        self.pump()

    def destroy(self) -> None:
        if getattr(self, "hwnd", 0):
            self.user32.DestroyWindow(wintypes.HWND(self.hwnd))
            self.hwnd = 0
        instance = self.kernel32.GetModuleHandleW(None)
        self.user32.UnregisterClassW(self._class_name, instance)


def ensure_fixture_foreground(root: object, hwnd: int) -> bool:
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

    def __init__(self, root: NativeEditFixture, entry: NativeEditFixture, target_hwnd: int):
        self.root = root
        self.entry = entry
        self.target_hwnd = int(target_hwnd or 0)
        self.stage = 0
        self.focus_reacquire_failures = 0
        self.last_uia_observation_id = ""
        self.last_uia_control_id = ""

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

    @classmethod
    def _last_edit_control(cls, payload: dict) -> str:
        """Select an observed editable control, never invent a UIA control ID."""
        observations: list[dict] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if value.get("uia_observation_id") and isinstance(value.get("controls"), list):
                    observations.append(value)
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)
            elif isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError):
                    return
                if decoded != value:
                    visit(decoded)

        visit(payload.get("input") or [])
        if not observations:
            return ""
        latest = observations[-1]
        for item in latest.get("recommended_actions") or []:
            if isinstance(item, dict) and "set_value" in (item.get("actions") or ()):
                return str(item.get("control_id") or "")
        for item in latest.get("controls") or []:
            if isinstance(item, dict) and "set_value" in (item.get("actions") or ()):
                return str(item.get("control_id") or "")
        return ""

    @classmethod
    def _latest_uia_observation_id(cls, payload: dict) -> str:
        observations: list[dict] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if value.get("uia_observation_id") and isinstance(value.get("controls"), list):
                    observations.append(value)
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)
            elif isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError):
                    return
                if decoded != value:
                    visit(decoded)

        visit(payload.get("input") or [])
        return str(observations[-1].get("uia_observation_id") or "") if observations else ""

    def __call__(self, payload: dict, _api_key: str) -> dict:
        self._pump()
        if self.stage == 0:
            call = {}
            name = "desktop_capture_state"
        elif self.stage == 1:
            self._refocus_input()
            call = {"window_handle": self.target_hwnd, "max_elements": 80}
            name = "desktop_uia_observe"
        elif self.stage == 2:
            observation_id = self._latest_uia_observation_id(payload)
            control_id = self._last_edit_control(payload)
            self.last_uia_observation_id = observation_id
            self.last_uia_control_id = control_id
            call = {
                "control_id": control_id,
                "window_handle": self.target_hwnd,
                "uia_observation_id": observation_id,
                "value": "DESKORB_DESKTOP_E2E",
            }
            name = "desktop_uia_set_value"
        elif self.stage == 3:
            return {"output_text": "已通过 UIA 值回读和重新观察验证。", "output": []}
        else:
            return {"output_text": "已输入并通过桌面状态基线验证。", "output": []}
        self.stage += 1
        return {"output": [{"type": "function_call", "call_id": f"desktop-{self.stage}",
                            "name": name, "arguments": json.dumps(call, ensure_ascii=False)}]}


def main() -> int:
    root = NativeEditFixture()
    entry = root
    root.update_idletasks()
    root.deiconify()
    root.lift()
    root.focus_force()
    entry.focus_force()
    root.update()
    target_child_hwnd = int(root.edit_hwnd or 0)
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
            # The production runtime publishes bounded tool telemetry through
            # _publish_tool_result.  Keep the desktop probe on that stable
            # interface instead of the removed browser-era dispatcher hook.
            original_publish = runtime._publish_tool_result

            def publish_tool_result(name, arguments, result):
                if isinstance(result, dict):
                    tool_results.append({
                        "tool": str(name)[:48], "ok": bool(result.get("ok")),
                        "snapshot_id": str(arguments.get("snapshot_id") or "")[:16],
                        "screen_changed": result.get("screen_changed"),
                        "active_window_changed": result.get("active_window_changed"),
                        "verified": bool(result.get("verified")),
                        "error": str(result.get("error") or "")[:120],
                    })
                return original_publish(name, arguments, result)

            runtime._publish_tool_result = publish_tool_result
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
            uia_runtime_value_readback = any(
                item.get("tool") == "desktop_uia_set_value"
                and item.get("ok") and item.get("verified")
                for item in tool_results
            )
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
                "script_uia_observation_id": scripted.last_uia_observation_id,
                "script_uia_control_id": scripted.last_uia_control_id,
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
                "uia_value_readback": bool(uia_value_readback or uia_runtime_value_readback),
                "uia_controls_summary": [
                    {key: item.get(key) for key in ("control_id", "name", "control_type", "actions")}
                    for item in uia_probe.get("controls", [])[:24]
                    if isinstance(item, dict)
                ],
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
