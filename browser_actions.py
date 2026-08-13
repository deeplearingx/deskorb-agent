"""Validation for bounded, semantic browser action batches.

This deliberately has no raw JavaScript, CDP, request, upload, download, or
shell escape hatch.  A backend may execute a validated batch only through its
own already-allowlisted structured browser tools.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


ALLOWED_ACTIONS = frozenset({
    "navigate", "snapshot", "click_ref", "fill_ref", "select_ref", "wait",
    "switch_tab", "extract", "verify",
})
STATE_CHANGING_ACTIONS = frozenset({
    "navigate", "click_ref", "fill_ref", "select_ref", "wait", "switch_tab",
})
MAX_ACTIONS = 8
_MAX_REF_CHARS = 80
_MAX_FIELD_CHARS = 80
_SEMANTIC_FIELD_NAME = re.compile(r"^[^\s\[\]{}()<>/\\\\:;|]+$")


def _semantic_field_alias(value: Any) -> str:
    """Map common model shorthand to one bounded semantic evidence field."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if any(marker in text for marker in ("title", "heading", "name", "标题", "名称", "h1", "h2")):
        return "title"
    if any(marker in text for marker in ("price", "价格", "售价")):
        return "price"
    if any(marker in text for marker in ("source", "来源")) or text in {"p", "paragraph"}:
        return "source"
    if any(marker in text for marker in ("url", "link", "链接")) or text in {"a", "anchor", "href"}:
        return "url"
    return text if _SEMANTIC_FIELD_NAME.fullmatch(text) else ""


def _normalize_field_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized = [_semantic_field_alias(item) for item in values]
    return list(dict.fromkeys(field for field in normalized if field))


def _normalize_arguments(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize harmless model vocabulary aliases into the strict contract."""
    normalized = dict(arguments)
    if action in {"click_ref", "fill_ref", "select_ref", "extract"}:
        if "ref" not in normalized and isinstance(normalized.get("target"), str):
            normalized["ref"] = normalized["target"]
        if "ref" not in normalized and isinstance(normalized.get("refs"), list):
            refs = [item for item in normalized["refs"] if isinstance(item, str) and item.strip()]
            if len(refs) == 1:
                normalized["ref"] = refs[0]
    if action == "fill_ref" and "value" not in normalized and isinstance(normalized.get("text"), str):
        normalized["value"] = normalized["text"]
    if action == "select_ref" and "values" not in normalized:
        if isinstance(normalized.get("value"), str):
            normalized["values"] = [normalized["value"]]
        elif isinstance(normalized.get("options"), list):
            normalized["values"] = normalized["options"]
    if action == "wait":
        if "ms" not in normalized and "duration_ms" in normalized:
            normalized["ms"] = normalized["duration_ms"]
        if "ms" not in normalized and "milliseconds" in normalized:
            normalized["ms"] = normalized["milliseconds"]
    if action == "extract" and "fields" not in normalized:
        raw_fields = normalized.get("selectors")
        if not isinstance(raw_fields, list):
            raw_fields = normalized.get("field_names")
        if isinstance(raw_fields, list):
            normalized["fields"] = _normalize_field_names(raw_fields)
    if action == "extract" and isinstance(normalized.get("fields"), list):
        normalized["fields"] = _normalize_field_names(normalized["fields"])
    if action == "verify":
        if "contains" not in normalized and "expected" in normalized:
            expected = normalized["expected"]
            if isinstance(expected, (str, list)):
                normalized["contains"] = expected
            elif isinstance(expected, dict):
                normalized["expected"] = {
                    _semantic_field_alias(key): value for key, value in expected.items()
                    if _semantic_field_alias(key)
                }
                normalized.setdefault("required_fields", list(normalized["expected"]))
                normalized["contains"] = [str(value) for value in expected.values()]
        if isinstance(normalized.get("required_fields"), list):
            normalized["required_fields"] = _normalize_field_names(normalized["required_fields"])
        if "required_fields" not in normalized and isinstance(normalized.get("fields"), list):
            normalized["required_fields"] = _normalize_field_names(normalized["fields"])
        normalized.setdefault("required_fields", [])
    return normalized


@dataclass(frozen=True)
class BrowserAction:
    action: str
    arguments: dict[str, Any]


def validate_browser_action_batch(value: Any) -> tuple[list[BrowserAction], str | None]:
    """Return a bounded batch or a safe rejection reason."""
    if not isinstance(value, list) or not value or len(value) > MAX_ACTIONS:
        return [], f"A browser batch must contain 1-{MAX_ACTIONS} semantic actions."
    validated: list[BrowserAction] = []
    state_change_indexes: list[int] = []
    for item in value:
        if not isinstance(item, dict):
            return [], "Each browser action must be an object."
        action = str(item.get("action") or "").strip().lower()
        arguments = item.get("arguments")
        if action not in ALLOWED_ACTIONS or not isinstance(arguments, dict):
            return [], "Browser batch contains an unsupported action."
        arguments = _normalize_arguments(action, arguments)
        if action == "navigate":
            url = str(arguments.get("url") or "")
            if not url.startswith(("http://", "https://")):
                return [], "Navigate requires an http(s) URL."
        if action == "switch_tab":
            index = arguments.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                return [], "switch_tab requires a non-negative tab index."
        if action in {"click_ref", "fill_ref", "select_ref", "extract", "switch_tab"}:
            ref = str(arguments.get("ref") or "").strip()
            if action in {"click_ref", "fill_ref", "select_ref", "extract"} and not ref:
                return [], f"{action} requires a structured page reference."
            if ref and (len(ref) > _MAX_REF_CHARS or any(character.isspace() for character in ref)):
                return [], f"{action} requires a bounded structured page reference."
            if not str(arguments.get("observation_id") or "").strip():
                return [], f"{action} requires the current observation_id."
        if action == "fill_ref":
            value = arguments.get("value", arguments.get("text"))
            if not isinstance(value, str):
                return [], "fill_ref requires a string value."
            if "value" not in arguments:
                arguments = {**arguments, "value": value}
        if action == "select_ref":
            values = arguments.get("values")
            if (not isinstance(values, list) or not values or len(values) > 16
                    or any(not isinstance(item, str) or not item.strip() for item in values)):
                return [], "select_ref requires a non-empty list of bounded values."
        if action == "wait":
            for key in ("seconds", "time", "ms"):
                if key in arguments:
                    try:
                        number = float(arguments[key])
                    except (TypeError, ValueError):
                        return [], "wait duration must be numeric."
                    if key == "ms":
                        number /= 1000.0
                    if number < 0 or number > 30:
                        return [], "wait duration must be between 0 and 30 seconds."
                    break
        if action == "extract":
            fields = arguments.get("fields", [])
            if (not isinstance(fields, list) or not fields or len(fields) > 16
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
            if (not isinstance(required_fields, list) or len(required_fields) > 16
                                                or any(not isinstance(field, str) or not field.strip() for field in required_fields)):
                return [], "verify required_fields must be a list of at most 16 non-empty names."
        if action in STATE_CHANGING_ACTIONS:
            state_change_indexes.append(len(validated))
        validated.append(BrowserAction(action, dict(arguments)))
    if len(state_change_indexes) > 1:
        return [], "A browser batch may contain at most one state-changing action."
    if state_change_indexes and state_change_indexes[0] != len(validated) - 1:
        return [], "The state-changing browser action must be the final action in a batch."
    # Evidence operations are read-only. Normalize their order so a model
    # cannot lose a valid extraction merely by emitting verify before extract;
    # state-changing actions are never reordered.
    if not state_change_indexes:
        evidence_order = {"extract": 0, "verify": 1}
        if any(item.action == "extract" for item in validated) and any(item.action == "verify" for item in validated):
            validated = sorted(enumerate(validated), key=lambda pair: (
                evidence_order.get(pair[1].action, -1), pair[0],
            ))
            validated = [item for _index, item in validated]
    return validated, None
