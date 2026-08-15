"""Fail-closed validation for browser reference-bound evidence.

The bundled Playwright MCP exposes accessibility references, but an accessibility
snapshot alone does not prove that several fields belong to one DOM subtree.  This
module defines the small structured contract a trusted local adapter must satisfy
before DeskOrb treats an extraction as evidence.  It never evaluates page code or
parses arbitrary page text into card fields.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit


MAX_REF_CHARS = 80
MAX_FIELD_CHARS = 80
MAX_VALUE_CHARS = 500
MAX_OBSERVATION_ID_CHARS = 128
_FIELD_NAME = re.compile(r"^[^\s\[\]{}()<>/\\:;|]+$")
_OBSERVATION_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
_REF_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ROOT_REF = re.compile(
    r"^\s*-\s+[^\n]*?\[ref=(?P<ref>[^\]\s]+)\]"
    r"(?:\s+\[[^\]]+\])*\s*(?::.*)?$"
)
_NODE_VALUE = re.compile(
    r"^\s*-\s+(?P<label>/url|[^\s\[\]:]+)"
    r"(?:\s+\[[^\]]+\])*\s*:\s*(?P<value>.*)$"
)
_FIELD_VALUE = re.compile(r"^\s*(?P<label>[^\s:：]{1,80})\s*[:：]\s*(?P<value>.+?)\s*$")
_HEADING = re.compile(r"^\s*-\s+heading\s+(?P<value>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')")
_NAMED_NODE = re.compile(
    r"^\s*-\s+(?P<role>heading|link|img|strong)\b"
    r"(?:\s+\[[^\]]+\])*\s+"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
)
_PRICE_VALUE = re.compile(
    r"(?<![\w])(?:[$€£¥￥₹]\s*\d+(?:[.,]\d{1,2})?"
    r"|\d+(?:[.,]\d{1,2})?\s*(?:USD|EUR|GBP|CNY|RMB|元|人民币))(?![\w])",
    re.IGNORECASE,
)
_PAGE_URL = re.compile(r"^\s*-\s+Page URL:\s*(?P<url>https?://\S+)\s*$", re.IGNORECASE)
_SNAPSHOT_LIMIT = 128 * 1024
_FIELD_ALIASES = {
    "title": frozenset({"title", "name", "标题", "名称", "商品名称"}),
    "price": frozenset({"price", "price_cny", "价格", "售价"}),
    "source": frozenset({"source", "来源"}),
    "url": frozenset({"url", "link", "链接", "详情链接", "/url"}),
    "excerpt": frozenset({"excerpt", "summary", "摘要", "简介"}),
    "stars": frozenset({"star", "stars", "stargazers", "star数", "star 数", "星标"}),
    "language": frozenset({"language", "语言", "主要语言"}),
    "updated_at": frozenset({"updated", "updated_at", "last_updated", "last updated", "最近更新时间", "更新时间", "最后更新"}),
    "installation": frozenset({"installation", "install", "installation_command", "installation command", "安装", "安装命令"}),
    "text": frozenset({"text", "content", "raw text"}),
    "mcp": frozenset({"mcp"}),
    "memory": frozenset({"memory", "记忆"}),
    "multi_agent": frozenset({"multi-agent", "multi agent", "multi_agent", "多agent", "多 agent"}),
    "tool_calling": frozenset({"tool calling", "tool-calling", "tool_calling", "工具调用"}),
    "issue_title": frozenset({"issue", "issue title", "issue_title", "问题标题"}),
    "published_at": frozenset({"published", "published_at", "发布时间", "日期"}),
    "rating": frozenset({"rating", "评分"}),
    "review_count": frozenset({"reviews", "review_count", "评价", "评价数"}),
    "layout": frozenset({"layout", "配列", "键数"}),
    "connectivity": frozenset({"connectivity", "connection", "连接方式", "无线"}),
}
_INSTALLATION_COMMAND = re.compile(
    r"(?i)\b(?:pip|uv|poetry|conda|npm|pnpm|yarn|cargo|go)\s+(?:install|add|get)\b"
)
_TRUSTED_EXTRACTOR_TOOLS = frozenset({
    "browser_extract",
    "browser_extract_ref",
    "browser_ref_subtree_extract",
})
_TRUSTED_SOURCES = frozenset({
    "playwright_ref_subtree",
    "deskorb_ref_subtree",
})


@dataclass(frozen=True)
class RefExtraction:
    """A bounded, backend-attested extraction result."""

    ref: str
    fields: dict[str, str]
    matched_fields: int
    observed_chars: int
    trusted_ref: bool
    source_tool: str
    observation_id: str
    reason: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        """Return only structural metadata suitable for runtime observations."""
        return {
            "ref": self.ref,
            "fields": dict(self.fields),
            "matched_fields": self.matched_fields,
            "observed_chars": self.observed_chars,
            "trusted_ref": self.trusted_ref,
            "source_tool": self.source_tool,
            "observation_id": self.observation_id,
            **({"reason": self.reason} if self.reason else {}),
        }


def requested_fields(arguments: dict[str, Any] | None) -> list[str]:
    """Normalize the bounded semantic field list supplied to an extract action."""
    values = (arguments or {}).get("fields") or []
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values[:16]:
        if not isinstance(value, str):
            continue
        field = value.strip()
        if field and len(field) <= MAX_FIELD_CHARS and _FIELD_NAME.fullmatch(field):
            result.append(field)
    return result


def bounded_observation_id(value: Any) -> str:
    """Accept only an opaque adapter token, never arbitrary page text."""
    token = str(value or "").strip()
    if len(token) > MAX_OBSERVATION_ID_CHARS or not _OBSERVATION_ID.fullmatch(token):
        return ""
    return token


def _content_text(value: Any) -> str:
    """Flatten only MCP text content; never inspect arbitrary result keys."""
    if isinstance(value, str):
        raw = value
        text = raw[:_SNAPSHOT_LIMIT]
        if text.lstrip().startswith(("[{", "{")):
            try:
                decoded = json.loads(raw, strict=False)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, (list, dict)):
                return _content_text(decoded)
        return text
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"][:_SNAPSHOT_LIMIT]
        if isinstance(value.get("content"), (list, tuple)):
            return "\n".join(_content_text(item) for item in value["content"])
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(_content_text(item) for item in value)
    return ""


def _unquote_scalar(value: str) -> str:
    """Decode the small scalar subset emitted by Playwright MCP."""
    text = str(value or "").strip()
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS]
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            decoded = json.loads(text)
            return decoded if isinstance(decoded, str) else ""
        except (TypeError, ValueError):
            return text[1:-1][:MAX_VALUE_CHARS]
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")[:MAX_VALUE_CHARS]
    return text[:MAX_VALUE_CHARS]


def _safe_http_url(value: str, *, base_url: str = "") -> str:
    """Resolve a displayed URL without accepting script or non-web schemes."""
    candidate = _unquote_scalar(value).strip()
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
        return ""
    if not parsed.scheme:
        base = urlsplit(base_url)
        if base.scheme.lower() not in {"http", "https"} or not base.netloc:
            return candidate[:MAX_VALUE_CHARS]
        candidate = urljoin(base_url, candidate)
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate[:MAX_VALUE_CHARS]


def _canonical_field(label: str) -> str:
    normalized = " ".join(str(label or "").strip().casefold().replace("_", " ").replace("-", " ").split())
    for canonical, aliases in _FIELD_ALIASES.items():
        if normalized == canonical.replace("_", " ") or normalized in {
            " ".join(item.casefold().replace("_", " ").replace("-", " ").split())
            for item in aliases
        }:
            return canonical
    return normalized.replace(" ", "_")


def parse_ref_snapshot(content: Any, ref: str, fields: list[str] | tuple[str, ...], *,
                       page_url: str = "") -> dict[str, Any]:
    """Parse one Playwright target snapshot into bounded, ref-bound fields.

    This is intentionally a parser for the adapter's generated snapshot
    envelope, not a general YAML or HTML parser.  The first YAML node must be
    the requested ref.  Explicit field labels are preferred; standard
    accessibility names and currency values are also accepted for real DOM
    cards whose title and price are represented by nested link/paragraph nodes.
    """
    requested = [field for field in requested_fields({"fields": list(fields)})]
    empty = {field: "" for field in requested}
    requested_ref = str(ref or "").strip()
    if (not requested_ref or len(requested_ref) > MAX_REF_CHARS
            or not _REF_TOKEN.fullmatch(requested_ref)):
        return {"ref": requested_ref[:MAX_REF_CHARS], "fields": empty,
                "matched_fields": 0, "observed_chars": 0, "trusted_ref": False,
                "reason": "bounded_ref_required"}

    text = _content_text(content)
    lines = text.splitlines()
    marker_index = next((index for index, line in enumerate(lines)
                         if line.strip().casefold() == "### snapshot"), None)
    if marker_index is None:
        return {"ref": requested_ref, "fields": empty, "matched_fields": 0,
                "observed_chars": 0, "trusted_ref": False,
                "reason": "snapshot_marker_missing"}

    observed_page_url = str(page_url or "").strip()
    if not observed_page_url:
        for line in lines[:marker_index]:
            match = _PAGE_URL.match(line)
            if match:
                observed_page_url = match.group("url")
                break
    safe_base_url = _safe_http_url(observed_page_url)

    body = lines[marker_index + 1:]
    while body and not body[0].strip():
        body.pop(0)
    if body and body[0].strip().casefold() in {"```yaml", "```yml"}:
        body = body[1:]
        end = next((index for index, line in enumerate(body) if line.strip() == "```"), None)
        if end is None:
            return {"ref": requested_ref, "fields": empty, "matched_fields": 0,
                    "observed_chars": 0, "trusted_ref": False,
                    "reason": "snapshot_fence_missing"}
        body = body[:end]
    else:
        body = [line for line in body if not line.strip().startswith("### ")]

    first_node = next((line for line in body if line.strip()), "")
    root_match = _ROOT_REF.match(first_node)
    if not root_match or root_match.group("ref") != requested_ref:
        observed_ref = root_match.group("ref") if root_match else ""
        return {"ref": observed_ref[:MAX_REF_CHARS], "fields": empty,
                "matched_fields": 0, "observed_chars": 0, "trusted_ref": False,
                "reason": "snapshot_root_ref_mismatch"}

    values_by_canonical: dict[str, str] = {}
    heading_title = ""
    named_titles: list[tuple[int, str]] = []
    for line in body:
        heading = _HEADING.match(line)
        if heading and not heading_title:
            heading_title = _unquote_scalar(heading.group("value"))
        named = _NAMED_NODE.match(line)
        if named:
            role = named.group("role").casefold()
            title = _unquote_scalar(named.group("value"))
            if title:
                named_titles.append((
                    {"heading": 0, "strong": 0, "link": 1, "img": 2}.get(role, 3),
                    title,
                ))
        node = _NODE_VALUE.match(line)
        if not node:
            continue
        label = node.group("label")
        value = _unquote_scalar(node.group("value"))
        if not value:
            continue
        pairs = [_FIELD_VALUE.match(value)]
        if _canonical_field(label) in _FIELD_ALIASES:
            pairs.insert(0, _FIELD_VALUE.match(label + ": " + value))
        for pair in pairs:
            if not pair:
                continue
            canonical = _canonical_field(pair.group("label"))
            if canonical not in _FIELD_ALIASES:
                continue
            field_value = _unquote_scalar(pair.group("value"))
            if canonical == "url":
                field_value = _safe_http_url(field_value, base_url=safe_base_url)
            if field_value and canonical not in values_by_canonical:
                values_by_canonical[canonical] = field_value
        price_match = _PRICE_VALUE.search(value)
        if price_match and "price" not in values_by_canonical:
            values_by_canonical["price"] = price_match.group(0).strip()
        if _canonical_field(label) not in _FIELD_ALIASES and label.casefold() in {"heading", "strong"}:
            title_value = _unquote_scalar(value)
            if title_value and "title" not in values_by_canonical:
                values_by_canonical["title"] = title_value
        if (_canonical_field(label) not in _FIELD_ALIASES
                and label.casefold() in {"generic", "code", "pre", "text"}
                and _INSTALLATION_COMMAND.search(value)):
            command = _unquote_scalar(value)
            values_by_canonical.setdefault("installation", command)
            values_by_canonical.setdefault("text", command)

    # Official Playwright snapshots commonly expose a result URL and a
    # visible heading/domain instead of explicit ``source:`` labels.  The URL
    # is already constrained to http(s) above, so its hostname is a bounded,
    # backend-derived source value; never derive fields from arbitrary prose.
    if "source" not in values_by_canonical and values_by_canonical.get("url"):
        hostname = urlsplit(values_by_canonical["url"]).hostname or ""
        if hostname.lower().startswith("www."):
            hostname = hostname[4:]
        if hostname:
            values_by_canonical["source"] = hostname[:MAX_VALUE_CHARS]

    if heading_title and "title" not in values_by_canonical:
        values_by_canonical["title"] = heading_title[:MAX_VALUE_CHARS]
    if named_titles and "title" not in values_by_canonical:
        values_by_canonical["title"] = sorted(named_titles, key=lambda item: item[0])[0][1][:MAX_VALUE_CHARS]
    projected: dict[str, str] = {}
    for field in requested:
        canonical = _canonical_field(field)
        value = values_by_canonical.get(canonical, "")
        projected[field] = value[:MAX_VALUE_CHARS]
    observed_chars = min(sum(len(line) + 1 for line in body), 100_000)
    return {"ref": requested_ref, "fields": projected,
            "matched_fields": sum(bool(value) for value in projected.values()),
            "observed_chars": observed_chars, "trusted_ref": True}


class TrustedBrowserRefExtractor:
    """Track fresh adapter observations and validate ref-bound extraction payloads."""

    def __init__(self) -> None:
        self._observation_id = ""

    @property
    def observation_id(self) -> str:
        """Current opaque observation token, or empty when no trusted snapshot exists."""
        return self._observation_id

    def invalidate(self) -> None:
        """Invalidate all refs after navigation, handoff, reconnect, or task reset."""
        self._observation_id = ""

    def observe(self, result: dict[str, Any] | None) -> str:
        """Register only an explicit adapter observation token.

        Generic accessibility snapshots do not carry this token and therefore
        deliberately leave the extractor unavailable.  The token must be a
        structured top-level field (or structured ``observation`` field), never
        a value parsed from page text.
        """
        value = result.get("observation_id") if isinstance(result, dict) else None
        if value is None and isinstance(result, dict) and isinstance(result.get("observation"), dict):
            value = result["observation"].get("observation_id")
        self._observation_id = bounded_observation_id(value)
        return self._observation_id

    def extract(self, result: dict[str, Any] | None, arguments: dict[str, Any] | None,
                *, source_tool: str) -> RefExtraction:
        """Validate the explicit ref-subtree adapter contract.

        A result is trusted only when it came from an allowlisted extractor tool,
        explicitly identifies the approved subtree source, attests the requested
        ref, and matches the most recent fresh observation token.  A snapshot or
        model-supplied ``trusted_ref`` flag cannot satisfy these requirements.
        """
        args = arguments if isinstance(arguments, dict) else {}
        requested_ref = str(args.get("ref") or "").strip()
        fields = requested_fields(args)
        empty = {field: "" for field in fields}
        native = result.get("extraction") if isinstance(result, dict) else None
        source_tool = str(source_tool or "").strip()
        if source_tool not in _TRUSTED_EXTRACTOR_TOOLS:
            return RefExtraction(requested_ref[:MAX_REF_CHARS], empty, 0, 0, False,
                                 source_tool, "", reason="trusted_extractor_tool_required")
        if not requested_ref or len(requested_ref) > MAX_REF_CHARS or any(character.isspace() for character in requested_ref):
            return RefExtraction(requested_ref[:MAX_REF_CHARS], empty, 0, 0, False,
                                 source_tool, "", reason="bounded_ref_required")
        if not isinstance(native, dict):
            return RefExtraction(requested_ref, empty, 0, 0, False,
                                 source_tool, "", reason="backend_extraction_missing")
        source = str(native.get("source") or "").strip()
        if source not in _TRUSTED_SOURCES:
            return RefExtraction(requested_ref, empty, 0, 0, False,
                                 source_tool, "", reason="trusted_subtree_source_required")
        observed_ref = str(native.get("ref") or "").strip()
        if observed_ref != requested_ref:
            return RefExtraction(observed_ref[:MAX_REF_CHARS], empty, 0, 0, False,
                                 source_tool, "", reason="ref_provenance_mismatch")
        observation_id = bounded_observation_id(native.get("observation_id"))
        if not self._observation_id or observation_id != self._observation_id:
            return RefExtraction(observed_ref, empty, 0, 0, False,
                                 source_tool, observation_id, reason="stale_or_missing_observation")
        if native.get("trusted_ref") is not True:
            return RefExtraction(observed_ref, empty, 0, 0, False,
                                 source_tool, observation_id, reason="backend_attestation_missing")
        raw_fields = native.get("fields")
        if not isinstance(raw_fields, dict):
            return RefExtraction(observed_ref, empty, 0, 0, False,
                                 source_tool, observation_id, reason="backend_fields_missing")
        values: dict[str, str] = {}
        for field in fields:
            value = raw_fields.get(field)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                values[field] = str(value)[:MAX_VALUE_CHARS]
            else:
                values[field] = ""
        try:
            observed_chars = max(0, min(int(native.get("observed_chars") or 0), 100_000))
        except (TypeError, ValueError):
            observed_chars = 0
        return RefExtraction(observed_ref, values,
                             sum(bool(value) for value in values.values()),
                             observed_chars, True, source_tool, observation_id)


__all__ = [
    "MAX_FIELD_CHARS", "MAX_REF_CHARS", "MAX_VALUE_CHARS",
    "RefExtraction", "TrustedBrowserRefExtractor", "bounded_observation_id",
    "parse_ref_snapshot", "requested_fields",
]
