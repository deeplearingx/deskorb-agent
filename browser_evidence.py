"""Bounded browser evidence and accessibility-snapshot helpers.

The helpers in this module only consume the structured text returned by the
trusted Playwright adapter.  They never execute page JavaScript and never treat
page text as a new instruction or permission.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import re
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


_PAGE_URL = re.compile(r"^\s*-\s+Page URL:\s*(https?://\S+)\s*$", re.IGNORECASE)
_TAB_LINE = re.compile(
    r"^\s*-\s*(?P<index>\d+)\s*:\s*(?P<current>\(current\)\s*)?\[(?P<title>[^\]]*)\]\((?P<url>[^)]+)\)\s*$",
    re.IGNORECASE,
)
_REF = re.compile(r"\[ref=(?P<ref>[^\]\s]+)\]")
_URL_VALUE = re.compile(r"^\s*-\s*/url(?:\s+\[[^\]]+\])?\s*:\s*(?P<url>\S+)\s*$", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\]\((?P<url>https?://[^)\s]+|/[^)\s]+)\)", re.IGNORECASE)
_FIELD_VALUE = re.compile(r"(?P<label>[\w\- ]{1,80})\s*[:：]\s*(?P<value>[^,，;；|\n]{1,500})")
_QUOTED = re.compile(r"(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)')")

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "name", "heading", "标题", "名称", "项目"),
    "url": ("url", "link", "href", "链接", "地址"),
    "source": ("source", "来源", "网站"),
    "excerpt": ("excerpt", "summary", "摘要", "简介"),
    "stars": ("star", "stars", "stargazers", "star数", "star 数", "星标"),
    "language": ("language", "语言", "主要语言"),
    "updated_at": ("updated", "updated_at", "last updated", "最近更新时间", "更新时间", "最后更新"),
    "installation": ("installation", "install", "installation_command", "installation command", "安装", "安装命令"),
    "text": ("text", "content", "raw text"),
    "mcp": ("mcp",),
    "memory": ("memory", "记忆"),
    "multi_agent": ("multi-agent", "multi agent", "multi_agent", "多agent", "多 agent"),
    "tool_calling": ("tool calling", "tool-calling", "tool_calling", "工具调用"),
    "issue_title": ("issue", "issue title", "issue_title", "问题标题"),
    "published_at": ("published", "published_at", "发布时间", "日期"),
    "price": ("price", "价格", "售价"),
    "rating": ("rating", "评分"),
    "review_count": ("reviews", "review_count", "评价", "评价数"),
    "layout": ("layout", "配列", "键数"),
    "connectivity": ("connectivity", "connection", "连接方式", "无线"),
}

_BLOCKED_BROWSER_HOSTS = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "0.0.0.0", "::1",
}
_BLOCKED_BROWSER_SUFFIXES = (
    ".localhost", ".local", ".internal", ".lan", ".home.arpa",
    ".test", ".invalid",
)
# This is intentionally a small, conservative list.  It is only used to
# recognise a normal public-site redirect (for example
# www.wikipedia.org -> en.wikipedia.org); it never authorises a model to
# invent a new navigation target.  Keeping the common multi-label suffixes
# here avoids treating evil.co.uk as part of example.co.uk.
_COMMON_TWO_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "com.cn", "net.cn", "org.cn", "gov.cn", "com.hk", "com.sg", "co.jp",
    "co.kr", "com.br", "com.mx", "co.nz", "co.za",
})
_CAPABILITY_FIELDS = frozenset({"mcp", "memory", "multi_agent", "tool_calling"})
_CAPABILITY_NEGATIVE = (
    "not supported", "unsupported", "does not support", "without support",
    "不支持", "不具备", "没有", "无", "false", "no", "否", "✗", "❌",
)
_CAPABILITY_UNKNOWN = (
    "no mention", "not mentioned", "not specified", "unclear", "unknown",
    "没有明确说明", "未说明", "未找到", "未提供", "未知",
)
_CAPABILITY_POSITIVE = (
    "supported", "support", "supports", "支持", "具备", "有", "true", "yes",
    "是", "available", "enabled", "✅", "✓",
)
_NON_RECORD_TITLE_MARKERS = (
    "you must be signed in",
    "sign in to star",
    "sign in to follow",
    "sponsor @",
)
_NON_RECORD_URL_PATHS = (
    "/login", "/sponsors/", "/settings/", "/notifications",
)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        raw = value
        text = raw[:256_000]
        # Some Playwright MCP responses encode the TextContent list one extra
        # time as JSON. Decode only structured list/object strings; ordinary
        # page text remains untouched and untrusted.
        stripped = text.lstrip()
        if stripped.startswith(("[{", "{")):
            try:
                decoded = json.loads(raw, strict=False)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, (list, dict)):
                return _content_text(decoded)
        return text
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"][:256_000]
        if isinstance(value.get("content"), (list, tuple)):
            return "\n".join(_content_text(item) for item in value["content"])
        if isinstance(value.get("data"), str):
            return value["data"][:256_000]
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(_content_text(item) for item in value)
    return ""


def decode_browser_content(value: Any) -> Any:
    """Decode one JSON-encoded MCP content envelope without executing it."""
    if not isinstance(value, str):
        return value
    text = value
    if not text.lstrip().startswith(("[{", "{")):
        return value
    try:
        decoded = json.loads(text, strict=False)
    except (TypeError, ValueError):
        # Older Playwright MCP builds cap a single accessibility text block
        # by cutting the JSON envelope itself. Recover the bounded text prefix
        # without treating it as executable JSON; this keeps the Page URL and
        # the first observed refs available for semantic recovery.
        marker = '"text":"'
        start = text.find(marker)
        if not text.lstrip().startswith("[{") or start < 0:
            return value
        body = text[start + len(marker):]
        if body.endswith("'}]") or body.endswith('"}]'):
            body = body[:-3]
        for _ in range(8):
            try:
                recovered = json.loads('"' + body + '"', strict=False)
                if isinstance(recovered, str):
                    return [{"type": "text", "text": recovered}]
            except (TypeError, ValueError):
                body = body[:-1]
        return value
    return decoded if isinstance(decoded, (list, dict)) else value


def _unquote(value: str) -> str:
    text = str(value or "").strip()
    match = _QUOTED.search(text)
    if match:
        return (match.group("double") or match.group("single") or "").strip()[:500]
    return text.strip("\"'")[:500]


def safe_http_url(value: Any, *, strip_query: bool = False) -> str:
    text = _unquote(str(value or "")).strip().rstrip(".,;)]}")
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    query = "" if strip_query else parsed.query
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", query, ""))[:1000]


def is_safe_public_browser_url(value: Any) -> bool:
    """Return whether a browser navigation target is safe for real acceptance.

    This is intentionally a local, non-DNS check.  The Playwright adapter uses
    it at the actual browser boundary, while fixture backends can still use
    loopback pages in unit tests.  DNS rebinding protections belong to the
    browser/network sandbox; IP literals and reserved development suffixes are
    rejected here before they reach the MCP process.
    """
    url = safe_http_url(value)
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    host = str(parsed.hostname or "").casefold().rstrip(".")
    if not host or host in _BLOCKED_BROWSER_HOSTS or host.endswith(_BLOCKED_BROWSER_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return bool(address.is_global)


def normalize_capability_status(value: Any) -> str:
    """Normalize a capability claim without inventing a negative result."""
    text = " ".join(str(value or "").casefold().split())
    if not text:
        return "unknown"
    if any(marker in text for marker in _CAPABILITY_UNKNOWN):
        return "unknown"
    if any(marker in text for marker in _CAPABILITY_NEGATIVE):
        return "no"
    if any(marker in text for marker in _CAPABILITY_POSITIVE):
        return "yes"
    return "unknown"


def normalize_evidence_fields(fields: dict[str, Any] | None) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        normalized[_canonical_field(key)] = value
    if "stars" in normalized:
        parsed_stars = parse_github_star_count(normalized["stars"])
        if parsed_stars is not None:
            normalized["stars"] = str(parsed_stars)
    if "updated_at" in normalized:
        parsed_date = parse_github_relative_date(normalized["updated_at"])
        if parsed_date:
            normalized["updated_at"] = parsed_date
    for key in _CAPABILITY_FIELDS:
        if key in normalized:
            normalized[key] = normalize_capability_status(normalized[key])
    return normalized


def parse_github_star_count(value: Any) -> int | None:
    """Parse GitHub's bounded abbreviated star counts (``12k``, ``1.2M``)."""
    text = str(value or "").strip().casefold().replace(",", "")
    match = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([kmb])?(?![\w.])", text)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except (TypeError, ValueError):
        return None
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(match.group(2) or "", 1)
    return max(0, int(round(number * multiplier)))


def parse_github_relative_date(value: Any, *, now: datetime | None = None) -> str:
    """Normalize common GitHub absolute/relative update labels to ISO date."""
    text = " ".join(str(value or "").strip().casefold().split())
    if not text or text in {"unknown", "n/a", "na", "-"}:
        return ""
    reference = now or datetime.now(timezone.utc)
    relative = re.search(
        r"(?:about\s+)?(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)\s+ago",
        text,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit.startswith("minute"):
            result = reference - timedelta(minutes=amount)
        elif unit.startswith("hour"):
            result = reference - timedelta(hours=amount)
        elif unit.startswith("day"):
            result = reference - timedelta(days=amount)
        elif unit.startswith("week"):
            result = reference - timedelta(weeks=amount)
        elif unit.startswith("month"):
            result = reference - timedelta(days=30 * amount)
        else:
            result = reference - timedelta(days=365 * amount)
        return result.date().isoformat()
    cleaned = re.sub(r"^on\s+", "", text, flags=re.IGNORECASE)
    for pattern in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, pattern).date().isoformat()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except (TypeError, ValueError):
        return ""


def page_url_from_content(value: Any) -> str:
    for line in _content_text(value).splitlines()[:80]:
        match = _PAGE_URL.match(line)
        if match:
            return safe_http_url(match.group(1))
    return ""


def origin_from_url(value: Any) -> str:
    url = safe_http_url(value)
    if not url:
        return ""
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}".casefold()


def public_site_key(value: Any) -> str:
    """Return a bounded registrable-site key for a public HTTP(S) URL.

    This helper is deliberately not a general URL allowlist.  It exists for
    post-navigation redirect validation, where a site can legitimately move
    between sibling language/CDN subdomains.  Callers must still validate the
    original URL and must not use this result to admit private or non-HTTP
    addresses.
    """
    url = safe_http_url(value)
    if not url:
        return ""
    try:
        host = str(urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""
    if not host or host in _BLOCKED_BROWSER_HOSTS or host.endswith(_BLOCKED_BROWSER_SUFFIXES):
        return ""
    try:
        if ipaddress.ip_address(host):
            return host
    except ValueError:
        pass
    labels = [item for item in host.split(".") if item]
    if len(labels) < 2:
        return host
    suffix = ".".join(labels[-2:])
    label_count = 3 if suffix in _COMMON_TWO_LABEL_SUFFIXES and len(labels) >= 3 else 2
    return ".".join(labels[-label_count:])


def compatible_public_origin(left: Any, right: Any) -> bool:
    """Return whether two already-observed public origins share a site key.

    This is a redirect compatibility check, not permission to navigate to an
    arbitrary sibling host.  The semantic runtime still requires an observed
    link or an explicit user target before the state action is sent.
    """
    left_key = public_site_key(left)
    right_key = public_site_key(right)
    return bool(left_key and right_key and left_key == right_key)


def parse_tab_list(value: Any) -> list[dict[str, Any]]:
    """Parse Playwright MCP's bounded markdown tab listing."""
    tabs: list[dict[str, Any]] = []
    for line in _content_text(value).splitlines()[:256]:
        match = _TAB_LINE.match(line)
        if not match:
            continue
        try:
            index = int(match.group("index"))
        except (TypeError, ValueError):
            continue
        url = safe_http_url(match.group("url"))
        if not url:
            continue
        tabs.append({
            "index": index,
            "current": bool(match.group("current")),
            "title": _unquote(match.group("title"))[:240],
            "url": url,
        })
    return tabs[:32]


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def subtree_lines(value: Any, ref: str) -> list[str]:
    """Return one accessibility subtree bounded by indentation and ref."""
    lines = _content_text(value).splitlines()
    target_index = next((i for i, line in enumerate(lines) if f"[ref={ref}]" in line), None)
    if target_index is None:
        return []
    root_indent = _line_indent(lines[target_index])
    selected = [lines[target_index]]
    for line in lines[target_index + 1:]:
        stripped = line.strip()
        if stripped.startswith("### "):
            break
        if stripped.startswith("-") and _line_indent(line) <= root_indent:
            break
        selected.append(line)
    return selected


def observed_link_url(value: Any, ref: str, *, base_url: str = "") -> str:
    lines = subtree_lines(value, ref)
    for line in lines:
        match = _URL_VALUE.match(line)
        if match:
            url = safe_http_url(match.group("url"))
            if url:
                return url
    # A few Playwright versions render the href inline in link metadata.
    for line in lines:
        if "href=" in line.casefold():
            candidate = line.split("href=", 1)[-1].strip().split()[0].strip("\"'[]")
            url = safe_http_url(candidate)
            if url:
                return url
    if base_url:
        for line in lines:
            candidate = line.split(":", 1)[-1].strip() if ":" in line else ""
            if candidate.startswith(("/", "./")):
                from urllib.parse import urljoin
                url = safe_http_url(urljoin(base_url, candidate))
                if url:
                    return url
    return ""


def observed_link_urls(value: Any, *, base_url: str = "") -> tuple[str, ...]:
    """Return bounded HTTP(S) links visibly attested by a page snapshot."""
    from urllib.parse import urljoin

    found: list[str] = []
    for line in _content_text(value).splitlines()[:512]:
        candidates: list[str] = []
        match = _URL_VALUE.match(line)
        if match:
            candidates.append(match.group("url"))
        candidates.extend(item.group("url") for item in _MARKDOWN_LINK.finditer(line))
        if "href=" in line.casefold():
            candidates.append(line.split("href=", 1)[-1].strip().split()[0].strip("\"'[]"))
        for candidate in candidates:
            target = safe_http_url(candidate)
            if not target and base_url and str(candidate).startswith(("/", "./")):
                target = safe_http_url(urljoin(base_url, str(candidate)))
            if target and target not in found:
                found.append(target)
            if len(found) >= 128:
                return tuple(found)
    return tuple(found)


def _canonical_field(value: Any) -> str:
    text = " ".join(str(value or "").casefold().replace("_", " ").replace("-", " ").split())
    for canonical, aliases in _FIELD_ALIASES.items():
        if text == canonical.replace("_", " ") or text in {item.casefold() for item in aliases}:
            return canonical
    return text.replace(" ", "_")[:80]


def _field_values(lines: Iterable[str], *, root_ref: str, page_url: str) -> dict[str, str]:
    values: dict[str, str] = {}
    title_candidates: list[tuple[str, str]] = []
    for line in lines:
        stripped = line.strip()
        quoted = _QUOTED.search(stripped)
        if quoted and stripped.startswith("-") and "[ref=" in stripped:
            role = stripped[1:].strip().split()[0].casefold()
            if role in {"heading", "link", "article", "listitem", "row"}:
                title_candidates.append((role, _unquote(quoted.group(0))))
        if ":" in stripped:
            label, raw = stripped[1:].split(":", 1) if stripped.startswith("-") else stripped.split(":", 1)
            canonical = _canonical_field(label.strip().split("[", 1)[0].strip())
            candidate = _unquote(raw)
            if canonical in _FIELD_ALIASES and candidate and canonical not in values:
                values[canonical] = candidate
        for match in _FIELD_VALUE.finditer(stripped):
            canonical = _canonical_field(match.group("label"))
            if canonical in _FIELD_ALIASES and canonical not in values:
                values[canonical] = _unquote(match.group("value"))
    if "title" not in values:
        # A GitHub card can expose a sign-in/star control before the actual
        # repository link. Prefer a heading, then the first non-utility link,
        # instead of treating that control as the record title.
        ranked_titles = sorted(
            title_candidates,
            key=lambda item: {"heading": 0, "link": 1, "article": 2,
                              "listitem": 3, "row": 4}.get(item[0], 5),
        )
        for _role, candidate in ranked_titles:
            normalized = " ".join(candidate.casefold().split())
            if candidate and not any(marker in normalized for marker in _NON_RECORD_TITLE_MARKERS):
                values["title"] = candidate
                break
    links = observed_link_urls("\n".join(lines), base_url=page_url)
    if "url" not in values or not safe_http_url(values.get("url")):
        values.pop("url", None)
        for link in links:
            try:
                path = urlsplit(link).path.casefold()
            except ValueError:
                path = ""
            if not any(path == marker or path.startswith(marker) for marker in _NON_RECORD_URL_PATHS):
                values["url"] = link
                break
        if "url" not in values and links:
            values["url"] = links[0]
    if page_url and "source" not in values:
        host = urlsplit(page_url).hostname or ""
        values["source"] = host.removeprefix("www.")[:500]
    return values


def _is_non_record_item(fields: dict[str, str]) -> bool:
    """Reject repeated utility/auth controls masquerading as result records."""
    title = " ".join(str(fields.get("title") or "").casefold().split())
    url = safe_http_url(fields.get("url"))
    if any(marker in title for marker in _NON_RECORD_TITLE_MARKERS):
        return True
    if url:
        try:
            path = urlsplit(url).path.casefold()
        except ValueError:
            path = ""
        if any(path == marker or path.startswith(marker) for marker in _NON_RECORD_URL_PATHS):
            return True
    return False


def extract_list_from_snapshot(content: Any, fields: Iterable[str], *, limit: int = 20,
                               scope_ref: str = "", unique_by: Iterable[str] | None = None,
                               page_url: str = "", observation_id: str = "") -> dict[str, Any]:
    """Extract bounded repeated accessibility subtrees with provenance."""
    requested = list(dict.fromkeys(_canonical_field(item) for item in fields if str(item).strip()))[:16]
    limit = max(1, min(20, int(limit or 20)))
    text = _content_text(content)
    lines = text.splitlines()
    roots: list[tuple[int, int, str, str]] = []
    allowed_roles = {"article", "listitem", "row", "group", "region", "card", "generic"}
    for index, line in enumerate(lines):
        match = re.match(r"^(?P<indent>\s*)-\s+(?P<role>[A-Za-z0-9_-]+).*\[ref=(?P<ref>[^\]\s]+)\]", line)
        if not match or match.group("role").casefold() not in allowed_roles:
            continue
        roots.append((index, _line_indent(line), match.group("ref"), match.group("role").casefold()))
    scope_text = ""
    if scope_ref:
        scope = subtree_lines(content, scope_ref)
        scope_text = "\n".join(scope)
        roots = [item for item in roots if f"[ref={item[2]}]" in scope_text]

    # GitHub Issues pages contain several repeated ``listitem`` groups in the
    # sidebar (repository, Issues, Pull requests, etc.) and another repeated
    # group for the actual issue rows.  A role/indent-only choice can select
    # the sidebar because it happens to be larger.  When the caller asks for
    # issue titles, use observed issue-number links as the stronger semantic
    # boundary; never fall back to inventing issue records when the page has no
    # such evidence.
    try:
        page_path = urlsplit(page_url).path.casefold()
    except ValueError:
        page_path = ""
    if "issue_title" in requested and "/issues" in page_path:
        issue_roots: list[tuple[int, int, str, str]] = []
        for candidate in roots:
            candidate_lines = subtree_lines(content, candidate[2])
            candidate_urls = observed_link_urls(
                "\n".join(candidate_lines), base_url=page_url,
            )
            if any(re.search(r"/issues/\d+(?:/|$)", urlsplit(url).path.casefold())
                   for url in candidate_urls):
                issue_roots.append(candidate)
        if issue_roots:
            roots = issue_roots

    # Prefer a repeated semantic role/indent group over a generic wrapper.
    # Modern headed Playwright snapshots can contain a generic results
    # container whose subtree encloses every card.  Treating that wrapper as
    # the item root collapses the whole page to one record and prevents
    # ``extract_list`` from satisfying a real multi-result task.  Sibling
    # article/listitem/row/card groups are stable item boundaries; generic
    # wrappers remain a bounded fallback when no repeated semantic group is
    # available.
    role_priority = {
        "article": 5, "listitem": 4, "row": 4, "card": 4,
        "region": 2, "group": 1, "generic": 0,
    }
    grouped: dict[tuple[str, int], list[tuple[int, int, str, str]]] = {}
    for candidate in roots:
        role = candidate[3]
        grouped.setdefault((role, candidate[1]), []).append(candidate)
    repeated = [
        (key, members) for key, members in grouped.items()
        if len(members) >= 2
    ]
    if repeated:
        semantic_repeated = [
            item for item in repeated
            if item[0][0] in {"article", "listitem", "row", "card"}
        ]
        if semantic_repeated:
            repeated = semantic_repeated
        _best_key, roots = max(
            repeated,
            key=lambda item: (
                len(item[1]), role_priority.get(item[0][0], 0), -item[0][1], item[0][0],
            ),
        )
    else:
        # Only treat the outer repeated node as an item.  Nested generic nodes
        # such as a card's price/metadata group are fields of that item, not
        # extra records themselves.
        outer_roots: list[tuple[int, int, str, str]] = []
        for candidate in roots:
            candidate_index, candidate_indent, _, _ = candidate
            nested = False
            for parent_index, parent_indent, _, _ in outer_roots:
                if parent_indent >= candidate_indent:
                    continue
                parent_end = len(lines)
                for next_line in lines[parent_index + 1:]:
                    if next_line.strip().startswith("-") and _line_indent(next_line) <= parent_indent:
                        parent_end = lines.index(next_line, parent_index + 1)
                        break
                if parent_index < candidate_index < parent_end:
                    nested = True
                    break
            if not nested:
                outer_roots.append(candidate)
        roots = outer_roots

    dedupe_fields = list(unique_by or ("url", "title"))
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for position, (root_index, root_indent, ref, role) in enumerate(roots):
        if len(items) >= limit:
            break
        selected = [lines[root_index]]
        for line in lines[root_index + 1:]:
            stripped = line.strip()
            if stripped.startswith("### "):
                break
            if stripped.startswith("-") and _line_indent(line) <= root_indent:
                break
            selected.append(line)
        values = _field_values(selected, root_ref=ref, page_url=page_url)
        projected: dict[str, str] = {}
        for field in requested:
            value = values.get(field)
            # Issue/result cards expose their human-readable label as a title;
            # preserve the caller's semantic field name in the evidence record.
            if not value and field in {"issue_title", "framework", "repository"}:
                value = values.get("title") if field != "repository" else values.get("url")
            projected[field] = str(value or "")[:500]
        for field in _CAPABILITY_FIELDS:
            if field in projected and not projected[field].strip():
                # ``unknown`` is explicit and bounded: it is never emitted as
                # ``no`` merely because this page lacked a positive claim.
                projected[field] = "unknown"
        projected = normalize_evidence_fields(projected)
        if not any(projected.values()):
            continue
        if _is_non_record_item(projected):
            continue
        key_parts = [str(projected.get(_canonical_field(field)) or "").casefold().strip()
                     for field in dedupe_fields]
        key = "|".join(key_parts).strip("|") or f"position:{position}"
        if key in seen:
            continue
        seen.add(key)
        support = " ".join(line.strip() for line in selected if line.strip())[:240]
        items.append({
            "ref": ref,
            "role": role,
            "fields": projected,
            "dedupe_key": key[:240],
            "supporting_text": support,
            "source_url": safe_http_url(page_url, strip_query=True),
            "observation_id": str(observation_id or "")[:128],
        })
    return {
        "items": items,
        "fields": requested,
        "count": len(items),
        "limit": limit,
        "observation_id": str(observation_id or "")[:128],
        "source_url": safe_http_url(page_url, strip_query=True),
    }


@dataclass(frozen=True)
class EvidenceRecord:
    kind: str
    tab_id: str
    observation_id: str
    source_url: str
    fields: dict[str, Any]
    supporting_text: str = ""

    def safe_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind[:40],
            "tab_id": self.tab_id[:80],
            "observation_id": self.observation_id[:128],
            "source_url": safe_http_url(self.source_url, strip_query=True),
            "fields": {str(key)[:80]: str(value)[:500] for key, value in self.fields.items()},
            "supporting_text_hash": hashlib.sha256(self.supporting_text.encode("utf-8", "replace")).hexdigest()[:24]
            if self.supporting_text else "",
        }


class BrowserEvidenceLedger:
    """Bounded evidence collection that survives page and Tab changes."""

    def __init__(self, max_records: int = 60):
        self.max_records = max(1, min(200, int(max_records)))
        self._records: list[EvidenceRecord] = []

    def clear(self) -> None:
        self._records.clear()

    def add(self, *, kind: str, tab_id: str, observation_id: str, source_url: str,
            fields: dict[str, Any] | None = None, supporting_text: str = "") -> EvidenceRecord:
        normalized_fields = normalize_evidence_fields(fields)
        source = safe_http_url(source_url, strip_query=True)
        try:
            source_path = urlsplit(source).path.casefold()
        except ValueError:
            source_path = ""
        if (str(kind).casefold() == "list_item" and "/issues" in source_path
                and normalized_fields.get("title") and not normalized_fields.get("issue_title")):
            normalized_fields["issue_title"] = normalized_fields["title"]
        record = EvidenceRecord(
            kind=str(kind or "browser").strip()[:40],
            tab_id=str(tab_id or "")[:80],
            observation_id=str(observation_id or "")[:128],
            source_url=source,
            fields=normalized_fields,
            supporting_text=str(supporting_text or "")[:240],
        )
        self._records.append(record)
        if len(self._records) > self.max_records:
            del self._records[:len(self._records) - self.max_records]
        return record

    @staticmethod
    def _repository_source_key(source_url: str) -> str:
        """Normalize a repository and its Issues URL to the same evidence key."""
        source = safe_http_url(source_url, strip_query=True)
        try:
            parsed = urlsplit(source)
        except ValueError:
            return ""
        host = str(parsed.hostname or "").casefold().rstrip(".")
        if host not in {"github.com", "www.github.com"}:
            return ""
        parts = [part for part in str(parsed.path or "").split("/") if part]
        lowered = [part.casefold() for part in parts]
        if "issues" in lowered:
            parts = parts[:lowered.index("issues")]
        if len(parts) < 2:
            return ""
        return f"https://github.com/{parts[0]}/{parts[1]}".casefold()

    def merge_repository_fields(self, *, source_url: str, fields: dict[str, Any] | None = None,
                                supporting_text: str = "") -> bool:
        """Attach supplemental Issues/README fields to an existing repo record.

        List-item records remain in the ledger for item-level provenance.  The
        repository record is also enriched so a composite research result can
        be verified as one framework record without treating an Issue row as a
        second framework.
        """
        source_key = self._repository_source_key(source_url)
        normalized_fields = normalize_evidence_fields(fields)
        if not source_key or not normalized_fields:
            return False
        for index in range(len(self._records) - 1, -1, -1):
            record = self._records[index]
            if str(record.kind).casefold() in {"list_item", "issue"}:
                continue
            if self._repository_source_key(record.source_url) != source_key:
                continue
            merged_fields = {**record.fields, **normalized_fields}
            merged_supporting = record.supporting_text
            if supporting_text and supporting_text not in merged_supporting:
                merged_supporting = (f"{merged_supporting}; {supporting_text}" if merged_supporting
                                     else supporting_text)
            self._records[index] = EvidenceRecord(
                kind=record.kind,
                tab_id=record.tab_id,
                observation_id=record.observation_id,
                source_url=record.source_url,
                fields=merged_fields,
                supporting_text=merged_supporting[:240],
            )
            return True
        return False

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records)

    def safe_dict(self) -> dict[str, Any]:
        return {"max_records": self.max_records, "count": len(self._records),
                "records": [item.safe_dict() for item in self._records]}


__all__ = [
    "BrowserEvidenceLedger", "EvidenceRecord", "extract_list_from_snapshot",
    "decode_browser_content",
    "origin_from_url", "page_url_from_content", "parse_tab_list", "safe_http_url",
    "is_safe_public_browser_url", "public_site_key", "compatible_public_origin",
    "normalize_capability_status",
    "normalize_evidence_fields", "parse_github_relative_date", "parse_github_star_count",
    "observed_link_url", "observed_link_urls", "subtree_lines",
]
