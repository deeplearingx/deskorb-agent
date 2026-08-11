"""Runtime-owned task state shared by the production loop and E2E runners.

This module is intentionally an adapter around :mod:`task_runtime` and
:mod:`workflow_runtime`.  The model still chooses tools, but only this state
object may publish a terminal progress record.  It stores structural facts
about execution and never copies tool arguments or model text into the task
journal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from task_runtime import (
    InMemoryTaskJournal,
    TASK_STATUS_ACTIVE,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_WAITING_HUMAN,
    TASK_STATUS_WAITING_VERIFICATION,
    classify_failure,
)
from workflow_runtime import TaskContract, TaskWorkflow, WorkflowNode


def _node_data(node: WorkflowNode) -> dict[str, Any]:
    """Project a workflow node to the bounded fields used by the journal."""
    return {
        "node_id": node.node_id,
        "kind": node.kind,
        "status": node.status.value,
        "evidence": bool(node.evidence),
        "failure_kind": node.failure_kind,
        "evidence_schema": node.evidence_schema,
    }


@dataclass
class RuntimeTaskState:
    """One runtime task's contract, workflow graph and privacy-safe journal."""

    journal: Any
    task_id: str
    goal: str
    contract: TaskContract
    workflow: TaskWorkflow
    action_steps: int = 0
    confirmation_count: int = 0
    handoff_count: int = 0
    last_failure_kind: str | None = None

    @classmethod
    def start(cls, journal: Any | None, goal: str, *, requires_action: bool = False) -> "RuntimeTaskState":
        selected_journal = journal if journal is not None else InMemoryTaskJournal()
        normalized_goal = " ".join(str(goal or "Desktop task").split())[:240]
        task_id = selected_journal.start(normalized_goal)
        contract = TaskContract.from_goal(normalized_goal, requires_action=requires_action)
        selected_journal.set_contract(task_id, contract.safe_dict())
        workflow = TaskWorkflow(task_id, normalized_goal, contract=contract)
        selected_journal.event(task_id, "workflow_started", {
            "contract_version": contract.version,
            "requires_verification": contract.requires_verification,
            "required_evidence_schemas": list(contract.required_evidence_schemas),
        })
        return cls(selected_journal, task_id, normalized_goal, contract, workflow)

    def record_tool_result(self, tool_name: str, result: dict[str, Any], *,
                           failure_kind: str | None = None) -> WorkflowNode:
        """Record only structural tool outcome and checkpoint data."""
        safe_result = result if isinstance(result, dict) else {}
        failure = failure_kind
        if failure is None and not bool(safe_result.get("ok")):
            failure = classify_failure(safe_result.get("error"))
        if failure:
            self.last_failure_kind = failure
        node = self.workflow.record_tool_result(str(tool_name), safe_result, failure_kind=failure)
        if node.kind in {"action", "verification"}:
            self.action_steps += 1
        self.journal.event(self.task_id, "workflow_node", _node_data(node))
        self.journal.event(self.task_id, "tool_result", {
            "tool": str(tool_name),
            "ok": bool(safe_result.get("ok")),
            "verified": bool(safe_result.get("verified")),
            "evidence": bool(node.evidence),
            "evidence_schema": node.evidence_schema,
            "failure_kind": failure,
        })
        self.journal.checkpoint(self.task_id, {
            "last_tool": str(tool_name),
            "last_status": node.status.value,
            "steps": len(self.workflow.nodes),
            "action_steps": self.action_steps,
            "evidence_steps": sum(bool(item.evidence) for item in self.workflow.nodes),
        })
        return node

    def waiting_for_human(self, reason: str = "human verification") -> dict[str, Any]:
        self.handoff_count += 1
        self.workflow.waiting_for_human()
        self.journal.event(self.task_id, "human_handoff", {
            "required": True,
            "handoff_count": self.handoff_count,
            "reason_kind": classify_failure(reason),
        })
        self.journal.set_status(self.task_id, TASK_STATUS_WAITING_HUMAN)
        return self.progress()

    def resumed_by_human(self) -> dict[str, Any]:
        self.workflow.resumed_by_human()
        self.journal.event(self.task_id, "human_handoff", {
            "required": False,
            "resumed": True,
            "fresh_observation_required": True,
        })
        self.journal.set_status(self.task_id, TASK_STATUS_ACTIVE)
        return self.progress()

    def record_confirmation(self) -> None:
        self.confirmation_count += 1
        self.journal.event(self.task_id, "approval_requested", {
            "confirmation_count": self.confirmation_count,
        })

    def progress(self) -> dict[str, Any]:
        value = dict(self.workflow.progress())
        value.update({
            "action_steps": self.action_steps,
            "confirmation_count": self.confirmation_count,
            "handoff_count": self.handoff_count,
        })
        return value

    def finish(self, terminal: str, *, failure_kind: str | None = None) -> dict[str, Any]:
        """Verify the graph, publish one terminal event, and close the journal state."""
        progress = self.workflow.finish(str(terminal))
        effective_terminal = str(progress.get("terminal") or terminal)
        if effective_terminal != "completed":
            # A blocked, failed, or handoff state is never a verified success,
            # even when the contract has no required evidence domain.
            progress["verified"] = False
        if failure_kind is None:
            failure_kind = self.last_failure_kind
        if failure_kind is None and effective_terminal == "waiting_verification":
            failure_kind = "verification_failed"
        if effective_terminal == "completed" and bool(progress.get("verified")):
            status = TASK_STATUS_COMPLETED
        elif effective_terminal == "waiting_verification":
            status = TASK_STATUS_WAITING_VERIFICATION
        elif effective_terminal == "waiting_human":
            status = TASK_STATUS_WAITING_HUMAN
        elif effective_terminal in {"blocked", "failed", "partial"}:
            status = TASK_STATUS_FAILED
        else:
            status = TASK_STATUS_ACTIVE
        self.journal.event(self.task_id, "workflow_finished", {
            "terminal": effective_terminal,
            "verified": bool(progress.get("verified")),
            "steps": int(progress.get("steps") or 0),
            "failed_steps": int(progress.get("failed_steps") or 0),
            "evidence_steps": int(progress.get("evidence_steps") or 0),
            "failure_kind": failure_kind,
        })
        self.journal.set_status(self.task_id, status, failure=failure_kind)
        progress.update({
            "terminal": effective_terminal,
            "failure_kind": failure_kind,
            "action_steps": self.action_steps,
            "confirmation_count": self.confirmation_count,
            "handoff_count": self.handoff_count,
        })
        return progress
