"""Small, privacy-aware task graph and evidence verifier for DeskOrb.

The graph is deliberately not a second planner.  It records the execution
nodes the runtime actually performed, links each node to the preceding one,
and distinguishes a successful tool call from evidence that the requested
effect can be observed.
"""
from __future__ import annotations

import time
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


def _contains_marker(text: str, marker: str) -> bool:
    """Match English intent markers as words so ``Windows`` is not ``window``."""
    if marker.isascii() and marker.isalnum():
        return re.search(r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])", text) is not None
    return marker in text


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


@dataclass(frozen=True)
class TaskContract:
    """Small, serializable success contract for one execution task.

    The first contract version deliberately stays deterministic: an action
    task needs at least one explicit evidence node, and common task families
    can require the matching evidence schema.  The model cannot satisfy this
    contract with prose alone.
    """

    goal: str
    requires_verification: bool = False
    required_evidence_schemas: tuple[str, ...] = ()
    goal_predicates: tuple[dict[str, Any], ...] = ()
    version: int = 1

    @classmethod
    def from_goal(cls, goal: str, *, requires_action: bool = False) -> "TaskContract":
        text = " ".join(str(goal or "").lower().split())
        schemas: list[str] = []
        predicates: list[dict[str, Any]] = []
        if any(marker in text for marker in ("淘宝", "商品", "价格", "网页", "网站", "搜索", "浏览器", "browser", "web")):
            schemas.append("browser_structured_verification")
            price_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:到|至|[-~])\s*(\d+(?:\.\d+)?)\s*元?", text)
            if price_match:
                lower, upper = float(price_match.group(1)), float(price_match.group(2))
                predicates.append({"type": "price_range", "minimum": min(lower, upper),
                                   "maximum": max(lower, upper), "currency": "CNY"})
            terms = []
            if "t恤" in text or "t 恤" in text:
                terms.append("T 恤")
            if terms:
                predicates.append({"type": "contains", "values": terms})
            for marker, origin in (("淘宝", "taobao.com"), ("京东", "jd.com"), ("天猫", "tmall.com")):
                if marker in text:
                    predicates.append({"type": "origin", "value": origin})
                    break
            if any(marker in text for marker in ("搜索", "资料", "商品", "结果", "search", "research")):
                predicates.append({"type": "required_fields", "fields": ["title", "url"]})
        # Message delivery is a separate evidence domain from editing a draft:
        # typing text or dispatching a send button is not proof that the target
        # application delivered it.  QQ's UIA profile emits a bounded delivery
        # marker only after a fresh post-send observation.
        message_intent = any(marker in text for marker in (
            "发消息", "发送消息", "发信息", "私信", "send message", "send a message", "message to",
        ))
        if message_intent and any(marker in text for marker in ("qq", "消息", "信息", "send", "发送", "发")):
            schemas.append("message_delivery")
        if any(marker in text for marker in ("保存", "写入", "创建", "修改", "覆盖")) \
                or re.search(r"\b(?:write|save|create|modify|overwrite)\b", text):
            schemas.append("path_and_content_hash")
        # Browser page tasks commonly begin with “打开浏览器”.  Their
        # structured page snapshot is the authoritative evidence; requiring a
        # second desktop delta for the launcher would make a valid browser
        # task impossible to complete.  Add the desktop schema only when the
        # goal explicitly names a desktop/window/control/application concern.
        desktop_markers = ("窗口", "记事本", "桌面", "应用", "qq", "计算器",
                           "window", "desktop", "notepad", "calculator", "application")
        explicit_desktop = any(_contains_marker(text, marker) for marker in desktop_markers)
        # Browser prompts often mention that the agent must not type into a
        # search box.  That is a browser policy constraint, not a desktop UI
        # action.  Keep the generic input marker only for non-browser tasks;
        # named desktop/window markers remain explicit and still win.
        generic_desktop_input = ("输入" in text and "browser_structured_verification" not in schemas)
        if (explicit_desktop or generic_desktop_input) and "message_delivery" not in schemas:
            schemas.append("desktop_state_delta")
        return cls(" ".join(str(goal or "Desktop task").split())[:240],
                   requires_verification=bool(requires_action),
                   required_evidence_schemas=tuple(dict.fromkeys(schemas)),
                   goal_predicates=tuple(predicates))

    @classmethod
    def from_dict(cls, value: Any, fallback_goal: str) -> "TaskContract":
        if not isinstance(value, dict) or "requires_verification" not in value:
            # Legacy recoverable tasks predate contracts and are action tasks
            # by definition; restore them under the strict safe default.
            return cls.from_goal(fallback_goal, requires_action=True)
        schemas = tuple(str(item) for item in value.get("required_evidence_schemas") or () if str(item).strip())
        predicates = tuple(item for item in value.get("goal_predicates") or ()
                          if isinstance(item, dict))
        try:
            version = int(value.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        return cls(" ".join(str(value.get("goal") or fallback_goal).split())[:240],
                   requires_verification=bool(value.get("requires_verification")),
                   required_evidence_schemas=schemas,
                   goal_predicates=predicates,
                   version=version)

    def safe_dict(self) -> dict[str, Any]:
        return {"version": self.version, "goal": self.goal,
                "requires_verification": self.requires_verification,
                "required_evidence_schemas": list(self.required_evidence_schemas),
                "goal_predicates": [dict(item) for item in self.goal_predicates[:8]]}

    def browser_postconditions(self) -> dict[str, Any]:
        """Compile bounded browser predicates into verifier arguments."""
        result: dict[str, Any] = {}
        contains: list[str] = []
        fields: list[str] = []
        for predicate in self.goal_predicates:
            kind = str(predicate.get("type") or "")
            if kind == "price_range":
                try:
                    result["price_min"] = float(predicate["minimum"])
                    result["price_max"] = float(predicate["maximum"])
                except (KeyError, TypeError, ValueError):
                    continue
            elif kind == "contains":
                contains.extend(str(value).strip() for value in predicate.get("values") or () if str(value).strip())
            elif kind == "required_fields":
                fields.extend(str(value).strip() for value in predicate.get("fields") or () if str(value).strip())
            elif kind == "origin" and str(predicate.get("value") or "").strip():
                result["origin"] = str(predicate["value"]).strip()[:120]
        if contains:
            result["contains"] = list(dict.fromkeys(contains))
        if fields:
            result["required_fields"] = list(dict.fromkeys(fields))
        return result

    def verify(self, nodes: list[WorkflowNode]) -> bool:
        if not self.requires_verification:
            return True
        action_nodes = [node for node in nodes if node.kind in {"action", "verification"}]
        if not action_nodes:
            return True
        evidence_nodes = [node for node in nodes if node.evidence and node.status is NodeStatus.SUCCEEDED]
        if not evidence_nodes:
            return False
        if not self.required_evidence_schemas:
            return True
        # A composite task (for example, search in a browser and save the
        # verified result to a file) is complete only when every declared
        # evidence domain has been observed.  Accepting ``any`` here lets one
        # successful sub-step hide an unfinished side effect.
        observed_schemas = {node.evidence_schema for node in evidence_nodes}
        return all(schema in observed_schemas for schema in self.required_evidence_schemas)


class TaskWorkflow:
    """Append-only execution graph with bounded, structural evidence only."""

    def __init__(self, task_id: str, goal: str, *, contract: TaskContract | None = None,
                 clock=time.monotonic):
        self.task_id = str(task_id)
        self.goal = " ".join(str(goal or "Desktop task").split())[:240]
        # Direct workflow users do not have the runtime's intent classifier;
        # default to the strict action contract and allow pure answer tasks
        # through the no-action branch in TaskContract.verify().
        self.contract = contract or TaskContract.from_goal(self.goal, requires_action=True)
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
                            evidence_schema=self._evidence_schema(str(tool_name), result),
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
        requested_terminal = str(terminal)
        actions = [node for node in self._nodes if node.kind in {"action", "verification"} and node.status is NodeStatus.SUCCEEDED]
        # A pure answer/observation task has no claimed external effect.  For a
        # task that changed something, require a separately observable result.
        self._verified = self.contract.verify(self._nodes)
        # Do not let a successful model response turn an unverified action
        # sequence into a completed task.  The runtime may later resume this
        # state after a fresh observation or an explicit verifier result.
        self._terminal = ("waiting_verification"
                          if requested_terminal == "completed" and actions and not self._verified
                          else requested_terminal)
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
            "contract": self.contract.safe_dict(),
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
    def _evidence_schema(tool_name: str, result: dict[str, Any] | None = None) -> str:
        if isinstance(result, dict) and isinstance(result.get("verification"), dict):
            if str(result["verification"].get("kind") or "") == "message_delivery":
                return "message_delivery"
        if tool_name == "filesystem_write":
            return "path_and_content_hash"
        if tool_name == "shell_run":
            return "command_exit_status"
        if tool_name in {"desktop_verify_state", "window_control", "window_focus",
                         "desktop_uia_invoke", "desktop_uia_set_value"}:
            return "desktop_state_delta"
        if tool_name == "browser_action_batch" or (tool_name.startswith("mcp_") and "playwright" in tool_name):
            return "browser_structured_verification"
        return "tool_ok_only"

    @staticmethod
    def _has_verifiable_evidence(tool_name: str, result: dict[str, Any]) -> bool:
        if bool(result.get("verified")):
            return True
        if tool_name == "desktop_verify_state":
            return bool(result.get("screen_changed") or result.get("active_window_changed"))
        if tool_name == "shell_run":
            try:
                return int(result.get("exit_code")) == 0
            except (TypeError, ValueError):
                return False
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
