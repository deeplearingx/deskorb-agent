"""Declarative definitions and result handling for office workflows."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class WorkflowParameter:
    """A small, UI-agnostic workflow form field."""

    id: str
    label: str
    default: str


@dataclass(frozen=True)
class WorkflowOutputField:
    """A structured field saved alongside the readable workflow result."""

    id: str
    label: str
    value_type: str = "text"


@dataclass(frozen=True)
class WorkflowDefinition:
    """The complete declaration for one built-in workflow."""

    id: str
    label: str
    parameters: tuple[WorkflowParameter, ...]
    allowed_sources: frozenset[str]
    output_fields: tuple[WorkflowOutputField, ...]
    instruction: str


@dataclass(frozen=True)
class WorkflowSource:
    """One transient input source for a workflow run; its content is never persisted."""

    source_type: str
    summary: str
    content: str
    source_id: str = ""
    status: str = ""


@dataclass(frozen=True)
class ParsedWorkflowResult:
    """Markdown shown to the user plus optional validated record data."""

    markdown: str
    data: dict[str, Any] | None


_COMMON_PARAMETERS = (
    WorkflowParameter("language", "Output language", "follow the conversation language"),
)
_COMMON_SOURCES = frozenset({"active_window", "text", "file", "word_document"})


WORKFLOWS: tuple[WorkflowDefinition, ...] = (
    WorkflowDefinition(
        id="meeting_minutes",
        label="会议纪要",
        parameters=_COMMON_PARAMETERS + (WorkflowParameter("detail", "Detail", "concise"),),
        allowed_sources=_COMMON_SOURCES,
        output_fields=(
            WorkflowOutputField("summary", "Summary"),
            WorkflowOutputField("decisions", "Decisions", "list"),
            WorkflowOutputField("action_items", "Action items", "list"),
            WorkflowOutputField("open_questions", "Open questions", "list"),
        ),
        instruction=(
            "Create meeting minutes with a concise summary, explicit decisions, actionable "
            "items, and questions that remain unresolved."
        ),
    ),
    WorkflowDefinition(
        id="action_tracker",
        label="行动项追踪",
        parameters=_COMMON_PARAMETERS + (WorkflowParameter("scope", "Scope", "all visible actions"),),
        allowed_sources=_COMMON_SOURCES,
        output_fields=(
            WorkflowOutputField("summary", "Summary"),
            WorkflowOutputField("action_items", "Action items", "list"),
            WorkflowOutputField("risks", "Risks", "list"),
            WorkflowOutputField("source_notes", "Source notes"),
        ),
        instruction=(
            "Extract and prioritize actionable work. Keep uncertain owners and dates explicitly "
            "marked as unknown instead of inferring them."
        ),
    ),
    WorkflowDefinition(
        id="draft_reply",
        label="回复草拟",
        parameters=_COMMON_PARAMETERS + (WorkflowParameter("tone", "Tone", "professional and concise"),),
        allowed_sources=_COMMON_SOURCES,
        output_fields=(
            WorkflowOutputField("suggested_reply", "Suggested reply"),
            WorkflowOutputField("tone", "Tone"),
            WorkflowOutputField("commitments_to_confirm", "Commitments to confirm", "list"),
            WorkflowOutputField("risks", "Risks", "list"),
        ),
        instruction=(
            "Draft a ready-to-copy reply. Do not invent commitments, dates, facts, or intent "
            "that are not supported by the supplied sources."
        ),
    ),
    WorkflowDefinition(
        id="post_meeting_followup",
        label="会后跟进",
        parameters=_COMMON_PARAMETERS + (WorkflowParameter("tone", "Tone", "professional and clear"),),
        allowed_sources=_COMMON_SOURCES,
        output_fields=(
            WorkflowOutputField("meeting_recap", "Meeting recap"),
            WorkflowOutputField("action_items", "Action items", "list"),
            WorkflowOutputField("followup_draft", "Follow-up draft"),
            WorkflowOutputField("open_questions", "Open questions", "list"),
        ),
        instruction=(
            "Prepare a factual post-meeting recap, a follow-up message, and a clearly separated "
            "list of actions and outstanding questions."
        ),
    ),
)


_RECORD_TAIL = re.compile(
    r"\s*<!--\s*CODEX_OVERLAY_RECORD\s*\n(?P<payload>.*?)\n\s*-->\s*$",
    re.DOTALL,
)


def get_workflow(workflow_id: str) -> WorkflowDefinition:
    """Return a workflow definition or reject an unknown user selection."""
    for workflow in WORKFLOWS:
        if workflow.id == workflow_id:
            return workflow
    raise ValueError(f"Unknown workflow: {workflow_id}")


def build_workflow_prompt(
    workflow_id: str,
    sources: Sequence[WorkflowSource],
    parameters: Mapping[str, str] | None = None,
) -> str:
    """Build the model prompt from a declaration and explicitly supplied materials."""
    workflow = get_workflow(workflow_id)
    parameter_values = _validate_parameters(workflow, parameters or {})
    source_blocks = []
    for source in sources:
        if source.source_type not in workflow.allowed_sources:
            raise ValueError(
                f"Workflow {workflow.id!r} does not accept source type {source.source_type!r}."
            )
        source_blocks.append(
            f"[Source: {source.source_type}; {source.summary}]\n{source.content.strip()}"
        )

    fields = ", ".join(field.id for field in workflow.output_fields)
    parameter_lines = "\n".join(f"- {name}: {value}" for name, value in parameter_values.items())
    source_text = "\n\n---\n\n".join(source_blocks) or "[No sources were supplied.]"
    example = {"workflow_id": workflow.id}
    example.update({field.id: _example_value(field) for field in workflow.output_fields})
    record_example = json.dumps(example, ensure_ascii=False, indent=2)
    return (
        f"You are running the {workflow.label} workflow.\n\n"
        "Use only the supplied sources. State uncertainty or missing information instead of "
        "guessing about content outside them.\n"
        f"Task: {workflow.instruction}\n\n"
        f"Parameters:\n{parameter_lines}\n\n"
        f"Sources:\n{source_text}\n\n"
        "First return readable Markdown for the user. Then append exactly one machine-readable "
        "record tail, using this exact HTML-comment form and valid JSON. Do not put the record "
        "tail inside a Markdown code fence.\n"
        "<!-- CODEX_OVERLAY_RECORD\n"
        f"{record_example}\n"
        "-->\n\n"
        f"The JSON object must set \"workflow_id\": \"{workflow.id}\" and include these fields: {fields}. "
        "Use an array of objects for action_items; each item should use task, owner, due_date, "
        "priority, and status when that information is available."
    )


def parse_workflow_result(response: str, workflow_id: str) -> ParsedWorkflowResult:
    """Remove the hidden tail and return data only when it matches the selected workflow."""
    workflow = get_workflow(workflow_id)
    match = _RECORD_TAIL.search(response)
    if not match:
        return ParsedWorkflowResult(markdown=response.strip(), data=None)

    visible_markdown = response[:match.start()].rstrip()
    try:
        data = json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return ParsedWorkflowResult(markdown=visible_markdown, data=None)
    if not _is_valid_record_data(data, workflow):
        return ParsedWorkflowResult(markdown=visible_markdown, data=None)
    return ParsedWorkflowResult(markdown=visible_markdown, data=data)


def _validate_parameters(
    workflow: WorkflowDefinition, supplied: Mapping[str, str]
) -> dict[str, str]:
    known = {parameter.id: parameter for parameter in workflow.parameters}
    unknown = set(supplied) - set(known)
    if unknown:
        raise ValueError(f"Unknown parameter(s) for {workflow.id}: {', '.join(sorted(unknown))}")
    return {
        parameter.id: str(supplied.get(parameter.id, parameter.default)).strip() or parameter.default
        for parameter in workflow.parameters
    }


def _is_valid_record_data(data: Any, workflow: WorkflowDefinition) -> bool:
    if not isinstance(data, dict) or data.get("workflow_id") != workflow.id:
        return False
    for field in workflow.output_fields:
        value = data.get(field.id)
        if field.id not in data:
            return False
        if field.value_type == "list":
            if not isinstance(value, list):
                return False
        elif field.value_type == "text":
            if not isinstance(value, str):
                return False
        else:
            return False
    if "action_items" in data and not _has_valid_action_items(data["action_items"]):
        return False
    return True


def _has_valid_action_items(items: Any) -> bool:
    """Validate the portable action-item shape used by the record repository."""
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        task = item.get("task")
        if not isinstance(task, str) or not task.strip():
            return False
        for key in ("owner", "priority", "status"):
            if key in item and not isinstance(item[key], str):
                return False
        if "due_date" in item:
            due_date = item["due_date"]
            if not isinstance(due_date, str):
                return False
            if due_date.strip():
                try:
                    date.fromisoformat(due_date.strip())
                except ValueError:
                    return False
    return True


def _example_value(field: WorkflowOutputField) -> Any:
    return [] if field.value_type == "list" else ""
