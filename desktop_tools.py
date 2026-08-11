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
        self._window_snapshot: dict[int, WindowSnapshot] = {}
        self._window_snapshot_at = 0.0
        self.target_window: int | None = None
        self.overlay_window: int | None = None
        self.overlay_windows: set[int] = set()
        self._current_desktop_authorized = False
        self._action_baseline: DesktopSnapshot | None = None
        self.user32 = ctypes.windll.user32 if os.name == "nt" else None
        self.kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None

    def set_target_window(self, hwnd: int) -> dict:
        """Restrict future desktop observations and actions to one HWND."""
        try:
            value = int(hwnd)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Target window handle must be an integer."}
        if value <= 0:
            return {"ok": False, "error": "Target window handle must be positive."}
        if self.user32 is not None:
            try:
                if not self.user32.IsWindow(value):
                    return {"ok": False, "error": "Target window handle is not a live window."}
            except (AttributeError, OSError):
                return {"ok": False, "error": "Target window could not be validated."}
        self.target_window = value
        self._current_desktop_authorized = False
        self._action_baseline = None
        self.snapshot = None
        self._window_snapshot.clear()
        self._window_snapshot_at = 0.0
        return {"ok": True, "target_window": value}

    def set_current_desktop_authorization(self, authorized: bool) -> None:
        """Enable explicit harness-only recovery from an unrelated foreground HWND."""
        self._current_desktop_authorized = bool(authorized)

    def clear_target_window(self) -> None:
        self.target_window = None
        self.overlay_window = None
        self.overlay_windows.clear()
        self._current_desktop_authorized = False
        self._action_baseline = None
        self.snapshot = None
        self._window_snapshot.clear()
        self._window_snapshot_at = 0.0

    @staticmethod
    def _top_level_window(user32: object, hwnd: int) -> int:
        """Normalize child/owned foreground handles to one top-level HWND."""
        try:
            get_ancestor = getattr(user32, "GetAncestor", None)
            if callable(get_ancestor):
                return int(get_ancestor(int(hwnd), 2) or int(hwnd))
        except Exception:
            pass
        return int(hwnd or 0)

    def set_overlay_window(self, hwnd: int | None) -> None:
        """Remember a temporary test/interaction overlay allowed to hand back focus."""
        try:
            value = int(hwnd or 0)
        except (TypeError, ValueError):
            value = 0
        self.overlay_window = value if value > 0 else None
        self.overlay_windows = {value} if value > 0 else set()

    def add_overlay_window(self, hwnd: int | None) -> None:
        """Add another known runner/overlay HWND without widening the focus boundary."""
        try:
            value = int(hwnd or 0)
        except (TypeError, ValueError):
            return
        if value > 0:
            self.overlay_windows.add(value)

    def _foreground_matches_target(self, foreground: int, target: int) -> bool:
        """Accept a child/owned foreground handle for the locked target window."""
        foreground = int(foreground or 0)
        target = int(target or 0)
        return foreground == target or (
            foreground > 0
            and target > 0
            and self._top_level_window(self.user32, foreground)
            == self._top_level_window(self.user32, target)
        )

    def _activate_with_thread_input(self, target: int, current: int) -> bool:
        """Use the Windows foreground-lock fallback within the existing boundary."""
        if not self.user32 or not current or current == target:
            return self._foreground_matches_target(current, target)
        try:
            get_thread = getattr(self.user32, "GetWindowThreadProcessId", None)
            attach_input = getattr(self.user32, "AttachThreadInput", None)
            set_foreground = getattr(self.user32, "SetForegroundWindow", None)
            if not callable(get_thread) or not callable(attach_input) or not callable(set_foreground):
                return False
            get_thread.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
            get_thread.restype = ctypes.wintypes.DWORD
            attach_input.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.BOOL]
            attach_input.restype = ctypes.wintypes.BOOL
            target_pid = ctypes.wintypes.DWORD()
            target_thread = int(get_thread(target, ctypes.byref(target_pid)) or 0)
            if not target_thread:
                return False
            peek_message = getattr(self.user32, "PeekMessageW", None)
            if callable(peek_message):
                try:
                    peek_message.argtypes = [ctypes.c_void_p, ctypes.wintypes.HWND, ctypes.wintypes.UINT,
                                             ctypes.wintypes.UINT, ctypes.wintypes.UINT]
                    peek_message.restype = ctypes.wintypes.BOOL
                    peek_message(None, 0, 0, 0, 0)
                except Exception:
                    pass
            thread_candidates: list[int] = []
            get_current_thread = getattr(self.kernel32, "GetCurrentThreadId", None)
            if callable(get_current_thread):
                try:
                    get_current_thread.restype = ctypes.wintypes.DWORD
                    thread_candidates.append(int(get_current_thread() or 0))
                except Exception:
                    pass
            current_pid = ctypes.wintypes.DWORD()
            foreground_thread = int(get_thread(current, ctypes.byref(current_pid)) or 0)
            thread_candidates.append(foreground_thread)
            seen: set[int] = set()
            for source_thread in thread_candidates:
                if not source_thread or source_thread == target_thread or source_thread in seen:
                    continue
                seen.add(source_thread)
                attached = bool(attach_input(source_thread, target_thread, True))
                if not attached:
                    continue
                try:
                    bring_to_top = getattr(self.user32, "BringWindowToTop", None)
                    if callable(bring_to_top):
                        bring_to_top(target)
                    set_foreground(target)
                    set_active = getattr(self.user32, "SetActiveWindow", None)
                    if callable(set_active):
                        set_active(target)
                    set_focus = getattr(self.user32, "SetFocus", None)
                    if callable(set_focus):
                        set_focus(target)
                finally:
                    attach_input(source_thread, target_thread, False)
                deadline = time.monotonic() + 0.75
                while time.monotonic() < deadline:
                    foreground = int(self.user32.GetForegroundWindow() or 0)
                    if self._foreground_matches_target(foreground, target):
                        return True
                    time.sleep(0.02)
                foreground = int(self.user32.GetForegroundWindow() or 0)
                if self._foreground_matches_target(foreground, target):
                    return True
            return self._activate_with_alt_wakeup(target)
        except Exception:
            return False

    def _activate_with_alt_wakeup(self, target: int) -> bool:
        """Wake Windows' foreground permission before one bounded retry."""
        if not self.user32:
            return False
        keybd_event = getattr(self.user32, "keybd_event", None)
        set_foreground = getattr(self.user32, "SetForegroundWindow", None)
        if not callable(keybd_event) or not callable(set_foreground):
            return False
        try:
            keybd_event.argtypes = [ctypes.wintypes.BYTE, ctypes.wintypes.BYTE,
                                    ctypes.wintypes.DWORD, ctypes.c_void_p]
            keybd_event.restype = None
            keybd_event(0x12, 0, 0, 0)       # VK_MENU down.
            keybd_event(0x12, 0, 0x0002, 0)  # VK_MENU up.
            bring_to_top = getattr(self.user32, "BringWindowToTop", None)
            if callable(bring_to_top):
                bring_to_top(target)
            set_foreground(target)
            deadline = time.monotonic() + 0.75
            while time.monotonic() < deadline:
                foreground = int(self.user32.GetForegroundWindow() or 0)
                if self._foreground_matches_target(foreground, target):
                    return True
                time.sleep(0.02)
            foreground = int(self.user32.GetForegroundWindow() or 0)
            return self._foreground_matches_target(foreground, target)
        except Exception:
            return False

    def focus_target(self, hwnd: int) -> dict:
        """Return focus to the configured target only from the known overlay window.

        The foreground-window check is deliberate: it prevents a stale desktop
        action from stealing focus from an unrelated application after the
        user has interacted with the desktop.
        """
        if not self.user32:
            return {"ok": False, "error": "Desktop controls require Windows."}
        try:
            target = int(hwnd)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Target window handle must be an integer."}
        if target <= 0:
            return {"ok": False, "error": "Target window handle must be positive."}
        if self.target_window is not None and target != self.target_window:
            return {"ok": False, "error": "Target window is outside the configured desktop boundary."}
        try:
            if not self.user32.IsWindow(target):
                return {"ok": False, "error": "The target window is no longer available; observe again."}
            is_visible = getattr(self.user32, "IsWindowVisible", None)
            if callable(is_visible) and not is_visible(target):
                return {"ok": False, "error": "The target window is not visible; observe again."}
            current = int(self.user32.GetForegroundWindow() or 0)
            if current != target:
                if (not self._current_desktop_authorized
                        and (not self.overlay_windows or current not in self.overlay_windows)):
                    return {"ok": False,
                            "error": "Active window changed since the snapshot; focus the intended application and observe again."}
                show_window = getattr(self.user32, "ShowWindow", None)
                if callable(show_window):
                    show_window(target, 9)  # SW_RESTORE
                bring_to_top = getattr(self.user32, "BringWindowToTop", None)
                if callable(bring_to_top):
                    bring_to_top(target)
                result = self.user32.SetForegroundWindow(target)
                foreground_after = int(self.user32.GetForegroundWindow() or 0)
                verified = self._foreground_matches_target(foreground_after, target)
                if not verified:
                    verified = self._activate_with_thread_input(target, current)
                if not verified and result is not None and not bool(result):
                    return {"ok": False, "error": "Windows rejected the target window focus."}
            else:
                verified = True
            if not verified:
                return {"ok": False,
                        "error": "Windows did not activate the target window; retry after focusing it manually."}
            return {"ok": True, "window_handle": target, "verified": True}
        except Exception as exc:
            return {"ok": False, "error": f"Could not activate the target window: {exc}"}

    def capture_state(self) -> dict:
        if not self.user32:
            return {"ok": False, "error": "Desktop controls require Windows."}
        point = ctypes.wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        if self.target_window is not None:
            current_getter = getattr(self.user32, "GetForegroundWindow", None)
            current = int(current_getter() or 0) if callable(current_getter) else 0
            target = int(self.target_window)
            if current and self._top_level_window(self.user32, current) != self._top_level_window(self.user32, target):
                focused = self.focus_target(target)
                if not focused.get("ok"):
                    return focused
        hwnd = foreground_capture_window()
        if self.target_window is not None:
            target = int(self.target_window)
            if int(hwnd or 0) != target:
                current_getter = getattr(self.user32, "GetForegroundWindow", None)
                current = int(current_getter() or 0) if callable(current_getter) else 0
                if not current or self._top_level_window(self.user32, current) != self._top_level_window(self.user32, target):
                    return {"ok": False, "error": "Target window is not the foreground window."}
                # Windows can report a child/owned foreground handle while
                # the target boundary was recorded against its top-level
                # ancestor. Keep evidence tied to the configured target.
                hwnd = target
        digest = ""
        try:
            image = ImageGrab.grab()
            image.thumbnail((320, 180))
            digest = hashlib.sha256(image.tobytes()).hexdigest()[:16]
        except Exception:
            pass
        state = DesktopSnapshot(secrets.token_hex(4).upper(), time.monotonic(), point.x, point.y,
                                int(hwnd or 0), window_title(hwnd) if hwnd else "", digest)
        self.snapshot = state
        return {"ok": True, "snapshot_id": state.snapshot_id, "cursor": {"x": point.x, "y": point.y},
                "active_window": state.active_title, "screen_digest": state.screen_digest}

    def _remember_action_baseline(self, snapshot_id: str) -> None:
        """Keep the pre-action snapshot while auto-observation advances ``snapshot``."""
        state = self.snapshot
        if state is not None and state.snapshot_id == str(snapshot_id):
            self._action_baseline = state

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
        self._remember_action_baseline(snapshot_id)
        if not self.user32.SetCursorPos(x, y):
            return {"ok": False, "error": "Windows rejected the cursor move."}
        flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010),
                 "middle": (0x0020, 0x0040)}
        down, up = flags[button]
        for _ in range(clicks):
            self.user32.mouse_event(down, 0, 0, 0, 0)
            self.user32.mouse_event(up, 0, 0, 0, 0)
        return {"ok": True, "clicked": {"x": x, "y": y, "button": button, "count": clicks}}

    def type_text(self, snapshot_id: str, text: str) -> dict:
        error = self._valid(snapshot_id, require_same_target=True)
        if error:
            return {"ok": False, "error": error}
        text = str(text)
        if not text or len(text) > 4000 or "\x00" in text:
            return {"ok": False, "error": "Text must contain 1-4000 characters."}
        self._remember_action_baseline(snapshot_id)
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
                        "error": "Windows accepted only part of the keyboard input."}
        return {"ok": True, "characters": len(text), "events_sent": sent_total}

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
        self._remember_action_baseline(snapshot_id)
        pressed = []
        try:
            for vk in virtual:
                self.user32.keybd_event(vk, 0, 0, 0)
                pressed.append(vk)
        finally:
            for vk in reversed(pressed):
                self.user32.keybd_event(vk, 0, 0x0002, 0)
        return {"ok": True, "keys": normalized}

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
        self._remember_action_baseline(snapshot_id)
        flag = 0x0800 if axis == "vertical" else 0x1000
        self.user32.mouse_event(flag, 0, 0, ctypes.c_ulong(amount * 120).value, 0)
        return {"ok": True, "delta": amount, "axis": axis}

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
        return {"ok": self.user32.GetForegroundWindow() == hwnd, "title": title}

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
        if self.target_window is not None:
            windows = [item for item in windows if item.window_id == self.target_window]
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
                return {"ok": True, "window_id": hwnd, "action": action, "title": item.title}
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
        return {"ok": True, "window_id": hwnd, "action": action, "title": item.title,
                "after": self._window_bounds(hwnd)}

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
        state = self.snapshot
        baseline = self._action_baseline
        valid_ids = {str(state.snapshot_id) if state else ""}
        if baseline is not None:
            valid_ids.add(str(baseline.snapshot_id))
        if not state or str(snapshot_id) not in valid_ids:
            return {"ok": False, "error": "Unknown desktop snapshot."}
        current = self.capture_state()
        if not current.get("ok"):
            return current
        reference = baseline if baseline is not None and str(snapshot_id) in {
            str(baseline.snapshot_id), str(state.snapshot_id)
        } else state
        active_changed = current["active_window"] != reference.active_title
        screen_changed = bool(reference.screen_digest and current["screen_digest"]
                              and current["screen_digest"] != reference.screen_digest)
        verified = bool(active_changed or screen_changed)
        self._action_baseline = None
        return {"ok": True, "active_window_changed": active_changed,
                "screen_changed": screen_changed, "verified": verified,
                "after": current}

    def _valid(self, snapshot_id: str, *, require_same_target: bool = False) -> str | None:
        state = self.snapshot
        if not self.user32:
            return "Desktop controls require Windows."
        if not state or snapshot_id != state.snapshot_id:
            return "Unknown desktop snapshot; capture fresh state first."
        if self.target_window is not None and state.active_hwnd != self.target_window:
            return "Desktop snapshot is not for the configured target window."
        if time.monotonic() - state.created_at > self.TTL_SECONDS:
            return "Desktop snapshot expired; capture fresh state first."
        if require_same_target and state.active_hwnd:
            current = int(self.user32.GetForegroundWindow() or 0)
            if current != state.active_hwnd:
                return "Active window changed since the snapshot; capture fresh state first."
        return None

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
        if self.target_window is not None and requested != self.target_window:
            return None, "Window is outside the configured target window."
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
