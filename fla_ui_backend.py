"""Fail-closed semantic adapter for the local FlaUI-MCP server.

The upstream server exposes accessibility-tree references such as ``w1e5``.
Those references are useful only inside the MCP process and may become stale
after any UI change.  DeskOrb therefore wraps them in its own short-lived
observation/control IDs and never exposes the upstream tool set to the model.

This module deliberately does not use screenshots or coordinates.  If the
configured server is missing, cannot be discovered, or returns an unsupported
shape, callers receive ``desktop_backend_unavailable`` and must not fall back
to coordinate input automatically.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _Control:
    raw_ref: str
    window_handle: int
    observation_id: str
    created_at: float
    descriptor: dict[str, str]
    actions: tuple[str, ...]
    high_risk: bool


@dataclass(frozen=True)
class _Observation:
    observation_id: str
    window_handle: int
    fla_handle: str
    created_at: float
    controls: tuple[str, ...]
    blocking: bool


class FlaUIBackend:
    """Use only the bounded semantic subset of FlaUI-MCP."""

    UPSTREAM_RELEASE = "v0.2.0"
    UPSTREAM_COMMIT = "08301c31b301994c612ef5c6874c61f5d9578ea0"
    TTL_SECONDS = 12
    _OPERATIONS = (
        "windows_launch", "windows_snapshot", "windows_click", "windows_fill",
        "windows_type", "windows_get_text", "windows_focus",
    )
    _REQUIRED_OPERATIONS = {"windows_snapshot", "windows_click", "windows_get_text"}
    _HIGH_RISK_MARKERS = (
        "发送", "send", "发布", "publish", "购买", "buy", "purchase", "结算", "checkout",
        "上传", "upload", "删除", "delete", "登录", "login", "提交", "submit",
        "权限", "permission",
    )
    _ROLE_ACTIONS = {
        "button": "invoke", "listitem": "invoke", "menuitem": "invoke",
        "checkbox": "invoke", "radiobutton": "invoke", "tabitem": "invoke",
        "textbox": "set_value", "edit": "set_value", "combobox": "set_value",
    }
    _APP_EXECUTABLES = {
        "notepad": "notepad.exe", "calculator": "calc.exe", "explorer": "explorer.exe",
        "chrome": "chrome.exe", "edge": "msedge.exe", "firefox": "firefox.exe", "qq": "qq.exe",
    }

    def __init__(self, bridge: Any, *, server_name: str = "windows",
                 clock=time.monotonic, ttl_seconds: int = TTL_SECONDS,
                 deadline_getter=None):
        self._bridge = bridge
        self.server_name = str(server_name or "").strip()
        self._clock = clock
        self._deadline_getter = deadline_getter
        self.TTL_SECONDS = max(3, min(60, int(ttl_seconds)))
        self._discovered = False
        self._tool_names: dict[str, str] = {}
        self._diagnostic = ""
        self._observation_counter = 0
        self._observation_id = ""
        self._observations: dict[str, _Observation] = {}
        self._controls: dict[str, _Control] = {}
        self._window_refs: dict[int, str] = {}
        self._window_process_names: dict[int, str] = {}
        self._virtual_window_counter = -1000
        self._blocking_observations: set[str] = set()

    @property
    def configured(self) -> bool:
        servers = getattr(self._bridge, "available_servers", ())
        try:
            return bool(self.server_name and self.server_name in set(servers))
        except TypeError:
            return False

    @property
    def available(self) -> bool:
        return self._ensure_tools()

    @property
    def internal_tool_names(self) -> tuple[str, ...]:
        """Return only the raw names used internally, never a model schema."""
        return tuple(sorted(self._tool_names))

    @property
    def diagnostic(self) -> str:
        return self._diagnostic[:240]

    def register_window_handle(self, window_handle: int, fla_handle: str) -> None:
        """Bind a DeskOrb handle to FlaUI's opaque window handle in memory."""
        try:
            hwnd = int(window_handle)
        except (TypeError, ValueError):
            return
        value = str(fla_handle or "").strip()
        if hwnd and value:
            self._window_refs[hwnd] = value[:80]

    def launch_application(self, application: str) -> dict[str, Any]:
        if not self._ensure_tools() or "windows_launch" not in self._tool_names:
            return self._unavailable("FlaUI-MCP windows_launch is not available.")
        app = str(application or "").strip().lower()
        executable = self._APP_EXECUTABLES.get(app)
        if not executable:
            return {"ok": False, "failure_kind": "desktop_application_not_allowlisted",
                    "error": "The requested desktop application is not allowlisted."}
        result = self._call("windows_launch", {"app": executable})
        if not result.get("ok"):
            return result
        fla_handle = self._extract_handle(result)
        if not fla_handle:
            return self._unavailable("FlaUI-MCP did not return a window handle.")
        virtual_handle = self._virtual_window_counter
        self._virtual_window_counter -= 1
        self.register_window_handle(virtual_handle, fla_handle)
        self._window_process_names[virtual_handle] = executable
        return {"ok": True, "application": app, "window_handle": virtual_handle,
                "backend": "fla-ui-mcp", "backend_release": self.UPSTREAM_RELEASE}

    def application_process_name(self, window_handle: int) -> str:
        """Return the launch identity for a virtual FlaUI window handle."""
        return str(self._window_process_names.get(int(window_handle or 0), ""))

    def observe_active_window(self, hwnd: int, *, max_elements: int = 80) -> dict[str, Any]:
        if not self._ensure_tools():
            return self._unavailable()
        fla_handle = self._window_refs.get(int(hwnd or 0))
        if not fla_handle:
            return self._unavailable("The target window is not bound to a FlaUI-MCP handle; launch it through DeskOrb first.")
        limit = max(1, min(int(max_elements or 80), 200))
        result = self._call("windows_snapshot", {"handle": fla_handle})
        if not result.get("ok"):
            return result
        controls = self._parse_controls(result, int(hwnd), fla_handle, limit)
        if controls is None:
            return self._unavailable("FlaUI-MCP returned an unsupported accessibility snapshot.")
        self._invalidate_controls()
        self._observation_counter += 1
        observation_id = f"uia-obs-{self._observation_counter}"
        now = self._clock()
        normalized: list[dict[str, Any]] = []
        control_ids: list[str] = []
        for index, item in enumerate(controls):
            raw_ref = str(item.get("raw_ref") or "").strip()
            role = str(item.get("control_type") or "").strip().lower()
            name = str(item.get("name") or "").strip()[:160]
            if not raw_ref or not (name or role):
                continue
            control_id = self._control_id(observation_id, raw_ref, index)
            actions = self._actions_for(item)
            descriptor = {"name": name, "automation_id": "", "control_type": role}
            high_risk = self._is_high_risk(name, role)
            self._controls[control_id] = _Control(
                raw_ref=raw_ref, window_handle=int(hwnd), observation_id=observation_id,
                created_at=now, descriptor=descriptor, actions=actions, high_risk=high_risk,
            )
            control_ids.append(control_id)
            normalized.append({
                "control_id": control_id, "name": name, "automation_id": "",
                "control_type": role, "enabled": not bool(item.get("disabled")),
                "actions": list(actions),
            })
        if not normalized:
            return self._unavailable("FlaUI-MCP returned no usable semantic controls.")
        dialogs = [item for item in normalized if item.get("control_type") == "dialog"]
        blocking = bool(dialogs)
        fingerprint = self._fingerprint(normalized)
        self._observation_id = observation_id
        self._observations[observation_id] = _Observation(
            observation_id, int(hwnd), fla_handle, now, tuple(control_ids), blocking,
        )
        if blocking:
            self._blocking_observations.add(observation_id)
        return {
            "ok": True, "window_handle": int(hwnd), "uia_observation_id": observation_id,
            "observation_fingerprint": fingerprint, "controls": normalized,
            "dialogs": dialogs[:8], "requires_user_attention": blocking,
            "expires_in_seconds": self.TTL_SECONDS, "backend": "fla-ui-mcp",
            "backend_release": self.UPSTREAM_RELEASE,
        }

    def invoke(self, control_id: str, hwnd: int, observation_id: str | None = None) -> dict[str, Any]:
        control, error = self._valid(control_id, hwnd, observation_id)
        if error:
            return error
        if "invoke" not in control.actions:
            return {"ok": False, "failure_kind": "desktop_uia_invoke_unavailable",
                    "error": "The observed control does not advertise an invoke action."}
        result = self._call("windows_click", {"ref": control.raw_ref})
        self._invalidate_controls()
        if not result.get("ok"):
            return result
        return {"ok": True, "control_id": str(control_id), "action": "invoke",
                "verified": False, "requires_reobserve": True,
                "verification": {"passed": False, "kind": "uia_invoke_dispatch",
                                  "requires_reobserve": True}}

    def set_value(self, control_id: str, hwnd: int, value: str,
                  observation_id: str | None = None) -> dict[str, Any]:
        control, error = self._valid(control_id, hwnd, observation_id)
        if error:
            return error
        value = str(value)
        if len(value) > 4000 or "\x00" in value:
            return {"ok": False, "failure_kind": "desktop_uia_invalid_value",
                    "error": "UI Automation value must contain at most 4000 characters and no NUL bytes."}
        if "set_value" not in control.actions:
            return {"ok": False, "failure_kind": "desktop_uia_value_pattern_unavailable",
                    "error": "The observed control does not advertise a fillable value action."}
        operation = "windows_fill" if "windows_fill" in self._tool_names else "windows_type"
        if operation not in self._tool_names:
            return {"ok": False, "failure_kind": "desktop_uia_value_pattern_unavailable",
                    "error": "FlaUI-MCP has no safe fill operation."}
        arguments = {"ref": control.raw_ref, "text": value}
        result = self._call(operation, arguments)
        if not result.get("ok"):
            self._invalidate_controls()
            return result
        readback = self._read_raw_text(control.raw_ref)
        exact = bool(readback is not None and readback == value)
        self._invalidate_controls()
        return {
            "ok": exact, "control_id": str(control_id), "action": "set_value",
            "characters": len(value), "readback_available": readback is not None,
            "verified": exact, "verification": {"passed": exact, "kind": "uia_value_readback"},
            **({} if exact else {
                "failure_kind": "desktop_uia_value_readback_failed",
                "error": "FlaUI-MCP did not provide an exact value readback.",
            }),
        }

    def focus_control(self, control_id: str, hwnd: int,
                      observation_id: str | None = None) -> dict[str, Any]:
        control, error = self._valid(control_id, hwnd, observation_id)
        if error:
            return {"ok": False, "failure_kind": "desktop_uia_stale_control",
                    "error": error.get("error", "The observed control is stale.")}
        if "windows_focus" not in self._tool_names:
            return {"ok": False, "failure_kind": "desktop_uia_focus_unavailable",
                    "error": "FlaUI-MCP does not provide semantic focus for this installation."}
        result = self._call("windows_focus", {"ref": control.raw_ref})
        self._invalidate_controls()
        if not result.get("ok"):
            return {"ok": False, "failure_kind": "desktop_uia_focus_failed",
                    "error": str(result.get("error") or "FlaUI-MCP focus failed.")[:240]}
        return {"ok": True, "control_id": str(control_id), "requires_reobserve": True}

    def control_descriptor(self, control_id: str, observation_id: str | None = None) -> dict[str, str] | None:
        control = self._controls.get(str(control_id))
        if control is None or (observation_id and control.observation_id != str(observation_id)):
            return None
        return dict(control.descriptor)

    def find_control(self, descriptor: dict[str, str] | None,
                     observation_id: str | None = None) -> str | None:
        if not isinstance(descriptor, dict):
            return None
        wanted = {
            "name": str(descriptor.get("name") or "").strip(),
            "automation_id": str(descriptor.get("automation_id") or "").strip(),
            "control_type": str(descriptor.get("control_type") or "").strip().lower(),
        }
        for control_id, control in self._controls.items():
            if observation_id and control.observation_id != str(observation_id):
                continue
            current = control.descriptor
            if wanted["name"] and current.get("name") != wanted["name"]:
                continue
            if wanted["automation_id"] and current.get("automation_id") != wanted["automation_id"]:
                continue
            if wanted["control_type"] and current.get("control_type", "").lower() != wanted["control_type"]:
                continue
            return control_id
        return None

    def read_value(self, control_id: str, hwnd: int,
                   observation_id: str | None = None) -> dict[str, Any]:
        control, error = self._valid(control_id, hwnd, observation_id)
        if error:
            return {"ok": False, "failure_kind": "desktop_uia_stale_control",
                    "error": error.get("error", "The observed control is stale.")}
        observed = self._read_raw_text(control.raw_ref)
        return {"ok": observed is not None, "readback_available": observed is not None,
                "value": observed, "characters": len(observed or "")}

    def is_high_risk(self, control_id: str) -> bool:
        control = self._controls.get(str(control_id))
        return bool(control and self._clock() - control.created_at <= self.TTL_SECONDS and control.high_risk)

    def is_blocking_observation(self, observation_id: str) -> bool:
        return str(observation_id or "") in self._blocking_observations

    def coordinate_fallback_eligible(self, observation_id: str) -> bool:
        # A configured FlaUI backend must fail closed.  It is never safe to
        # silently cross from semantic UIA into coordinates after an MCP error.
        return False

    def reset(self) -> None:
        self._invalidate_controls()
        self._observations.clear()
        self._blocking_observations.clear()
        self._window_refs.clear()
        self._window_process_names.clear()
        self._observation_id = ""

    def _ensure_tools(self) -> bool:
        if self._discovered:
            return bool(self._tool_names and self._REQUIRED_OPERATIONS.issubset(self._tool_names))
        self._discovered = True
        if not self.configured:
            self._diagnostic = "configured FlaUI-MCP server was not found"
            return False
        try:
            schemas = self._bridge.schemas((self.server_name,))
        except Exception as exc:
            self._diagnostic = f"FlaUI-MCP discovery failed: {type(exc).__name__}"
            return False
        prefix = f"mcp_{self.server_name}_"
        for schema in schemas if isinstance(schemas, list) else []:
            name = str(schema.get("name") or "") if isinstance(schema, dict) else ""
            if not name.startswith(prefix):
                continue
            original = name[len(prefix):]
            if original in self._OPERATIONS:
                self._tool_names[original] = name
        if not self._REQUIRED_OPERATIONS.issubset(self._tool_names):
            self._diagnostic = "FlaUI-MCP semantic tool contract is incomplete"
            return False
        return True

    def _call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        exposed = self._tool_names.get(operation)
        if not exposed or operation not in self._OPERATIONS:
            return self._unavailable(f"FlaUI-MCP operation {operation} is not allowlisted.")
        timeout, timeout_failure = self._call_timeout(operation)
        if timeout_failure:
            return timeout_failure
        try:
            call = self._bridge.call
            if timeout is not None:
                try:
                    parameters = inspect.signature(call).parameters
                    accepts_timeout = ("timeout_seconds" in parameters
                                       or any(parameter.kind is inspect.Parameter.VAR_KEYWORD
                                              for parameter in parameters.values()))
                except (TypeError, ValueError):
                    accepts_timeout = True
                result = (call(exposed, arguments, timeout_seconds=timeout)
                          if accepts_timeout else call(exposed, arguments))
            else:
                result = call(exposed, arguments)
        except Exception as exc:
            return self._unavailable(f"FlaUI-MCP call failed: {type(exc).__name__}")
        if not isinstance(result, dict) or not bool(result.get("ok")):
            return {"ok": False, "failure_kind": "desktop_backend_unavailable",
                    "error": str((result or {}).get("error") or "FlaUI-MCP rejected the semantic action.")[:240]}
        return result

    def _call_timeout(self, operation: str) -> tuple[float | None, dict[str, Any] | None]:
        if not callable(self._deadline_getter):
            return None, None
        try:
            deadline = self._deadline_getter()
        except Exception:
            deadline = None
        if deadline is None:
            return None, None
        phase = "desktop_observation" if operation in {"windows_snapshot"} else "tool_execution"
        if operation == "windows_get_text":
            phase = "postcondition_verification"
        try:
            timeout = float(deadline.timeout_for(30.0, phase=phase))
        except Exception:
            return None, {"ok": False, "failure_kind": "desktop_backend_unavailable",
                          "error": "The desktop execution deadline could not be applied."}
        if timeout <= 0:
            failure_kind = {
                "desktop_observation": "desktop_observation_timeout",
                "postcondition_verification": "postcondition_verification_timeout",
            }.get(phase, "tool_execution_timeout")
            return None, {"ok": False, "failure_kind": failure_kind,
                          "error": "The task deadline was exhausted before the FlaUI action started."}
        return timeout, None

    def _valid(self, control_id: str, hwnd: int,
               observation_id: str | None) -> tuple[_Control | None, dict[str, Any] | None]:
        control = self._controls.get(str(control_id))
        if control is None:
            return None, {"ok": False, "failure_kind": "desktop_uia_stale_control",
                          "error": "Unknown UI Automation control; observe again."}
        if not str(observation_id or "").strip():
            return None, {"ok": False, "failure_kind": "desktop_uia_stale_control",
                          "error": "A current UI Automation observation ID is required."}
        if observation_id and control.observation_id != str(observation_id):
            return None, {"ok": False, "failure_kind": "desktop_uia_stale_control",
                          "error": "UI Automation observation changed; observe again."}
        if control.observation_id != self._observation_id:
            return None, {"ok": False, "failure_kind": "desktop_uia_stale_control",
                          "error": "UI Automation control is stale; observe again."}
        if self._clock() - control.created_at > self.TTL_SECONDS:
            return None, {"ok": False, "failure_kind": "desktop_uia_stale_control",
                          "error": "UI Automation observation expired; observe again."}
        if int(hwnd or 0) != control.window_handle:
            return None, {"ok": False, "failure_kind": "desktop_uia_window_changed",
                          "error": "The target window changed; observe again."}
        if self.is_blocking_observation(control.observation_id):
            return None, {"ok": False, "failure_kind": "desktop_modal_dialog",
                          "error": "A modal dialog requires user attention before UIA actions can continue."}
        return control, None

    def _read_raw_text(self, raw_ref: str) -> str | None:
        if not self._ensure_tools() or "windows_get_text" not in self._tool_names:
            return None
        result = self._call("windows_get_text", {"ref": str(raw_ref)})
        if not result.get("ok"):
            return None
        content = self._content(result)
        if isinstance(content, dict):
            for key in ("text", "value", "content"):
                if isinstance(content.get(key), str):
                    return content[key]
        if isinstance(content, str):
            try:
                decoded = json.loads(content)
            except (TypeError, ValueError):
                return content
            if isinstance(decoded, dict):
                for key in ("text", "value", "content"):
                    if isinstance(decoded.get(key), str):
                        return decoded[key]
            return content
        return None

    def _parse_controls(self, result: dict[str, Any], hwnd: int,
                        fla_handle: str, limit: int) -> list[dict[str, Any]] | None:
        content = self._content(result)
        raw_controls: list[dict[str, Any]] = []
        if isinstance(content, dict) and isinstance(content.get("controls"), list):
            raw_controls = [item for item in content["controls"] if isinstance(item, dict)]
        elif isinstance(content, list) and any(isinstance(item, dict) for item in content):
            raw_controls = [item for item in content if isinstance(item, dict)]
        elif isinstance(result.get("controls"), list):
            raw_controls = [item for item in result["controls"] if isinstance(item, dict)]
        else:
            text = self._content_text(result)
            if text is None:
                return None
            raw_controls = self._parse_snapshot_text(text)
        normalized: list[dict[str, Any]] = []
        for item in raw_controls[:limit]:
            raw_ref = str(item.get("raw_ref") or item.get("ref") or "").strip()
            role = str(item.get("control_type") or item.get("role") or item.get("type") or "").strip().lower()
            name = str(item.get("name") or "").strip()
            if not raw_ref or not (role or name):
                continue
            # The top-level window is context, not an actionable control. A
            # named ``dialog`` remains in the tree so the caller can stop for
            # user attention.
            if role == "window":
                continue
            normalized.append({
                "raw_ref": raw_ref, "control_type": role, "name": name,
                "disabled": bool(item.get("disabled")) or "disabled" in str(item.get("state") or "").lower(),
            })
        return normalized

    @staticmethod
    def _parse_snapshot_text(text: str) -> list[dict[str, Any]]:
        pattern = re.compile(
            r'^\s*-\s*(?P<role>[A-Za-z][\w -]*)\s+"(?P<name>(?:\\.|[^"])*)"\s+\[ref=(?P<ref>[^\]]+)\](?P<state>.*)$'
        )
        result: list[dict[str, Any]] = []
        for line in str(text).splitlines():
            match = pattern.match(line)
            if not match:
                continue
            result.append({
                "raw_ref": match.group("ref").strip(),
                "control_type": match.group("role").strip().lower(),
                "name": match.group("name").replace('\\"', '"').strip(),
                "state": match.group("state"),
            })
        return result

    @classmethod
    def _actions_for(cls, item: dict[str, Any]) -> tuple[str, ...]:
        role = str(item.get("control_type") or "").lower()
        if bool(item.get("disabled")):
            return ()
        action = cls._ROLE_ACTIONS.get(role)
        return (action,) if action else ()

    @classmethod
    def _is_high_risk(cls, name: str, role: str) -> bool:
        lowered = f"{name} {role}".lower()
        return any(marker in lowered for marker in cls._HIGH_RISK_MARKERS)

    @staticmethod
    def _control_id(observation_id: str, raw_ref: str, index: int) -> str:
        digest = hashlib.sha256(f"{observation_id}|{raw_ref}|{index}".encode()).hexdigest()[:10].upper()
        return f"U{digest}"

    @staticmethod
    def _fingerprint(controls: list[dict[str, Any]]) -> str:
        material = "\n".join(
            "|".join(str(item.get(key) or "")[:160]
                      for key in ("name", "control_type", "disabled"))
            for item in controls
        )
        return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _content(result: dict[str, Any]) -> Any:
        if isinstance(result.get("structuredContent"), (dict, list, str)):
            return result["structuredContent"]
        content = result.get("content", result)
        if isinstance(content, list):
            if any(isinstance(item, dict) and ("ref" in item or "raw_ref" in item)
                   for item in content):
                return content
            structured: list[Any] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "json" and isinstance(item.get("data"), (dict, list)):
                    structured.append(item["data"])
                elif isinstance(item.get("text"), str):
                    try:
                        structured.append(json.loads(item["text"]))
                    except (TypeError, ValueError):
                        structured.append(item["text"])
            if len(structured) == 1:
                return structured[0]
            return structured
        return content

    @classmethod
    def _content_text(cls, result: dict[str, Any]) -> str | None:
        content = cls._content(result)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text = "\n".join(item for item in content if isinstance(item, str))
            return text or None
        return None

    @staticmethod
    def _extract_handle(result: dict[str, Any]) -> str | None:
        content = FlaUIBackend._content(result)
        if isinstance(content, dict):
            for key in ("handle", "window_handle", "ref"):
                value = str(content.get(key) or "").strip()
                if value:
                    return value[:80]
        text = FlaUIBackend._content_text(result) or ""
        match = re.search(r"(?:window\s+handle|handle)\s*[:=]\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
        return match.group(1)[:80] if match else None

    def _invalidate_controls(self) -> None:
        self._controls.clear()
        self._observation_id = ""

    @staticmethod
    def _unavailable(message: str = "") -> dict[str, Any]:
        return {"ok": False, "failure_kind": "desktop_backend_unavailable",
                "error": (message or "FlaUI-MCP desktop backend is unavailable.")[:240]}


__all__ = ["FlaUIBackend"]
