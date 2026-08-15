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
    "switch_tab", "list_tabs", "open_ref_new_tab", "close_tab", "press_key",
    "find_text", "scroll", "go_back", "extract", "extract_list", "verify",
})
STATE_CHANGING_ACTIONS = frozenset({
    "navigate", "click_ref", "fill_ref", "select_ref", "wait", "switch_tab",
    "open_ref_new_tab", "close_tab", "press_key", "scroll", "go_back",
})
SAFE_PRESS_KEYS = frozenset({
    "Enter", "Escape", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "PageUp", "PageDown",
})
MAX_ACTIONS = 8
_MAX_REF_CHARS = 80
_MAX_FIELD_CHARS = 80
_SEMANTIC_FIELD_NAME = re.compile(r"^[^\s\[\]{}()<>/\\\\:;|]+$")

# Some Responses-compatible gateways flatten a function object's nested
# ``arguments`` into the action item itself.  Keep this compatibility list
# deliberately finite: accepting arbitrary top-level keys would turn the
# protocol adapter into an unbounded escape hatch.
_FLATTENED_ARGUMENT_KEYS: dict[str, frozenset[str]] = {
    "navigate": frozenset({"url"}),
    "snapshot": frozenset(),
    "click_ref": frozenset({"ref", "target", "refs", "observation_id"}),
    "fill_ref": frozenset({"ref", "target", "refs", "observation_id", "value", "text"}),
    "select_ref": frozenset({
        "ref", "target", "refs", "observation_id", "values", "value", "options",
    }),
    "wait": frozenset({"observation_id", "seconds", "time", "ms", "duration_ms", "milliseconds"}),
    "switch_tab": frozenset({"observation_id", "index", "tab_ref", "tab_snapshot_id"}),
    "list_tabs": frozenset({"observation_id"}),
    "open_ref_new_tab": frozenset({"ref", "target", "refs", "observation_id"}),
    "close_tab": frozenset({"observation_id", "index", "tab_ref", "tab_snapshot_id"}),
    "press_key": frozenset({"ref", "target", "refs", "observation_id", "key"}),
    "find_text": frozenset({"observation_id", "query", "text"}),
    "scroll": frozenset({"observation_id", "direction"}),
    "go_back": frozenset({"observation_id"}),
    "extract": frozenset({
        "ref", "target", "refs", "observation_id", "fields", "selectors", "field_names",
    }),
    "extract_list": frozenset({
        "observation_id", "scope_ref", "fields", "selectors", "field_names", "limit", "unique_by",
    }),
    "verify": frozenset({
        "contains", "expected", "required_fields", "fields", "postcondition", "price_min", "price_max",
        "min_items", "unique_by", "max_tabs", "final_tabs", "required_origins", "forbidden_actions",
        "required_evidence", "source_priority",
        "required_confirmations",
    }),
}


def _flatten_action_arguments(action: str, item: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return nested arguments, or a bounded provider-flattened equivalent."""
    arguments = item.get("arguments")
    if isinstance(arguments, dict):
        return dict(arguments), None
    if arguments is not None:
        return None, "Browser action arguments must be an object."
    allowed = _FLATTENED_ARGUMENT_KEYS.get(action)
    if allowed is None:
        return None, "Browser batch contains an unsupported action."
    flattened = {key: value for key, value in item.items() if key != "action"}
    unknown = sorted(set(flattened) - allowed)
    if unknown:
        return None, "Browser action contains unsupported top-level arguments."
    return flattened, None


def _semantic_field_alias(value: Any) -> str:
    """Map common model shorthand to one bounded semantic evidence field."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    # Exact semantic names must win over substring heuristics.  Without this
    # precedence, ``issue_title`` was incorrectly reduced to ``title``.
    exact_aliases = {
        "issue": "issue_title", "issue title": "issue_title", "issue_title": "issue_title",
        "installation_command": "installation", "installation": "installation", "install": "installation",
        "star": "stars", "stars": "stars", "stargazers": "stars", "星标": "stars",
        "language": "language", "语言": "language", "主要语言": "language",
        "updated": "updated_at", "updated_at": "updated_at", "更新时间": "updated_at",
        "mcp": "mcp", "memory": "memory", "记忆": "memory",
        "multi-agent": "multi_agent", "multi agent": "multi_agent", "multi_agent": "multi_agent",
        "tool calling": "tool_calling", "tool-calling": "tool_calling", "tool_calling": "tool_calling",
        "发布时间": "published_at", "rating": "rating", "评分": "rating",
        "review_count": "review_count", "layout": "layout", "配列": "layout",
        "connectivity": "connectivity", "连接方式": "connectivity",
    }
    if text in exact_aliases:
        return exact_aliases[text]
    if any(marker in text for marker in ("title", "heading", "name", "标题", "名称", "h1", "h2")):
        return "title"
    if any(marker in text for marker in ("price", "价格", "售价")):
        return "price"
    if any(marker in text for marker in ("source", "来源")) or text in {"p", "paragraph"}:
        return "source"
    if any(marker in text for marker in ("url", "link", "链接")) or text in {"a", "anchor", "href"}:
        return "url"
    aliases = {
        "star": "stars", "stars": "stars", "stargazers": "stars", "星标": "stars",
        "language": "language", "语言": "language", "主要语言": "language",
        "updated": "updated_at", "updated_at": "updated_at", "更新时间": "updated_at",
        "installation": "installation", "install": "installation", "安装": "installation",
        "mcp": "mcp", "memory": "memory", "记忆": "memory",
        "multi-agent": "multi_agent", "multi agent": "multi_agent", "multi_agent": "multi_agent",
        "tool calling": "tool_calling", "tool-calling": "tool_calling", "tool_calling": "tool_calling",
        "issue": "issue_title", "issue_title": "issue_title", "发布时间": "published_at",
        "rating": "rating", "评分": "rating", "review_count": "review_count",
        "layout": "layout", "配列": "layout", "connectivity": "connectivity", "连接方式": "connectivity",
    }
    if text in aliases:
        return aliases[text]
    return text if _SEMANTIC_FIELD_NAME.fullmatch(text) else ""


def _normalize_field_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized = [_semantic_field_alias(item) for item in values]
    return list(dict.fromkeys(field for field in normalized if field))


def _normalize_arguments(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize harmless model vocabulary aliases into the strict contract."""
    normalized = dict(arguments)
    if action in {"click_ref", "fill_ref", "select_ref", "extract", "open_ref_new_tab"}:
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
    if action == "press_key" and isinstance(normalized.get("key"), str):
        key = normalized["key"].strip().lower()
        normalized["key"] = next((allowed for allowed in SAFE_PRESS_KEYS
                                   if allowed.lower() == key), normalized["key"].strip())
    if action == "extract" and "fields" not in normalized:
        raw_fields = normalized.get("selectors")
        if not isinstance(raw_fields, list):
            raw_fields = normalized.get("field_names")
        if isinstance(raw_fields, list):
            normalized["fields"] = _normalize_field_names(raw_fields)
    if action == "extract" and isinstance(normalized.get("fields"), list):
        normalized["fields"] = _normalize_field_names(normalized["fields"])
    if action == "extract_list" and "fields" not in normalized:
        raw_fields = normalized.get("selectors")
        if not isinstance(raw_fields, list):
            raw_fields = normalized.get("field_names")
        if isinstance(raw_fields, list):
            normalized["fields"] = _normalize_field_names(raw_fields)
    if action == "extract_list" and isinstance(normalized.get("fields"), list):
        normalized["fields"] = _normalize_field_names(normalized["fields"])
    if action == "find_text" and "query" not in normalized and isinstance(normalized.get("text"), str):
        normalized["query"] = normalized["text"]
    if action == "scroll":
        direction = str(normalized.get("direction") or "down").strip().lower()
        normalized["direction"] = "up" if direction in {"up", "pageup", "向上", "上"} else "down"
    if action == "open_ref_new_tab" and "ref" not in normalized:
        target = normalized.get("target")
        if isinstance(target, str):
            normalized["ref"] = target
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
        if "postcondition" in normalized:
            normalized["postcondition"] = str(normalized["postcondition"]).strip().lower()
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
        if action not in ALLOWED_ACTIONS:
            return [], "Browser batch contains an unsupported action."
        arguments, flatten_error = _flatten_action_arguments(action, item)
        if flatten_error or arguments is None:
            return [], flatten_error or "Browser action arguments must be an object."
        arguments = _normalize_arguments(action, arguments)
        if action == "navigate":
            url = str(arguments.get("url") or "")
            if not url.startswith(("http://", "https://")):
                return [], "Navigate requires an http(s) URL."
        if action in {"switch_tab", "close_tab"}:
            has_opaque_tab = bool(str(arguments.get("tab_ref") or "").strip()
                                  and str(arguments.get("tab_snapshot_id") or "").strip())
            index = arguments.get("index")
            if not has_opaque_tab and (isinstance(index, bool) or not isinstance(index, int) or index < 0):
                return [], f"{action} requires a tab_ref/tab_snapshot_id pair or a non-negative legacy tab index."
        if action in {"click_ref", "fill_ref", "select_ref", "press_key", "extract", "open_ref_new_tab"}:
            ref = str(arguments.get("ref") or "").strip()
            if action in {"click_ref", "fill_ref", "select_ref", "press_key", "extract", "open_ref_new_tab"} and not ref:
                return [], f"{action} requires a structured page reference."
            if ref and (len(ref) > _MAX_REF_CHARS or any(character.isspace() for character in ref)):
                return [], f"{action} requires a bounded structured page reference."
            if not str(arguments.get("observation_id") or "").strip():
                return [], f"{action} requires the current observation_id."
        if action in {"list_tabs", "find_text", "scroll", "go_back", "extract_list"}:
            if not str(arguments.get("observation_id") or "").strip():
                return [], f"{action} requires the current observation_id."
        if action == "find_text":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip() or len(query.strip()) > 160:
                return [], "find_text requires a bounded non-empty plain-text query."
            if any(token in query for token in ("(?", "\\d", "\\w", "|", "*", ".*")):
                return [], "find_text accepts plain text only; regular expressions are not allowed."
        if action == "scroll":
            direction = str(arguments.get("direction") or "").strip().lower()
            if direction not in {"up", "down"}:
                return [], "scroll direction must be up or down."
        if action == "press_key":
            key = arguments.get("key")
            if not isinstance(key, str) or key not in SAFE_PRESS_KEYS:
                return [], "press_key key must be one of the allowed navigation keys."
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
        if action == "extract_list":
            fields = arguments.get("fields", [])
            if (not isinstance(fields, list) or not fields or len(fields) > 16
                    or any(not isinstance(field, str) or not field.strip()
                           or len(field.strip()) > _MAX_FIELD_CHARS
                           or not _SEMANTIC_FIELD_NAME.fullmatch(field.strip())
                           for field in fields)):
                return [], "extract_list fields must be a list of at most 16 bounded semantic names."
            try:
                limit = int(arguments.get("limit", 20))
            except (TypeError, ValueError):
                return [], "extract_list limit must be an integer."
            if limit < 1 or limit > 20:
                return [], "extract_list limit must be between 1 and 20."
            unique_by = arguments.get("unique_by")
            if unique_by is not None and (
                    not isinstance(unique_by, list) or len(unique_by) > 8
                    or any(not isinstance(item, str) or not item.strip() for item in unique_by)):
                return [], "extract_list unique_by must be a bounded list of field names."
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
            for key, upper in (("min_items", 20), ("max_tabs", 12), ("required_confirmations", 8)):
                if key in arguments:
                    try:
                        value_number = int(arguments[key])
                    except (TypeError, ValueError):
                        return [], f"verify {key} must be an integer."
                    if value_number < 0 or value_number > upper:
                        return [], f"verify {key} must be between 0 and {upper}."
            for key in ("unique_by", "required_origins", "forbidden_actions", "required_evidence",
                        "source_priority"):
                value = arguments.get(key)
                if value is not None and (not isinstance(value, list) or len(value) > 16
                                          or any(not isinstance(item, str) or not item.strip() for item in value)):
                    return [], f"verify {key} must be a bounded list of strings."
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
