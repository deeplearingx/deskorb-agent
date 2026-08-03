"""Validation for bounded, semantic browser action batches.

This deliberately has no raw JavaScript, CDP, request, upload, download, or
shell escape hatch.  A backend may execute a validated batch only through its
own already-allowlisted structured browser tools.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ALLOWED_ACTIONS = frozenset({"navigate", "snapshot", "click_ref", "fill_ref", "wait", "switch_tab", "extract", "verify"})
MAX_ACTIONS = 8


@dataclass(frozen=True)
class BrowserAction:
    action: str
    arguments: dict[str, Any]


def validate_browser_action_batch(value: Any) -> tuple[list[BrowserAction], str | None]:
    """Return a bounded batch or a safe rejection reason."""
    if not isinstance(value, list) or not value or len(value) > MAX_ACTIONS:
        return [], f"A browser batch must contain 1-{MAX_ACTIONS} semantic actions."
    validated: list[BrowserAction] = []
    for item in value:
        if not isinstance(item, dict):
            return [], "Each browser action must be an object."
        action = str(item.get("action") or "").strip().lower()
        arguments = item.get("arguments")
        if action not in ALLOWED_ACTIONS or not isinstance(arguments, dict):
            return [], "Browser batch contains an unsupported action."
        if action == "navigate":
            url = str(arguments.get("url") or "")
            if not url.startswith(("http://", "https://")):
                return [], "Navigate requires an http(s) URL."
        if action in {"click_ref", "fill_ref", "extract"} and not str(arguments.get("ref") or ""):
            return [], f"{action} requires a structured page reference."
        if action == "fill_ref" and not isinstance(arguments.get("value"), str):
            return [], "fill_ref requires a string value."
        validated.append(BrowserAction(action, dict(arguments)))
    return validated, None
