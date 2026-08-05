"""Small, privacy-aware task graph and evidence verifier for DeskOrb.

The graph is deliberately not a second planner.  It records the execution
nodes the runtime actually performed, links each node to the preceding one,
and distinguishes a successful tool call from evidence that the requested
effect can be observed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class NodeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WAITING_HUMAN = "waiting_human"


@dataclass(frozen=True)
class WorkflowNode:
    node_id: int
    kind: str
    label: str
    status: NodeStatus
    parent_id: int | None
    evidence: bool
    failure_kind: str | None
    created_at: float
    precondition: dict[str, Any]
    postcondition: dict[str, Any]
    evidence_schema: str
    retry_policy: dict[str, Any]


class TaskWorkflow:
    """Append-only execution graph with bounded, structural evidence only."""

    def __init__(self, task_id: str, goal: str, *, clock=time.monotonic):
        self.task_id = str(task_id)
        self.goal = " ".join(str(goal or "Desktop task").split())[:240]
        self._clock = clock
        self._nodes: list[WorkflowNode] = []
        self._terminal: str | None = None
        self._verified: bool | None = None

    @property
    def nodes(self) -> tuple[WorkflowNode, ...]:
        return tuple(self._nodes)

    def record_tool_result(self, tool_name: str, result: dict[str, Any], *, failure_kind: str | None = None,
                           precondition: dict[str, Any] | None = None,
                           retry_policy: dict[str, Any] | None = None) -> WorkflowNode:
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        evidence = ok and self._has_verifiable_evidence(str(tool_name), result)
        return self._append("verification" if evidence else "action", str(tool_name),
                            NodeStatus.SUCCEEDED if ok else NodeStatus.FAILED,
                            evidence=evidence, failure_kind=failure_kind,
                            precondition=precondition or {"authorized": True},
                            postcondition={"ok": ok, "evidence": evidence},
                            evidence_schema=self._evidence_schema(str(tool_name)),
                            retry_policy=retry_policy or {"max_attempts": 0, "retryable": False})

    def waiting_for_human(self, reason: str = "human verification") -> WorkflowNode:
        return self._append("handoff", str(reason)[:120], NodeStatus.WAITING_HUMAN, evidence=False,
                            precondition={"human_action_required": True},
                            postcondition={"resumed": False}, evidence_schema="human_handoff",
                            retry_policy={"max_attempts": 0, "retryable": False})

    def resumed_by_human(self) -> WorkflowNode:
        return self._append("handoff", "human control returned to agent", NodeStatus.SUCCEEDED, evidence=False,
                            precondition={"user_confirmed": True}, postcondition={"resumed": True},
                            evidence_schema="fresh_snapshot_required", retry_policy={"max_attempts": 0, "retryable": False})

    def resumed_from_checkpoint(self) -> WorkflowNode:
        return self._append("checkpoint", "task resumed from checkpoint", NodeStatus.SUCCEEDED, evidence=False,
                            precondition={"user_confirmed": True}, postcondition={"reobserve_required": True},
                            evidence_schema="fresh_observation_required", retry_policy={"max_attempts": 0, "retryable": False})

    def finish(self, terminal: str) -> dict[str, Any]:
        self._terminal = str(terminal)
        actions = [node for node in self._nodes if node.kind in {"action", "verification"} and node.status is NodeStatus.SUCCEEDED]
        # A pure answer/observation task has no claimed external effect.  For a
        # task that changed something, require a separately observable result.
        self._verified = not actions or any(node.evidence for node in actions)
        return self.progress()

    def progress(self) -> dict[str, Any]:
        failed = sum(node.status is NodeStatus.FAILED for node in self._nodes)
        waiting = any(node.status is NodeStatus.WAITING_HUMAN for node in self._nodes[-1:])
        return {
            "task_id": self.task_id,
            "steps": len(self._nodes),
            "failed_steps": failed,
            "waiting_human": waiting,
            "evidence_steps": sum(node.evidence for node in self._nodes),
            "terminal": self._terminal,
            "verified": self._verified,
        }

    def _append(self, kind: str, label: str, status: NodeStatus, *, evidence: bool,
                failure_kind: str | None = None, precondition: dict[str, Any] | None = None,
                postcondition: dict[str, Any] | None = None, evidence_schema: str = "none",
                retry_policy: dict[str, Any] | None = None) -> WorkflowNode:
        node = WorkflowNode(len(self._nodes) + 1, kind, label[:120], status,
                            self._nodes[-1].node_id if self._nodes else None,
                            evidence, failure_kind, self._clock(), precondition or {},
                            postcondition or {}, evidence_schema, retry_policy or {})
        self._nodes.append(node)
        return node

    @staticmethod
    def _evidence_schema(tool_name: str) -> str:
        if tool_name == "filesystem_write":
            return "path_and_content_hash"
        if tool_name == "desktop_verify_state":
            return "desktop_state_delta"
        if tool_name == "browser_action_batch":
            return "browser_structured_verification"
        return "tool_ok_only"

    @staticmethod
    def _has_verifiable_evidence(tool_name: str, result: dict[str, Any]) -> bool:
        if bool(result.get("verified")):
            return True
        if tool_name == "desktop_verify_state":
            return bool(result.get("screen_changed") or result.get("active_window_changed"))
        # A browser extraction is evidence only when its trusted adapter gives
        # an explicit, structured verification signal; text alone is not proof.
        return bool(result.get("verification", {}).get("passed")) if isinstance(result.get("verification"), dict) else False


def verify_price_candidates(items: list[dict[str, Any]], minimum: float, maximum: float) -> dict[str, Any]:
    """Validate structured product evidence without trusting model prose."""
    valid: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            price = float(item["price_cny"])
        except (KeyError, TypeError, ValueError):
            continue
        title, url = str(item.get("title") or "").strip(), str(item.get("url") or "").strip()
        if title and url.startswith(("http://", "https://")) and minimum <= price <= maximum:
            valid.append({"title": title[:240], "price_cny": price, "url": url[:500],
                          "rating": item.get("rating")})
    return {"ok": bool(valid), "verified": bool(valid), "candidates": valid,
            "minimum": minimum, "maximum": maximum}
