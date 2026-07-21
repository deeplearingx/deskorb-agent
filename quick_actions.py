"""Definitions for activity-window productivity quick actions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class QuickAction:
    """A named, fixed prompt for one activity-window action."""

    id: str
    label: str
    prompt: str


_COMMON = """You are handling the visible contents of the active window titled {window_title!r}.
Use only information visible in the attached image. Do not infer or invent information
that is outside the visible content. Respond in the language used by this conversation;
if that is unclear, use the dominant language in the window."""


QUICK_ACTIONS: tuple[QuickAction, ...] = (
    QuickAction(
        "summarize",
        "总结",
        _COMMON + """

Provide:
1. A concise summary.
2. The most important points.
3. Explicit decisions, risks, or open questions, if visible.""",
    ),
    QuickAction(
        "extract_tasks",
        "提取待办",
        _COMMON + """

Extract actionable tasks as a checklist. For every task, state the task, owner,
and deadline. Use “未说明” for any owner or deadline that is not visible.""",
    ),
    QuickAction(
        "draft_reply",
        "草拟回复",
        _COMMON + """

Draft one concise, professional reply to the visible request or discussion. Do not
claim facts, commitments, or deadlines that are not visible. Return only the draft.""",
    ),
    QuickAction(
        "explain",
        "解释内容",
        _COMMON + """

Explain the visible content in plain language. Define important terms and identify
any ambiguity or uncertainty that cannot be resolved from the visible content.""",
    ),
)


def get_quick_action(action_id: str) -> QuickAction:
    """Return the action selected by a UI button, or reject an unknown ID."""
    for action in QUICK_ACTIONS:
        if action.id == action_id:
            return action
    raise ValueError(f"Unknown quick action: {action_id}")


def build_quick_action_prompt(action_id: str, window_title: str) -> str:
    """Build the fixed instruction sent with the captured active-window image."""
    action = get_quick_action(action_id)
    return action.prompt.format(window_title=window_title or "untitled window")
