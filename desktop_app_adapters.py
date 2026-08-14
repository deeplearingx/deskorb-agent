"""Deterministic, semantic adapters for the small Windows app set DeskOrb tests.

These adapters are internal runtime helpers, not model tools. They intentionally
perform one observed UIA action at a time and require a fresh observation or an
exact readback before returning success. They are useful for real smoke tests
and for recovering from provider timeouts without asking the model to replay a
desktop side effect.
"""
from __future__ import annotations

import ast
import operator
import re
from pathlib import Path
from typing import Any


class DesktopAppAdapters:
    """Run bounded app-specific workflows through AgentRuntime's UIA facade."""

    _BINOPS = {
        ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
    }
    _BUTTON_ALIASES = {
        "0": ("0", "zero", "零"), "1": ("1", "one", "一"),
        "2": ("2", "two", "二"), "3": ("3", "three", "三"),
        "4": ("4", "four", "四"), "5": ("5", "five", "五"),
        "6": ("6", "six", "六"), "7": ("7", "seven", "七"),
        "8": ("8", "eight", "八"), "9": ("9", "nine", "九"),
        "+": ("+", "plus", "add", "加", "加法"),
        "-": ("-", "minus", "subtract", "减", "减法"),
        "*": ("*", "×", "multiply", "multiplication", "乘", "乘法"),
        "/": ("/", "÷", "divide", "division", "除", "除法"),
        "=": ("=", "equals", "equal", "结果", "等于"),
    }

    def __init__(self, runtime: Any):
        self.runtime = runtime

    def notepad(self, value: str, *, window_handle: int | None = None) -> dict[str, Any]:
        return self._set_editable("notepad", str(value), window_handle=window_handle)

    def explorer(self, path: str, *, window_handle: int | None = None) -> dict[str, Any]:
        safe_path = self._temporary_path(path)
        if safe_path is None:
            return self._failure("desktop_path_outside_working_dir",
                                 "Explorer adapter accepts only the temporary working directory.")
        return self._set_editable("explorer", str(safe_path), window_handle=window_handle,
                                  explorer=True)

    def calculator(self, expression: str = "2+2", *, window_handle: int | None = None) -> dict[str, Any]:
        tokens, expected = self._safe_expression(expression)
        if tokens is None or expected is None:
            return self._failure("calculator_expression_not_allowlisted",
                                 "Calculator adapter accepts only a bounded arithmetic expression.")
        launched = self._launch("calculator", window_handle)
        if not launched["ok"]:
            return launched
        hwnd = int(launched["window_handle"])
        observed = self._observe(hwnd)
        if not observed["ok"]:
            return observed
        action_steps = 0
        for token in tokens:
            control = self._find_control(observed, token)
            if control is None:
                return self._failure("desktop_control_not_found",
                                     "Calculator control was not found in the current observation.",
                                     action_steps=action_steps)
            invoked = self.runtime._run_local_tool("desktop_uia_invoke", {
                "control_id": control["control_id"], "window_handle": hwnd,
                "uia_observation_id": observed.get("uia_observation_id", ""),
            })
            action_steps += 1
            if not isinstance(invoked, dict) or not invoked.get("ok"):
                return self._failure(str((invoked or {}).get("failure_kind") or "desktop_action_failed"),
                                     "Calculator semantic button action failed.", action_steps=action_steps)
            observed = invoked.get("after_observation") if isinstance(invoked.get("after_observation"), dict) else self._observe(hwnd)
            if not isinstance(observed, dict) or not observed.get("ok"):
                return self._failure("desktop_observation_failed",
                                     "Calculator could not be re-observed after the button action.",
                                     action_steps=action_steps)
        display_match = self._display_contains(observed, expected)
        return {
            "ok": display_match, "verified": display_match,
            "postcondition_passed": display_match,
            "postcondition_kind": "calculator_result_observation",
            "action_steps": action_steps,
            **({} if display_match else {
                "failure_kind": "calculator_result_readback_failed",
                "error": "Calculator display did not contain the exact expected result.",
            }),
        }

    def _set_editable(self, application: str, value: str, *,
                      window_handle: int | None, explorer: bool = False) -> dict[str, Any]:
        launched = self._launch(application, window_handle)
        if not launched["ok"]:
            return launched
        hwnd = int(launched["window_handle"])
        observed = self._observe(hwnd)
        if not observed["ok"]:
            return observed
        control = self._find_editable(observed, explorer=explorer)
        if control is None:
            return self._failure("desktop_control_not_found",
                                 "No observed editable control matched the application adapter.")
        result = self.runtime._run_local_tool("desktop_uia_set_value", {
            "control_id": control["control_id"], "window_handle": hwnd,
            "uia_observation_id": observed.get("uia_observation_id", ""), "value": value,
        })
        passed = bool(isinstance(result, dict) and result.get("ok") and result.get("verified")
                      and result.get("postcondition_passed", True))
        if not passed:
            return self._failure(str((result or {}).get("failure_kind") or "desktop_value_readback_failed"),
                                 "The application adapter did not receive an exact value readback.")
        return {"ok": True, "verified": True, "postcondition_passed": True,
                "postcondition_kind": "uia_value_readback", "action_steps": 1}

    def _launch(self, application: str, window_handle: int | None) -> dict[str, Any]:
        if window_handle:
            return {"ok": True, "window_handle": int(window_handle), "reused_target_window": True}
        result = self.runtime._run_local_tool("application_launch", {"application": application})
        if not isinstance(result, dict) or not result.get("ok") or not result.get("window_handle"):
            return self._failure(str((result or {}).get("failure_kind") or "desktop_backend_unavailable"),
                                 "The application adapter could not launch the requested application.")
        return result

    def _observe(self, hwnd: int) -> dict[str, Any]:
        result = self.runtime._run_local_tool("desktop_uia_observe", {
            "window_handle": int(hwnd), "max_elements": 120,
        })
        if not isinstance(result, dict) or not result.get("ok"):
            return self._failure(str((result or {}).get("failure_kind") or "desktop_observation_failed"),
                                 "The application adapter could not obtain a fresh UIA observation.")
        return result

    @staticmethod
    def _find_editable(observed: dict[str, Any], *, explorer: bool) -> dict[str, Any] | None:
        controls = [item for item in observed.get("controls") or () if isinstance(item, dict)]
        markers = ("address", "地址", "location", "路径", "folder") if explorer else ()
        candidates = [item for item in controls
                      if item.get("enabled", True)
                      and "set_value" in {str(action) for action in item.get("actions") or ()}
                      and str(item.get("control_type") or "").lower() in {"edit", "textbox", "combobox"}]
        if explorer and markers:
            marked = [item for item in candidates
                      if any(marker in str(item.get("name") or "").lower() for marker in markers)]
            if marked:
                return marked[0]
        return candidates[0] if candidates else None

    def _find_control(self, observed: dict[str, Any], token: str) -> dict[str, Any] | None:
        aliases = self._BUTTON_ALIASES.get(token, (token,))
        aliases = tuple(alias.casefold() for alias in aliases)
        for item in observed.get("controls") or ():
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            if "invoke" not in {str(action) for action in item.get("actions") or ()}:
                continue
            name = str(item.get("name") or "").strip().casefold()
            if name in aliases or any(alias and alias in name for alias in aliases):
                return item
        return None

    @staticmethod
    def _display_contains(observed: dict[str, Any], expected: float) -> bool:
        expected_text = str(int(expected)) if float(expected).is_integer() else str(expected)
        return any(expected_text == str(item.get("name") or "").strip()
                   for item in observed.get("controls") or () if isinstance(item, dict)
                   and str(item.get("control_type") or "").lower() in {"text", "edit", "label"})

    @staticmethod
    def _safe_expression(expression: str) -> tuple[list[str] | None, float | None]:
        raw = str(expression or "").strip()
        if not re.fullmatch(r"\d+(?:\s*[+\-*/]\s*\d+)+", raw):
            return None, None
        try:
            tree = ast.parse(raw, mode="eval").body
            def evaluate(node: ast.AST) -> float:
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                    return float(node.value)
                if isinstance(node, ast.BinOp) and type(node.op) in DesktopAppAdapters._BINOPS:
                    return DesktopAppAdapters._BINOPS[type(node.op)](evaluate(node.left), evaluate(node.right))
                raise ValueError
            expected = evaluate(tree)
        except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
            return None, None
        tokens = re.findall(r"\d+|[+\-*/]", raw) + ["="]
        return tokens, expected

    def _temporary_path(self, value: str) -> Path | None:
        root = Path(getattr(self.runtime, "working_dir", ".")).resolve()
        candidate = Path(str(value)).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _failure(failure_kind: str, error: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "failure_kind": str(failure_kind or "desktop_adapter_failed")[:80],
                "error": str(error)[:240], **extra}


__all__ = ["DesktopAppAdapters"]
