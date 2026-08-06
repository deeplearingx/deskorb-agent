"""Windows desktop primitives guarded by short-lived state snapshots."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import base64
import io
import os
import secrets
import hashlib
import time
from dataclasses import dataclass

from win32utils import foreground_capture_window, window_title
from PIL import ImageGrab


ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.wintypes.WORD), ("wScan", ctypes.wintypes.WORD),
                ("dwFlags", ctypes.wintypes.DWORD), ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.wintypes.DWORD),
                ("dwFlags", ctypes.wintypes.DWORD), ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", INPUT_UNION)]


@dataclass(frozen=True)
class DesktopSnapshot:
    snapshot_id: str
    created_at: float
    cursor_x: int
    cursor_y: int
    active_hwnd: int
    active_title: str
    screen_digest: str


@dataclass(frozen=True)
class WindowSnapshot:
    window_id: int
    title: str
    process_id: int
    bounds: dict[str, int]
    minimized: bool
    maximized: bool


class DesktopTools:
    TTL_SECONDS = 30
    MAX_INPUT_EVENTS = 512
    MAX_WINDOWS = 80
    VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
          "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "backspace": 0x08,
          "space": 0x20, "delete": 0x2E, "insert": 0x2D,
          "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
          "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
          "printscreen": 0x2C}
    KEY_ALIASES = {"control": "ctrl", "option": "alt", "esc": "escape",
                   "return": "enter", "pgup": "pageup", "pgdn": "pagedown",
                   "del": "delete", "ins": "insert", "windows": "win"}

    def __init__(self):
        self.snapshot: DesktopSnapshot | None = None
        # Keep a tiny, in-memory history so a post-action verification can
        # compare the fresh screen with the snapshot captured immediately
        # before the action.  History is bounded and never persisted.
        self._snapshot_history: dict[str, DesktopSnapshot] = {}
        self.preferred_hwnd = 0
        self.overlay_hwnd = 0
        self._window_snapshot: dict[int, WindowSnapshot] = {}
        self._window_snapshot_at = 0.0
        if os.name == "nt":
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._configure_win32_signatures()
        else:
            self.user32 = None
            self.kernel32 = None

    def _configure_win32_signatures(self) -> None:
        """Declare pointer-sized Win32 signatures before sending input."""
        try:
            self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
            self.user32.SetCursorPos.restype = ctypes.wintypes.BOOL
            self.user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
            self.user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
            self.user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
            self.user32.SendInput.restype = ctypes.c_uint
            self.user32.mouse_event.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                                ctypes.c_uint, ULONG_PTR]
            self.user32.keybd_event.argtypes = [ctypes.wintypes.BYTE, ctypes.wintypes.BYTE,
                                                ctypes.c_uint, ULONG_PTR]
        except (AttributeError, TypeError):
            # Test doubles and restricted Win32 wrappers need no ctypes metadata.
            return

    @staticmethod
    def _win32_error(prefix: str) -> str:
        try:
            code = int(ctypes.get_last_error() or 0)
            detail = ctypes.FormatError(code).strip() if code else "unknown error"
            return f"{prefix} (Win32 error {code}: {detail})."
        except Exception:
            return prefix + "."

    def set_preferred_window(self, hwnd: int | None) -> None:
        """Remember the external window that was active before the overlay took focus."""
        try:
            self.preferred_hwnd = max(0, int(hwnd or 0))
        except (TypeError, ValueError):
            self.preferred_hwnd = 0

    def set_overlay_window(self, hwnd: int | None) -> None:
        """Identify the overlay HWND allowed to hand focus back to the target."""
        try:
            self.overlay_hwnd = max(0, int(hwnd or 0))
        except (TypeError, ValueError):
            self.overlay_hwnd = 0

    def resolve_target_window(self, hwnd: int | None = None) -> int:
        """Resolve a preferred external target, falling back to the live foreground window."""
        preferred = hwnd if hwnd is not None else self.preferred_hwnd
        return self._usable_window(preferred) or int(foreground_capture_window() or 0)

    def capture_state(self, target_hwnd: int | None = None) -> dict:
        if not self.user32:
            return {"ok": False, "error": "Desktop controls require Windows."}
        point = ctypes.wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        hwnd = self.resolve_target_window(target_hwnd)
        digest = ""
        try:
            image = ImageGrab.grab()
            image.thumbnail((320, 180))
            digest = hashlib.sha256(image.tobytes()).hexdigest()[:16]
        except Exception:
            pass
        previous = self.snapshot
        state = DesktopSnapshot(secrets.token_hex(4).upper(), time.monotonic(), point.x, point.y,
                                int(hwnd or 0), window_title(hwnd) if hwnd else "", digest)
        if previous is not None:
            self._snapshot_history[previous.snapshot_id] = previous
        self._snapshot_history[state.snapshot_id] = state
        cutoff = state.created_at - self.TTL_SECONDS
        self._snapshot_history = {
            key: value for key, value in self._snapshot_history.items()
            if value.created_at >= cutoff
        }
        if len(self._snapshot_history) > 32:
            newest = sorted(self._snapshot_history.values(),
                            key=lambda item: item.created_at, reverse=True)[:32]
            self._snapshot_history = {item.snapshot_id: item for item in newest}
        self.snapshot = state
        return {"ok": True, "snapshot_id": state.snapshot_id, "cursor": {"x": point.x, "y": point.y},
                "active_window": state.active_title, "screen_digest": state.screen_digest,
                "input_available": bool(state.active_hwnd),
                "input_error": None if state.active_hwnd else
                "No interactive foreground window is available; run DeskOrb in the signed-in user session."}

    def focus_target(self, hwnd: int | None) -> dict:
        """Activate a previously observed external window before input/UIA actions."""
        target = self._usable_window(hwnd)
        if not target:
            return {"ok": False, "error": "The target window is no longer available or visible; observe again."}
        try:
            current = int(self.user32.GetForegroundWindow() or 0)
            if current != target:
                if not self.overlay_hwnd or current != self.overlay_hwnd:
                    return {"ok": False,
                            "error": "Active window changed since the snapshot; focus the intended application and observe again."}
                if not bool(self.user32.SetForegroundWindow(target)):
                    return {"ok": False, "error": self._win32_error("Windows rejected the target window focus")}
                time.sleep(0.03)
            verified = int(self.user32.GetForegroundWindow() or 0) == target
            if not verified:
                return {"ok": False, "error": "Windows did not activate the target window; retry after focusing it manually."}
            return {"ok": True, "window_handle": target, "verified": True}
        except Exception as exc:
            return {"ok": False, "error": f"Could not activate the target window: {exc}"}

    @staticmethod
    def capture_image_data_url() -> str | None:
        """Capture a compact JPEG observation suitable for the next model tool round."""
        try:
            image = ImageGrab.grab(all_screens=True)
            image.thumbnail((1280, 800))
            if image.mode != "RGB":
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=72, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception:
            return None

    def click(self, snapshot_id: str, x: int, y: int, button: str, clicks: int = 1) -> dict:
        error = self._valid(snapshot_id, require_same_target=True)
        if error:
            return {"ok": False, "error": error}
        button = str(button).lower()
        if button not in {"left", "right", "middle"}:
            return {"ok": False, "error": "Unsupported button."}
        try:
            x, y, clicks = int(x), int(y), int(clicks)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Click coordinates and count must be integers."}
        if clicks not in {1, 2}:
            return {"ok": False, "error": "Click count must be 1 or 2."}
        if not self._in_virtual_screen(x, y):
            return {"ok": False, "error": "Click coordinates are outside the virtual desktop."}
        if not self.user32.SetCursorPos(x, y):
            return {"ok": False, "error": self._win32_error("Windows rejected the cursor move")}
        flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
                 "middle": (0x0020, 0x0040)}
        down, up = flags[button]
        for _ in range(clicks):
            self.user32.mouse_event(down, 0, 0, 0, 0)
            self.user32.mouse_event(up, 0, 0, 0, 0)
        return {"ok": True, "baseline_snapshot_id": snapshot_id,
                "clicked": {"x": x, "y": y, "button": button, "count": clicks}}

    def type_text(self, snapshot_id: str, text: str) -> dict:
        error = self._valid(snapshot_id, require_same_target=True)
        if error:
            return {"ok": False, "error": error}
        text = str(text)
        if not text or len(text) > 4000 or "\x00" in text:
            return {"ok": False, "error": "Text must contain 1-4000 characters."}
        encoded = text.encode("utf-16-le")
        units = [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]
        inputs = []
        for unit in units:
            inputs.extend((INPUT(1, INPUT_UNION(ki=KEYBDINPUT(0, unit, 0x0004, 0, 0))),
                           INPUT(1, INPUT_UNION(ki=KEYBDINPUT(0, unit, 0x0004 | 0x0002, 0, 0)))))
        sent_total = 0
        for offset in range(0, len(inputs), self.MAX_INPUT_EVENTS):
            chunk = inputs[offset:offset + self.MAX_INPUT_EVENTS]
            array = (INPUT * len(chunk))(*chunk)
            sent = int(self.user32.SendInput(len(chunk), array, ctypes.sizeof(INPUT)))
            sent_total += max(0, sent)
            if sent != len(chunk):
                return {"ok": False, "characters": len(text), "events_sent": sent_total,
                        "error": self._win32_error("Windows accepted only part of the keyboard input")}
        return {"ok": True, "baseline_snapshot_id": snapshot_id,
                "characters": len(text), "events_sent": sent_total}

    def hotkey(self, snapshot_id: str, keys: list[str]) -> dict:
        error = self._valid(snapshot_id, require_same_target=True)
        if error:
            return {"ok": False, "error": error}
        normalized = [self.KEY_ALIASES.get(str(key).strip().lower(), str(key).strip().lower()) for key in keys]
        if not 1 <= len(normalized) <= 5:
            return {"ok": False, "error": "A hotkey requires 1-5 keys."}
        if len(set(normalized)) != len(normalized):
            return {"ok": False, "error": "A hotkey cannot repeat a key."}
        virtual = []
        for key in normalized:
            if len(key) == 1 and (key.isascii() and key.isalnum()):
                virtual.append(ord(key.upper()))
            elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
                virtual.append(0x6F + int(key[1:]))
            elif key in self.VK:
                virtual.append(self.VK[key])
            else:
                return {"ok": False, "error": f"Unsupported hotkey key: {key}"}
        pressed = []
        try:
            for vk in virtual:
                self.user32.keybd_event(vk, 0, 0, 0)
                pressed.append(vk)
        finally:
            for vk in reversed(pressed):
                self.user32.keybd_event(vk, 0, 0x0002, 0)
        return {"ok": True, "baseline_snapshot_id": snapshot_id, "keys": normalized}

    def scroll(self, snapshot_id: str, delta: int, axis: str = "vertical") -> dict:
        error = self._valid(snapshot_id, require_same_target=True)
        if error:
            return {"ok": False, "error": error}
        try:
            amount = max(-10, min(10, int(delta)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Scroll delta must be an integer."}
        if not amount:
            return {"ok": False, "error": "Scroll delta cannot be zero."}
        axis = str(axis).lower()
        if axis not in {"vertical", "horizontal"}:
            return {"ok": False, "error": "Scroll axis must be vertical or horizontal."}
        flag = 0x0800 if axis == "vertical" else 0x1000
        self.user32.mouse_event(flag, 0, 0, ctypes.c_ulong(amount * 120).value, 0)
        return {"ok": True, "baseline_snapshot_id": snapshot_id,
                "delta": amount, "axis": axis}

    def focus_window(self, title: str) -> dict:
        if not self.user32:
            return {"ok": False, "error": "Desktop controls require Windows."}
        title = str(title).strip()
        if not title:
            return {"ok": False, "error": "Window title is empty."}
        hwnd = self.user32.FindWindowW(None, title)
        if not hwnd:
            return {"ok": False, "error": "Exact window title was not found."}
        self.user32.ShowWindow(hwnd, 9)
        self.user32.SetForegroundWindow(hwnd)
        verified = self.user32.GetForegroundWindow() == hwnd
        return {"ok": verified, "verified": verified, "title": title,
                "verification": {"passed": verified, "kind": "foreground_window"}}

    def list_windows(self) -> dict:
        """Return a short-lived inventory of normal visible top-level windows."""
        if not self.user32:
            return {"ok": False, "error": "Desktop controls require Windows."}
        windows: list[WindowSnapshot] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def visit(hwnd, _lparam):
            try:
                if not self.user32.IsWindowVisible(hwnd):
                    return True
                length = int(self.user32.GetWindowTextLengthW(hwnd))
                if length <= 0 or length > 512:
                    return True
                text = ctypes.create_unicode_buffer(length + 1)
                self.user32.GetWindowTextW(hwnd, text, len(text))
                title = text.value.strip()
                if not title:
                    return True
                rect = ctypes.wintypes.RECT()
                if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                if rect.right <= rect.left or rect.bottom <= rect.top:
                    return True
                process_id = ctypes.wintypes.DWORD()
                self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                windows.append(WindowSnapshot(
                    int(hwnd), title, int(process_id.value),
                    {"x": int(rect.left), "y": int(rect.top), "width": int(rect.right - rect.left),
                     "height": int(rect.bottom - rect.top)},
                    bool(self.user32.IsIconic(hwnd)), bool(self.user32.IsZoomed(hwnd)),
                ))
            except Exception:
                return True
            return len(windows) < self.MAX_WINDOWS

        try:
            self.user32.EnumWindows(visit, 0)
        except Exception as exc:
            return {"ok": False, "error": f"Could not enumerate windows: {exc}"}
        windows.sort(key=lambda item: item.title.lower())
        self._window_snapshot = {item.window_id: item for item in windows}
        self._window_snapshot_at = time.monotonic()
        return {"ok": True, "windows": [self._window_dict(item) for item in windows],
                "expires_in_seconds": self.TTL_SECONDS}

    def control_window(self, window_id: int, action: str, x: int | None = None, y: int | None = None,
                       width: int | None = None, height: int | None = None) -> dict:
        """Control a window selected from a fresh ``list_windows`` result."""
        item, error = self._valid_window(window_id)
        if error:
            return {"ok": False, "error": error}
        assert item is not None
        action = str(action or "").strip().lower()
        hwnd = item.window_id
        before_topmost = self._is_topmost(hwnd)
        try:
            if action == "focus":
                self.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                self.user32.SetForegroundWindow(hwnd)
            elif action == "minimize":
                self.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            elif action == "maximize":
                self.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            elif action == "restore":
                self.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            elif action == "close":
                self.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                verified = not bool(self.user32.IsWindow(hwnd))
                return {"ok": True, "verified": verified, "window_id": hwnd, "action": action,
                        "title": item.title, "verification": {"passed": verified, "kind": "window_closed"}}
            elif action == "toggle_topmost":
                exstyle = int(self.user32.GetWindowLongW(hwnd, -20))  # GWL_EXSTYLE
                topmost = not bool(exstyle & 0x00000008)               # WS_EX_TOPMOST
                self.user32.SetWindowPos(hwnd, -1 if topmost else -2, 0, 0, 0, 0,
                                         0x0001 | 0x0002 | 0x0040)      # no size/move, show
            elif action in {"snap_left", "snap_right"}:
                area = self._work_area(hwnd)
                half = max(1, area["width"] // 2)
                target_x = area["x"] if action == "snap_left" else area["x"] + area["width"] - half
                self.user32.SetWindowPos(hwnd, 0, target_x, area["y"], half, area["height"],
                                         0x0004 | 0x0040)  # no z-order, show
            elif action == "move_resize":
                values = (x, y, width, height)
                if any(value is None for value in values):
                    return {"ok": False, "error": "move_resize requires x, y, width, and height."}
                px, py, pw, ph = (int(value) for value in values)
                if pw < 100 or ph < 80 or not self._in_virtual_screen(px, py):
                    return {"ok": False, "error": "Window bounds are invalid or outside the virtual desktop."}
                self.user32.SetWindowPos(hwnd, 0, px, py, pw, ph, 0x0004 | 0x0040)
            else:
                return {"ok": False, "error": "Unsupported window action."}
        except (TypeError, ValueError, OSError) as exc:
            return {"ok": False, "error": f"Window action failed: {exc}"}
        after = self._window_bounds(hwnd)
        after_topmost = self._is_topmost(hwnd)
        verified = self._verify_window_action(hwnd, action, item.bounds, after,
                                               before_topmost=before_topmost,
                                               after_topmost=after_topmost,
                                               x=x, y=y, width=width, height=height)
        return {"ok": True, "verified": verified, "window_id": hwnd, "action": action, "title": item.title,
                "after": after, "verification": {"passed": verified, "kind": "window_state"}}

    def clipboard_text(self) -> dict:
        """Read Unicode clipboard text only after the runtime's explicit confirmation gate."""
        if not (self.user32 and self.kernel32):
            return {"ok": False, "error": "Clipboard controls require Windows."}
        self.user32.GetClipboardData.restype = ctypes.c_void_p
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        if not self.user32.OpenClipboard(None):
            return {"ok": False, "error": "Clipboard is currently busy."}
        try:
            handle = self.user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return {"ok": False, "error": "Clipboard does not contain Unicode text."}
            pointer = self.kernel32.GlobalLock(handle)
            if not pointer:
                return {"ok": False, "error": "Could not access clipboard text."}
            try:
                value = ctypes.wstring_at(pointer)
            finally:
                self.kernel32.GlobalUnlock(handle)
            cap = 32_000
            return {"ok": True, "text": value[:cap], "truncated": len(value) > cap,
                    "characters": len(value)}
        finally:
            self.user32.CloseClipboard()

    def verify_state(self, snapshot_id: str) -> dict:
        state = self._snapshot_history.get(str(snapshot_id))
        if state is None and self.snapshot and self.snapshot.snapshot_id == str(snapshot_id):
            state = self.snapshot
        if not state:
            return {"ok": False, "error": "Unknown desktop snapshot."}
        if time.monotonic() - state.created_at > self.TTL_SECONDS:
            return {"ok": False, "error": "Desktop snapshot expired; capture fresh state first."}
        current = self.capture_state(self.preferred_hwnd)
        if not current.get("ok"):
            return current
        return {"ok": True, "baseline_snapshot_id": state.snapshot_id,
                "active_window_changed": current["active_window"] != state.active_title,
                "screen_changed": bool(state.screen_digest and current["screen_digest"] and current["screen_digest"] != state.screen_digest),
                "after": current}

    def _valid(self, snapshot_id: str, *, require_same_target: bool = False) -> str | None:
        state = self.snapshot
        if not self.user32:
            return "Desktop controls require Windows."
        if not state or snapshot_id != state.snapshot_id:
            return "Unknown desktop snapshot; capture fresh state first."
        if time.monotonic() - state.created_at > self.TTL_SECONDS:
            return "Desktop snapshot expired; capture fresh state first."
        if require_same_target and state.active_hwnd:
            current = int(self.user32.GetForegroundWindow() or 0)
            if current != state.active_hwnd:
                focused = self.focus_target(state.active_hwnd)
                if not focused.get("ok"):
                    return "Active window changed since the snapshot and could not be reactivated; capture fresh state first."
        return None

    def _usable_window(self, hwnd: int | None) -> int:
        try:
            value = int(hwnd or 0)
        except (TypeError, ValueError):
            return 0
        if not value:
            return 0
        try:
            if hasattr(self.user32, "IsWindow") and not self.user32.IsWindow(value):
                return 0
            if hasattr(self.user32, "IsWindowVisible") and not self.user32.IsWindowVisible(value):
                return 0
            if hasattr(self.user32, "IsIconic") and self.user32.IsIconic(value):
                return 0
        except Exception:
            return 0
        return value

    def _valid_window(self, window_id: int) -> tuple[WindowSnapshot | None, str | None]:
        if not self.user32:
            return None, "Desktop controls require Windows."
        try:
            requested = int(window_id)
        except (TypeError, ValueError):
            return None, "window_id must come from a fresh window list."
        if time.monotonic() - self._window_snapshot_at > self.TTL_SECONDS:
            return None, "Window list expired; list windows again before acting."
        item = self._window_snapshot.get(requested)
        if not item or not self.user32.IsWindow(requested):
            return None, "Unknown or closed window; list windows again before acting."
        if window_title(requested) != item.title:
            return None, "Window changed since listing; list windows again before acting."
        return item, None

    def _window_bounds(self, hwnd: int) -> dict[str, int] | None:
        try:
            rect = ctypes.wintypes.RECT()
            if self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return {"x": int(rect.left), "y": int(rect.top), "width": int(rect.right - rect.left),
                        "height": int(rect.bottom - rect.top)}
        except Exception:
            pass
        return None

    def _verify_window_action(self, hwnd: int, action: str, before: dict[str, int],
                              after: dict[str, int] | None, *, before_topmost: bool | None = None,
                              after_topmost: bool | None = None, x: int | None = None,
                              y: int | None = None, width: int | None = None,
                              height: int | None = None) -> bool:
        """Read back the state changed by a window action whenever Win32 exposes it."""
        try:
            if action == "focus":
                return int(self.user32.GetForegroundWindow() or 0) == hwnd
            if action == "minimize":
                return bool(self.user32.IsIconic(hwnd))
            if action == "maximize":
                return bool(self.user32.IsZoomed(hwnd))
            if action == "restore":
                return not bool(self.user32.IsIconic(hwnd)) and not bool(self.user32.IsZoomed(hwnd))
            if action == "toggle_topmost":
                if before_topmost is not None and after_topmost is not None:
                    return after_topmost is not before_topmost
                return after_topmost is True
            if action == "move_resize" and after is not None:
                return after == {"x": int(x), "y": int(y), "width": int(width), "height": int(height)}
            if action in {"snap_left", "snap_right"}:
                return bool(after and (after["width"] != before.get("width") or after["x"] != before.get("x")))
        except (AttributeError, TypeError, ValueError, OSError):
            return False
        return bool(after)

    def _is_topmost(self, hwnd: int) -> bool | None:
        try:
            exstyle = int(self.user32.GetWindowLongW(hwnd, -20))
            return bool(exstyle & 0x00000008)
        except (AttributeError, TypeError, ValueError, OSError):
            return None

    def _work_area(self, hwnd: int) -> dict[str, int]:
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.wintypes.DWORD), ("rcMonitor", ctypes.wintypes.RECT),
                        ("rcWork", ctypes.wintypes.RECT), ("dwFlags", ctypes.wintypes.DWORD)]
        try:
            monitor = self.user32.MonitorFromWindow(hwnd, 2)  # nearest monitor
            info = MONITORINFO(ctypes.sizeof(MONITORINFO))
            if monitor and self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.rcWork
                return {"x": int(rect.left), "y": int(rect.top), "width": int(rect.right - rect.left),
                        "height": int(rect.bottom - rect.top)}
        except Exception:
            pass
        return {"x": 0, "y": 0, "width": max(1, int(self.user32.GetSystemMetrics(0))),
                "height": max(1, int(self.user32.GetSystemMetrics(1)))}

    @staticmethod
    def _window_dict(item: WindowSnapshot) -> dict:
        return {"window_id": item.window_id, "title": item.title, "process_id": item.process_id,
                "bounds": item.bounds, "minimized": item.minimized, "maximized": item.maximized}

    def _in_virtual_screen(self, x: int, y: int) -> bool:
        """Reject stale or hallucinated coordinates before moving the real cursor."""
        try:
            left = int(self.user32.GetSystemMetrics(76))
            top = int(self.user32.GetSystemMetrics(77))
            width = int(self.user32.GetSystemMetrics(78))
            height = int(self.user32.GetSystemMetrics(79))
            if width > 0 and height > 0:
                return left <= x < left + width and top <= y < top + height
        except AttributeError:
            pass
        return -32768 <= x <= 32767 and -32768 <= y <= 32767
