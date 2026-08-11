"""Validation for bounded, semantic browser action batches.

This deliberately has no raw JavaScript, CDP, request, upload, download, or
shell escape hatch.  A backend may execute a validated batch only through its
own already-allowlisted structured browser tools.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


ALLOWED_ACTIONS = frozenset({"navigate", "snapshot", "click_ref", "fill_ref", "wait", "switch_tab", "extract", "verify"})
MAX_ACTIONS = 8
_MAX_REF_CHARS = 80
_MAX_FIELD_CHARS = 80
_SEMANTIC_FIELD_NAME = re.compile(r"^[^\s\[\]{}()<>/\\\\:;|]+$")


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
        if action in {"click_ref", "fill_ref", "extract"}:
            ref = str(arguments.get("ref") or "").strip()
            if not ref:
                return [], f"{action} requires a structured page reference."
            if len(ref) > _MAX_REF_CHARS or any(character.isspace() for character in ref):
                return [], f"{action} requires a bounded structured page reference."
        if action == "fill_ref" and not isinstance(arguments.get("value"), str):
            return [], "fill_ref requires a string value."
        if action == "switch_tab":
            index = arguments.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                return [], "switch_tab requires a non-negative tab index."
        if action == "extract":
            fields = arguments.get("fields", [])
            if fields is not None and (not isinstance(fields, list) or len(fields) > 16
                                       or any(not isinstance(field, str) or not field.strip()
                                              or len(field.strip()) > _MAX_FIELD_CHARS
                                              or not _SEMANTIC_FIELD_NAME.fullmatch(field.strip())
                                              for field in fields)):
                return [], "extract fields must be a list of at most 16 bounded semantic names."
        if action == "verify":
            contains = arguments.get("contains")
            if contains is not None and not (isinstance(contains, str) or
                                             (isinstance(contains, list) and len(contains) <= 16
                                              and all(isinstance(item, str) and item.strip() for item in contains))):
                return [], "verify contains must be a string or a list of at most 16 non-empty strings."
            for key in ("price_min", "price_max"):
                if key in arguments:
                    try:
                        float(arguments[key])
                    except (TypeError, ValueError):
                        return [], f"verify {key} must be numeric."
            required_fields = arguments.get("required_fields")
            if required_fields is not None and (not isinstance(required_fields, list) or len(required_fields) > 16
                                                or any(not isinstance(field, str) or not field.strip() for field in required_fields)):
                return [], "verify required_fields must be a list of at most 16 non-empty names."
        validated.append(BrowserAction(action, dict(arguments)))
    return validated, None
