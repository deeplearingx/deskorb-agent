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
    active_title: str
    screen_digest: str


class DesktopTools:
    TTL_SECONDS = 30
    VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
          "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "backspace": 0x08,
          "delete": 0x2E, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}

    def __init__(self):
        self.snapshot: DesktopSnapshot | None = None
        self.user32 = ctypes.windll.user32 if os.name == "nt" else None

    def capture_state(self) -> dict:
        if not self.user32:
            return {"ok": False, "error": "Desktop controls require Windows."}
        point = ctypes.wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        hwnd = foreground_capture_window()
        digest = ""
        try:
            image = ImageGrab.grab()
            image.thumbnail((320, 180))
            digest = hashlib.sha256(image.tobytes()).hexdigest()[:16]
        except Exception:
            pass
        state = DesktopSnapshot(secrets.token_hex(4).upper(), time.monotonic(), point.x, point.y,
                                window_title(hwnd) if hwnd else "", digest)
        self.snapshot = state
        return {"ok": True, "snapshot_id": state.snapshot_id, "cursor": {"x": point.x, "y": point.y},
                "active_window": state.active_title, "screen_digest": state.screen_digest}

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

    def click(self, snapshot_id: str, x: int, y: int, button: str) -> dict:
        error = self._valid(snapshot_id)
        if error:
            return {"ok": False, "error": error}
        if button not in {"left", "right"}:
            return {"ok": False, "error": "Unsupported button."}
        self.user32.SetCursorPos(int(x), int(y))
        down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
        self.user32.mouse_event(down, 0, 0, 0, 0)
        self.user32.mouse_event(up, 0, 0, 0, 0)
        return {"ok": True, "clicked": {"x": int(x), "y": int(y), "button": button}}

    def type_text(self, snapshot_id: str, text: str) -> dict:
        error = self._valid(snapshot_id)
        if error:
            return {"ok": False, "error": error}
        text = str(text)
        if not text or len(text) > 4000:
            return {"ok": False, "error": "Text must contain 1-4000 characters."}
        units = [int.from_bytes(text.encode("utf-16-le")[i:i + 2], "little")
                 for i in range(0, len(text.encode("utf-16-le")), 2)]
        inputs = []
        for unit in units:
            inputs.extend((INPUT(1, INPUT_UNION(ki=KEYBDINPUT(0, unit, 0x0004, 0, 0))),
                           INPUT(1, INPUT_UNION(ki=KEYBDINPUT(0, unit, 0x0004 | 0x0002, 0, 0)))))
        array = (INPUT * len(inputs))(*inputs)
        sent = self.user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
        return {"ok": sent == len(inputs), "characters": len(text), "events_sent": int(sent)}

    def hotkey(self, snapshot_id: str, keys: list[str]) -> dict:
        error = self._valid(snapshot_id)
        if error:
            return {"ok": False, "error": error}
        normalized = [str(key).lower() for key in keys]
        if not 1 <= len(normalized) <= 5:
            return {"ok": False, "error": "A hotkey requires 1-5 keys."}
        virtual = []
        for key in normalized:
            if len(key) == 1 and (key.isascii() and key.isalnum()):
                virtual.append(ord(key.upper()))
            elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
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
        return {"ok": True, "keys": normalized}

    def scroll(self, snapshot_id: str, delta: int) -> dict:
        error = self._valid(snapshot_id)
        if error:
            return {"ok": False, "error": error}
        amount = max(-10, min(10, int(delta)))
        if not amount:
            return {"ok": False, "error": "Scroll delta cannot be zero."}
        self.user32.mouse_event(0x0800, 0, 0, ctypes.c_ulong(amount * 120).value, 0)
        return {"ok": True, "delta": amount}

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

    def verify_state(self, snapshot_id: str) -> dict:
        state = self.snapshot
        if not state or state.snapshot_id != snapshot_id:
            return {"ok": False, "error": "Unknown desktop snapshot."}
        current = self.capture_state()
        if not current.get("ok"):
            return current
        return {"ok": True, "active_window_changed": current["active_window"] != state.active_title,
                "screen_changed": bool(state.screen_digest and current["screen_digest"] and current["screen_digest"] != state.screen_digest),
                "after": current}

    def _valid(self, snapshot_id: str) -> str | None:
        state = self.snapshot
        if not self.user32:
            return "Desktop controls require Windows."
        if not state or snapshot_id != state.snapshot_id:
            return "Unknown desktop snapshot; capture fresh state first."
        if time.monotonic() - state.created_at > self.TTL_SECONDS:
            return "Desktop snapshot expired; capture fresh state first."
        return None
