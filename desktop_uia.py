"""Optional Windows UI Automation observer and semantic control invoker.

Coordinates are a last resort. When pywinauto's UIA backend is installed this
module exposes the current app's named controls with short-lived identifiers,
then invokes that exact control after validating the foreground window again.
"""
from __future__ import annotations

import hashlib
import ctypes
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from desktop_adapters import DesktopApplicationRegistry
from win32utils import window_process_name

try:
    from pywinauto import Desktop
except ImportError:  # pragma: no cover - availability is reported at runtime.
    Desktop = None


@dataclass
class CachedControl:
    wrapper: Any
    hwnd: int
    created_at: float
    observation_id: str


class DesktopUIA:
    TTL_SECONDS = 12
    MESSAGE_DELIVERY_MARKERS = (
        "已发送", "发送成功", "消息已发出", "已送达", "message sent", "sent", "delivered",
    )

    def __init__(self, desktop_factory=None, clock=time.monotonic,
                 application_registry: DesktopApplicationRegistry | None = None,
                 foreground_getter=None):
        self._desktop_factory = desktop_factory or Desktop
        self._clock = clock
        self._foreground_getter = foreground_getter or self._foreground_window
        self._controls: dict[str, CachedControl] = {}
        self._high_risk_controls: dict[str, float] = {}
        self._observation_fingerprints: dict[int, tuple[str, float]] = {}
        self._pending_message_verification: dict[int, tuple[float, str | None]] = {}
        self._application_registry = application_registry or DesktopApplicationRegistry()
        self._observation_counter = 0
        self._observation_id = ""
        self._blocking_observations: set[str] = set()

    @property
    def available(self) -> bool:
        return self._desktop_factory is not None

    def observe_active_window(self, hwnd: int, *, max_elements: int = 80) -> dict[str, Any]:
        if not self.available:
            return {"ok": False, "error": "Windows UI Automation is unavailable. Install pywinauto and restart DeskOrb."}
        if not hwnd:
            return {"ok": False, "error": "No active window is available for UI Automation."}
        if not self._foreground_matches(int(self._foreground_getter() or 0), int(hwnd)):
            return {"ok": False, "error": "Active window changed; observe the intended application again."}
        try:
            window = self._desktop_factory(backend="uia").window(handle=hwnd)
            descendants = window.descendants()
        except Exception as exc:
            return {"ok": False, "error": f"Could not inspect the active window with UI Automation: {exc}"}
        process_name = window_process_name(hwnd)
        application = self._application_registry.match(process_name)
        self._prune()
        self._observation_counter += 1
        self._observation_id = f"uia-obs-{self._observation_counter}"
        self._controls.clear()
        self._high_risk_controls.clear()
        controls: list[dict[str, Any]] = []
        for wrapper in descendants:
            if len(controls) >= max(1, min(int(max_elements), 200)):
                break
            item = self._describe(wrapper, hwnd)
            if item is not None:
                controls.append(item)
        dialogs = [item for item in controls if str(item.get("control_type", "")).lower() in {"dialog", "window"}]
        disabled = sum(not bool(item.get("enabled", True)) for item in controls)
        recommended_actions = self._application_registry.recommended_actions(process_name, controls)
        for recommendation in recommended_actions:
            if (isinstance(recommendation, dict)
                    and str(recommendation.get("risk_level") or "").lower() == "high"):
                control_id = str(recommendation.get("control_id") or "")
                if control_id:
                    self._high_risk_controls[control_id] = self._clock() + self.TTL_SECONDS
        fingerprint = self._fingerprint(controls)
        self._observation_fingerprints[int(hwnd)] = (fingerprint, self._clock())
        verification = self._message_delivery_verification(application.app_id, controls, fingerprint, int(hwnd))
        result = {
            "ok": True, "window_handle": hwnd, "process_name": process_name[:80],
            "uia_observation_id": self._observation_id,
            "application": application.safe_dict(), "controls": controls,
            "recommended_actions": recommended_actions, "dialogs": dialogs[:8],
            "disabled_control_count": disabled, "requires_user_attention": bool(dialogs),
            "expires_in_seconds": self.TTL_SECONDS,
        }
        if dialogs:
            self._blocking_observations.add(self._observation_id)
        if verification is not None:
            result["verification"] = verification
        return result

    def is_high_risk(self, control_id: str) -> bool:
        """Return the risk declared by the most recent bounded observation."""
        self._prune()
        return str(control_id or "") in self._high_risk_controls

    def invoke(self, control_id: str, hwnd: int, observation_id: str | None = None) -> dict[str, Any]:
        wrapper, error = self._valid(control_id, hwnd, observation_id)
        if error:
            return {"ok": False, "error": error}
        before = self._action_state(wrapper)
        message_action = self._is_message_send_control(wrapper, hwnd)
        message_baseline = self._observation_fingerprints.get(int(hwnd), (None, 0.0))[0] if message_action else None
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
        after = self._action_state(wrapper)
        changed = self._meaningful_state_change(before, after)
        dismissed = before.get("visible") is True and after.get("visible") is False
        verified = bool(changed or dismissed)
        if message_action:
            self._pending_message_verification[int(hwnd)] = (
                self._clock() + self.TTL_SECONDS, message_baseline)
        if dismissed:
            kind = "uia_control_dismissed"
        elif changed:
            kind = "uia_state_change"
        else:
            kind = "uia_invoke_dispatch"
        self._invalidate_controls()
        return {"ok": True, "control_id": control_id, "action": "invoke",
                "verified": verified,
                "uia_observation_id": str(observation_id or self._observation_id),
                "verification": {"passed": verified, "kind": kind,
                                  "requires_reobserve": not verified}}

    def set_value(self, control_id: str, hwnd: int, value: str,
                  observation_id: str | None = None) -> dict[str, Any]:
        wrapper, error = self._valid(control_id, hwnd, observation_id)
        if error:
            return {"ok": False, "error": error}
        value = str(value)
        if len(value) > 4000 or "\x00" in value:
            return {"ok": False, "error": "UI Automation value must contain at most 4000 characters and no NUL bytes."}
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
        observed = self._read_value(wrapper)
        verified = observed is not None and observed == value
        self._invalidate_controls()
        return {"ok": True, "control_id": control_id, "action": "set_value", "characters": len(value),
                "verified": verified, "uia_observation_id": str(observation_id or self._observation_id),
                "verification": {"passed": verified, "kind": "uia_value_readback"}}

    def is_blocking_observation(self, observation_id: str) -> bool:
        return str(observation_id or "") in self._blocking_observations

    def coordinate_fallback_eligible(self, observation_id: str) -> bool:
        """Allow coordinate fallback only after a fresh, non-modal UIA observation."""
        if not self.available:
            return True
        current = str(observation_id or "")
        return bool(current and current == self._observation_id
                    and current not in self._blocking_observations)

    def reset(self) -> None:
        self._controls.clear()
        self._high_risk_controls.clear()
        self._observation_fingerprints.clear()
        self._pending_message_verification.clear()
        self._blocking_observations.clear()
        self._observation_id = ""

    def _invalidate_controls(self) -> None:
        self._controls.clear()
        self._high_risk_controls.clear()

    @staticmethod
    def _read_value(wrapper: Any) -> str | None:
        """Read a control value without retaining the value in diagnostics."""
        for method_name in ("get_value", "window_text"):
            method = getattr(wrapper, method_name, None)
            if not callable(method):
                continue
            try:
                value = method()
            except Exception:
                continue
            if value is not None:
                return str(value)
        return None

    @staticmethod
    def _foreground_window() -> int:
        if os.name != "nt":
            return 0
        try:
            return int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            return 0

    @staticmethod
    def _foreground_matches(current: int, target: int) -> bool:
        current = int(current or 0)
        target = int(target or 0)
        if not current or not target:
            return False
        if current == target:
            return True
        if os.name != "nt":
            return False
        try:
            user32 = ctypes.windll.user32
            get_ancestor = getattr(user32, "GetAncestor", None)
            if not callable(get_ancestor):
                return False
            return int(get_ancestor(current, 2) or current) == int(get_ancestor(target, 2) or target)
        except Exception:
            return False

    @staticmethod
    def _action_state(wrapper: Any) -> dict[str, Any]:
        """Read non-sensitive control state for post-action verification."""
        state: dict[str, Any] = {}
        for key, method_names in {
            "visible": ("is_visible",), "enabled": ("is_enabled",),
            "focused": ("has_focus", "is_focused"), "selected": ("is_selected",),
            "expanded": ("is_expanded",), "checked": ("is_checked",),
            "toggle_state": ("get_toggle_state",),
        }.items():
            for method_name in method_names:
                method = getattr(wrapper, method_name, None)
                if not callable(method):
                    continue
                try:
                    value = method()
                except Exception:
                    continue
                if isinstance(value, (bool, int, float, str)) or value is None:
                    state[key] = value
                break
        return state

    @staticmethod
    def _meaningful_state_change(before: dict[str, Any], after: dict[str, Any]) -> bool:
        keys = ("selected", "expanded", "checked", "toggle_state", "focused")
        return any(key in before and key in after and before[key] != after[key] for key in keys)

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
            self._controls[control_id] = CachedControl(
                wrapper, hwnd, self._clock(), self._observation_id,
            )
            return {"control_id": control_id, "name": name[:160], "automation_id": automation_id[:160],
                    "control_type": control_type[:80], "enabled": self._enabled(wrapper),
                    "actions": self._actions(wrapper),
                    "rect": {"left": int(rect.left), "top": int(rect.top),
                             "right": int(rect.right), "bottom": int(rect.bottom)}}
        except Exception:
            return None

    @classmethod
    def _fingerprint(cls, controls: list[dict[str, Any]]) -> str:
        parts = []
        for item in controls if isinstance(controls, list) else []:
            if not isinstance(item, dict):
                continue
            parts.append("|".join(str(item.get(key) or "")[:160]
                                  for key in ("name", "automation_id", "control_type", "enabled", "actions")))
        return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()

    @classmethod
    def _delivery_marker_count(cls, controls: list[dict[str, Any]]) -> int:
        names = " ".join(str(item.get("name") or "") for item in controls if isinstance(item, dict)).lower()
        count = 0
        for marker in cls.MESSAGE_DELIVERY_MARKERS:
            lowered = marker.lower()
            matched = (re.search(r"\b" + re.escape(lowered) + r"\b", names)
                       if lowered.isascii() else lowered in names)
            count += bool(matched)
        return count

    def _message_delivery_verification(self, application_id: str, controls: list[dict[str, Any]],
                                       fingerprint: str, hwnd: int) -> dict[str, Any] | None:
        if str(application_id).lower() != "qq":
            return None
        pending = self._pending_message_verification.get(int(hwnd))
        marker_count = self._delivery_marker_count(controls)
        baseline = pending[1] if pending else None
        changed = bool(baseline and baseline != fingerprint)
        passed = bool(pending and marker_count and changed)
        if passed:
            self._pending_message_verification.pop(int(hwnd), None)
        return {"passed": passed, "kind": "message_delivery", "marker_count": marker_count,
                "state_changed": changed, "requires_reobserve": not passed}

    @classmethod
    def _is_message_send_control(cls, wrapper: Any, hwnd: int) -> bool:
        if window_process_name(hwnd).lower() not in {"qq.exe", "qqnt.exe"}:
            return False
        try:
            info = wrapper.element_info
            name = str(getattr(info, "name", "") or wrapper.window_text() or "").lower()
            control_type = str(getattr(info, "control_type", "") or "").lower()
            return control_type in {"button", "menuitem", "listitem"} and any(
                marker in name for marker in ("发送", "send", "提交", "publish"))
        except Exception:
            return False

    @staticmethod
    def _actions(wrapper: Any) -> list[str]:
        actions: list[str] = []
        if callable(getattr(wrapper, "invoke", None)) or callable(getattr(wrapper, "select", None)):
            actions.append("invoke")
        if callable(getattr(wrapper, "set_edit_text", None)) or callable(getattr(wrapper, "set_value", None)):
            actions.append("set_value")
        return actions

    @staticmethod
    def _enabled(wrapper: Any) -> bool:
        try:
            return bool(wrapper.is_enabled())
        except Exception:
            return True

    def _valid(self, control_id: str, hwnd: int,
               observation_id: str | None = None) -> tuple[Any | None, str | None]:
        item = self._controls.get(str(control_id))
        if item is None:
            return None, "Unknown UI Automation control; observe the active window again."
        if observation_id and str(observation_id) != item.observation_id:
            return None, "UI Automation observation changed; observe the active window again."
        if item.observation_id != self._observation_id:
            return None, "UI Automation control is stale; observe the active window again."
        if self._clock() - item.created_at > self.TTL_SECONDS:
            return None, "UI Automation observation expired; observe the active window again."
        if int(hwnd or 0) != item.hwnd:
            return None, "Active window changed since the UI Automation observation; observe again."
        if not self._foreground_matches(int(self._foreground_getter() or 0), item.hwnd):
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
        self._controls = {key: item for key, item in self._controls.items()
                          if now - item.created_at <= self.TTL_SECONDS}
        self._high_risk_controls = {key: expires for key, expires in self._high_risk_controls.items()
                                    if expires > now}
        self._observation_fingerprints = {key: item for key, item in self._observation_fingerprints.items()
                                          if now - item[1] <= self.TTL_SECONDS}
        self._pending_message_verification = {key: item for key, item in self._pending_message_verification.items()
                                              if item[0] > now}
