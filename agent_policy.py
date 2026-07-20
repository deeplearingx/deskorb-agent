"""Deterministic local safety policy for DeskOrb Agent tools.

The model may request a tool, but this module is the only authority that can
allow it, require a fresh user confirmation, or deny it.  It contains no model
calls and is intentionally usable before mutating tools are implemented.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Risk(str, Enum):
    OBSERVE = "observe"
    REVERSIBLE_LOCAL = "reversible_local"
    DESTRUCTIVE_LOCAL = "destructive_local"
    EXTERNAL_OR_ELEVATED = "external_or_elevated"


class DecisionKind(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyDecision:
    kind: DecisionKind
    risk: Risk
    reason: str


@dataclass(frozen=True)
class ApprovalRequest:
    token: str
    tool_name: str
    arguments: dict[str, Any]
    risk: Risk
    summary: str
    created_at: float
    expires_at: float

    def prompt(self) -> str:
        return f"⚠ Confirmation required: {self.summary}\nReply exactly: 确认 {self.token}\nReply 取消 to stop."


class ToolPolicy:
    """Policy classification independent of natural-language model output."""

    OBSERVE_TOOLS = {
        "desktop_get_active_window", "desktop_screenshot", "filesystem_list",
        "filesystem_read_text", "filesystem_search_text", "process_list", "window_list", "desktop_capture_state", "desktop_verify_state",
    }
    REVERSIBLE_TOOLS = {"filesystem_write", "filesystem_patch", "filesystem_copy"}
    DESTRUCTIVE_TOOLS = {"filesystem_move", "filesystem_delete", "process_stop", "shell_run"}
    EXTERNAL_TOOLS = {"desktop_click", "desktop_type", "desktop_hotkey", "desktop_scroll", "window_focus",
                      "process_start", "application_launch"}
    TASK_SCOPED_TOOLS = EXTERNAL_TOOLS

    def decide(self, tool_name: str, *, execution_requested: bool, full_access: bool,
               task_authorized: bool = False, high_risk: bool = False) -> PolicyDecision:
        if tool_name in self.OBSERVE_TOOLS:
            return PolicyDecision(DecisionKind.ALLOW, Risk.OBSERVE, "Read-only observation")
        if tool_name not in self.REVERSIBLE_TOOLS | self.DESTRUCTIVE_TOOLS | self.EXTERNAL_TOOLS:
            return PolicyDecision(DecisionKind.DENY, Risk.EXTERNAL_OR_ELEVATED, "Unknown tool")
        if not full_access:
            return PolicyDecision(DecisionKind.DENY, Risk.EXTERNAL_OR_ELEVATED, "Read-only mode is enabled")
        if not execution_requested:
            return PolicyDecision(DecisionKind.DENY, Risk.EXTERNAL_OR_ELEVATED,
                                  "The user did not explicitly request an action")
        if high_risk:
            return PolicyDecision(DecisionKind.CONFIRM, Risk.EXTERNAL_OR_ELEVATED,
                                  "This step has an external or sensitive effect")
        if tool_name in self.REVERSIBLE_TOOLS:
            return PolicyDecision(DecisionKind.ALLOW, Risk.REVERSIBLE_LOCAL,
                                  "Explicit, reversible local action")
        if tool_name in self.TASK_SCOPED_TOOLS and task_authorized:
            return PolicyDecision(DecisionKind.ALLOW, Risk.EXTERNAL_OR_ELEVATED,
                                  "Covered by the active task authorization")
        risk = Risk.DESTRUCTIVE_LOCAL if tool_name in self.DESTRUCTIVE_TOOLS else Risk.EXTERNAL_OR_ELEVATED
        return PolicyDecision(DecisionKind.CONFIRM, risk, "Fresh confirmation required")


class ApprovalManager:
    """Holds one short-lived, explicitly confirmed high-risk action."""

    def __init__(self, ttl_seconds: int = 120, clock=time.monotonic):
        self.ttl_seconds = max(15, int(ttl_seconds))
        self.clock = clock
        self.pending: ApprovalRequest | None = None

    def create(self, tool_name: str, arguments: dict[str, Any], risk: Risk, summary: str) -> ApprovalRequest:
        now = self.clock()
        request = ApprovalRequest(
            token=secrets.token_hex(3).upper(), tool_name=tool_name, arguments=dict(arguments),
            risk=risk, summary=summary, created_at=now, expires_at=now + self.ttl_seconds,
        )
        self.pending = request
        return request

    def resolve(self, user_text: str) -> tuple[str, ApprovalRequest | None]:
        request = self.pending
        if request is None:
            return "none", None
        if self.clock() > request.expires_at:
            self.pending = None
            return "expired", request
        text = str(user_text).strip()
        if text in {"取消", "cancel", "CANCEL"}:
            self.pending = None
            return "cancelled", request
        if text == f"确认 {request.token}":
            self.pending = None
            return "approved", request
        return "pending", request
