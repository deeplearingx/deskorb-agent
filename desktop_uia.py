"""Optional Windows UI Automation observer and semantic control invoker.

Coordinates are a last resort.  When pywinauto's UIA backend is installed this
module exposes the current app's named controls with a short-lived identifier,
then invokes that exact control after validating the foreground window again.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

try:  # Optional until the next setup/install; importing DeskOrb must still work.
    from pywinauto import Desktop
except ImportError:  # pragma: no cover - covered by the returned availability result.
    Desktop = None


@dataclass
class CachedControl:
    wrapper: Any
    hwnd: int
    created_at: float


class DesktopUIA:
    TTL_SECONDS = 12

    def __init__(self, desktop_factory=None, clock=time.monotonic):
        self._desktop_factory = desktop_factory or Desktop
        self._clock = clock
        self._controls: dict[str, CachedControl] = {}

    @property
    def available(self) -> bool:
        return self._desktop_factory is not None

    def observe_active_window(self, hwnd: int, *, max_elements: int = 80) -> dict[str, Any]:
        if not self.available:
            return {"ok": False, "error": "Windows UI Automation is unavailable. Install pywinauto and restart DeskOrb."}
        if not hwnd:
            return {"ok": False, "error": "No active window is available for UI Automation."}
        try:
            window = self._desktop_factory(backend="uia").window(handle=hwnd)
            descendants = window.descendants()
        except Exception as exc:
            return {"ok": False, "error": f"Could not inspect the active window with UI Automation: {exc}"}
        self._prune()
        controls: list[dict[str, Any]] = []
        for wrapper in descendants:
            if len(controls) >= max(1, min(int(max_elements), 200)):
                break
            item = self._describe(wrapper, hwnd)
            if item is not None:
                controls.append(item)
        dialogs = [item for item in controls if str(item.get("control_type", "")).lower() in {"dialog", "window"}]
        disabled = sum(not bool(item.get("enabled", True)) for item in controls)
        return {"ok": True, "window_handle": hwnd, "controls": controls,
                "dialogs": dialogs[:8], "disabled_control_count": disabled,
                "requires_user_attention": bool(dialogs), "expires_in_seconds": self.TTL_SECONDS}

    def invoke(self, control_id: str, hwnd: int) -> dict[str, Any]:
        wrapper, error = self._valid(control_id, hwnd)
        if error:
            return {"ok": False, "error": error}
        try:
            invoke = getattr(wrapper, "invoke", None)
            if callable(invoke):
                invoke()
            else:
                select = getattr(wrapper, "select", None)
                if callable(select):
                    select()
                else:
                    return {"ok": False, "error": "The selected control does not support a semantic invoke action."}
        except Exception as exc:
            return {"ok": False, "error": f"UI Automation invoke failed: {exc}"}
        return {"ok": True, "control_id": control_id, "action": "invoke"}

    def set_value(self, control_id: str, hwnd: int, value: str) -> dict[str, Any]:
        wrapper, error = self._valid(control_id, hwnd)
        if error:
            return {"ok": False, "error": error}
        try:
            setter = getattr(wrapper, "set_edit_text", None)
            if callable(setter):
                setter(value)
            else:
                set_value = getattr(wrapper, "set_value", None)
                if not callable(set_value):
                    return {"ok": False, "error": "The selected control does not support setting a value."}
                set_value(value)
        except Exception as exc:
            return {"ok": False, "error": f"UI Automation set value failed: {exc}"}
        return {"ok": True, "control_id": control_id, "action": "set_value", "characters": len(value)}

    def _describe(self, wrapper: Any, hwnd: int) -> dict[str, Any] | None:
        try:
            info = wrapper.element_info
            name = str(getattr(info, "name", "") or wrapper.window_text() or "").strip()
            automation_id = str(getattr(info, "automation_id", "") or "").strip()
            control_type = str(getattr(info, "control_type", "") or "").strip()
            if not (name or automation_id or control_type):
                return None
            if hasattr(wrapper, "is_visible") and not wrapper.is_visible():
                return None
            rect = wrapper.rectangle()
            control_id = "U" + secrets.token_hex(4).upper()
            self._controls[control_id] = CachedControl(wrapper, hwnd, self._clock())
            return {"control_id": control_id, "name": name[:160], "automation_id": automation_id[:160],
                    "control_type": control_type[:80], "enabled": self._enabled(wrapper),
                    "rect": {"left": int(rect.left), "top": int(rect.top), "right": int(rect.right), "bottom": int(rect.bottom)}}
        except Exception:
            return None

    @staticmethod
    def _enabled(wrapper: Any) -> bool:
        try:
            return bool(wrapper.is_enabled())
        except Exception:
            return True

    def _valid(self, control_id: str, hwnd: int) -> tuple[Any | None, str | None]:
        item = self._controls.get(str(control_id))
        if item is None:
            return None, "Unknown UI Automation control; observe the active window again."
        if self._clock() - item.created_at > self.TTL_SECONDS:
            return None, "UI Automation observation expired; observe the active window again."
        if int(hwnd or 0) != item.hwnd:
            return None, "Active window changed since the UI Automation observation; observe again."
        try:
            if hasattr(item.wrapper, "is_visible") and not item.wrapper.is_visible():
                return None, "The UI Automation control is no longer visible; observe again."
            if hasattr(item.wrapper, "is_enabled") and not item.wrapper.is_enabled():
                return None, "The UI Automation control is currently disabled; observe again."
        except Exception:
            return None, "The UI Automation control state changed; observe again."
        return item.wrapper, None

    def _prune(self) -> None:
        now = self._clock()
        self._controls = {key: item for key, item in self._controls.items() if now - item.created_at <= self.TTL_SECONDS}
