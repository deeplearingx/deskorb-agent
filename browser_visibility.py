"""Small Windows helpers for proving that the isolated browser is visible.

The MCP process does not expose a window handle.  These helpers therefore walk
the exact MCP process tree and inspect only top-level windows owned by those
processes.  No titles, URLs, or page content are returned to the runtime.
"""
from __future__ import annotations

import ctypes
import os
import time
from typing import Any


def _process_tree_ids(root_pid: int) -> set[int] | None:
    root = int(root_pid or 0)
    if root <= 0:
        return set()
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        class _ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_snapshot = kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create_snapshot.restype = ctypes.c_void_p
        snapshot = create_snapshot(0x00000002, 0)
        snapshot_value = int(getattr(snapshot, "value", snapshot) or 0)
        invalid = ctypes.c_void_p(-1).value
        if not snapshot_value or snapshot_value == invalid:
            return {root}
        first = kernel32.Process32FirstW
        next_process = kernel32.Process32NextW
        for function in (first, next_process):
            function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32W)]
            function.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = wintypes.BOOL
        parents: dict[int, int] = {}
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        try:
            if first(snapshot, ctypes.byref(entry)):
                while True:
                    parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                    if not next_process(snapshot, ctypes.byref(entry)):
                        break
        finally:
            close_handle(snapshot)
        result = {root}
        changed = True
        while changed and len(result) < 256:
            changed = False
            for pid, parent in parents.items():
                if parent in result and pid not in result:
                    result.add(pid)
                    changed = True
        return result
    except Exception:
        # A visibility probe is diagnostic only.  ``None`` lets the caller
        # distinguish an unavailable Win32 probe from a confirmed empty set.
        return None


def visible_browser_windows(root_pid: int) -> set[int] | None:
    """Return visible top-level HWNDs in the MCP process tree.

    ``None`` means that the host cannot perform a Win32 probe (for example a
    non-Windows test host); an empty set is an actual negative observation.
    """
    process_ids = _process_tree_ids(root_pid)
    if process_ids is None:
        return None
    if os.name != "nt" or not process_ids:
        return set()
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        is_visible = user32.IsWindowVisible
        is_visible.argtypes = [wintypes.HWND]
        is_visible.restype = wintypes.BOOL
        is_iconic = user32.IsIconic
        is_iconic.argtypes = [wintypes.HWND]
        is_iconic.restype = wintypes.BOOL
        get_pid = user32.GetWindowThreadProcessId
        get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        get_pid.restype = wintypes.DWORD
        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
        get_ancestor.restype = wintypes.HWND
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        enum_windows = user32.EnumWindows
        enum_windows.argtypes = [callback_type, wintypes.LPARAM]
        enum_windows.restype = wintypes.BOOL
        handles: set[int] = set()

        @callback_type
        def visit(hwnd: Any, _lparam: Any) -> bool:
            try:
                if not is_visible(hwnd) or is_iconic(hwnd):
                    return True
                process_id = wintypes.DWORD()
                if not get_pid(hwnd, ctypes.byref(process_id)):
                    return True
                if int(process_id.value) not in process_ids:
                    return True
                root = int(get_ancestor(hwnd, 2) or hwnd or 0)
                if root > 0:
                    handles.add(root)
            except Exception:
                return True
            return True

        enum_windows(visit, 0)
        return handles
    except Exception:
        return None


def focus_browser_window_once(hwnd: int) -> bool:
    """Attempt one foreground activation; callers must not retry repeatedly."""
    value = int(hwnd or 0)
    if os.name != "nt" or value <= 0:
        return False
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        show_window = user32.ShowWindow
        show_window.argtypes = [wintypes.HWND, ctypes.c_int]
        show_window.restype = wintypes.BOOL
        set_foreground = user32.SetForegroundWindow
        set_foreground.argtypes = [wintypes.HWND]
        set_foreground.restype = wintypes.BOOL
        show_window(value, 9)  # SW_RESTORE
        return bool(set_foreground(value))
    except Exception:
        return False


def flash_browser_window(hwnd: int) -> bool:
    """Ask Windows to flash a browser taskbar button when foreground is denied."""
    value = int(hwnd or 0)
    if os.name != "nt" or value <= 0:
        return False
    try:
        from ctypes import wintypes

        class _FlashInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        flash = user32.FlashWindowEx
        flash.argtypes = [ctypes.POINTER(_FlashInfo)]
        flash.restype = wintypes.BOOL
        info = _FlashInfo(ctypes.sizeof(_FlashInfo), value, 0x00000003, 2, 0)
        return bool(flash(ctypes.byref(info)))
    except Exception:
        return False


def wait_for_visible_browser(root_pid: int, timeout_seconds: float,
                             *, poll_seconds: float = 0.05) -> set[int] | None:
    """Poll the exact process tree until a visible window appears or a budget ends."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    latest: set[int] | None = set()
    while True:
        latest = visible_browser_windows(root_pid)
        if latest is None or latest:
            return latest
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return latest
        time.sleep(min(max(0.005, float(poll_seconds)), remaining))


__all__ = [
    "focus_browser_window_once", "flash_browser_window", "visible_browser_windows",
    "wait_for_visible_browser",
]
