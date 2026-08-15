"""Private, parameterized browser workflow cache and semantic locator helpers.

The cache stores only HMAC keys and a small allow-listed workflow template.  It
never persists the task text, URL, query, target label, page content, refs, or
model output.  Locator descriptors are intentionally semantic: a current
accessibility snapshot can re-bind a role/name target after a DOM re-render.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit
import uuid


CACHE_SCHEMA_VERSION = 1
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
CACHE_FAILURE_COOLDOWN_SECONDS = 24 * 60 * 60
CACHE_MAX_ENTRIES = 200
LOCATOR_MIN_SCORE = 0.85
LOCATOR_MIN_MARGIN = 0.15
# Stop at common Chinese sentence punctuation as well as ASCII whitespace.
# Real task text often writes ``打开 https://host/page。在...`` without a
# space; including the following sentence in ``start_url`` makes deterministic
# replay navigate to a malformed URL.
_URL = re.compile(r"https?://[^\s<>\"'，。；：！？、]+", re.IGNORECASE)
_QUERY = re.compile(
    r"(?:在)?(?:搜索框|搜索栏|搜索|查询|输入)(?:中|里|框中)?\s*"
    r"(?:输入|搜索)?\s*(?:[“\"'](?P<quoted>[^”\"']+)[”\"']|(?P<plain>[A-Za-z0-9][^，。；;，,。]*?))"
    r"(?=\s*(?:，|,|。|；|;|后|然后|并|只|点击|选择|$))",
    re.IGNORECASE,
)
_TARGET = re.compile(r"(?:点击|选择)[^，。；;,]{0,32}?[“‘\"'](?P<label>[^”’\"']+)[”’\"']")
_SNAPSHOT_LINE = re.compile(r"^(?P<indent>\s*)-\s+(?P<role>[A-Za-z0-9_-]+)(?P<rest>.*)$")
_REF = re.compile(r"\[ref=(?P<ref>[^\]\s]+)\]")
_STATE = re.compile(r"\[(?P<value>[^\]]+)\]")
_QUOTED = re.compile(r"(?:\"(?P<double>(?:[^\"\\]|\\.)*)\"|'(?P<single>(?:[^'\\]|\\.)*)')")
_FORBIDDEN_TEMPLATE_KEYS = frozenset({
    "prompt", "url", "query", "text", "content", "screenshot", "screenshot_path",
    "credential", "password", "token", "ref", "answer", "raw_input", "page_body",
})
_SAFE_TEMPLATE_LITERALS = {
    "action": {"navigate", "snapshot", "fill_ref", "wait_for_options", "click_ref", "extract", "verify"},
    "role": {"combobox", "option", "listbox", "article", "section", "region", "main", "group", "generic",
             "textbox", "searchbox", "heading", "paragraph", "link"},
    "postconditions": {"options_observed", "structured_evidence_verified"},
    "fields": {"title", "source", "price", "url"},
    "required_fields": {"title", "source", "price", "url"},
}
_HMAC_HEX = re.compile(r"^[0-9a-f]{64}$")
_SAFE_STRUCTURE_TOKEN = re.compile(r"^[A-Za-z0-9_=-]{1,64}$")
_PROCESS_KEY = secrets.token_bytes(32)


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _digest(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _default_key() -> bytes:
    """Obtain a stable local key without making the cache usable as a secret store."""
    try:
        from credential_store import get_browser_cache_key
        value = get_browser_cache_key()
        if value:
            return value
    except Exception:
        pass
    # This fallback is deliberately process-local.  A production Windows
    # session gets a DPAPI/Credential-Manager-backed key from credential_store.
    return _PROCESS_KEY


@dataclass(frozen=True)
class BrowserTaskIntent:
    kind: str
    origin_key: str
    scope_key: str
    exact_key: str
    query_key: str
    target_key: str
    fields: tuple[str, ...]
    # Raw values are kept only in memory for the current run and excluded from
    # repr/equality so they cannot accidentally become telemetry.
    start_url: str = field(repr=False, compare=False, default="")
    query: str = field(repr=False, compare=False, default="")
    target_label: str = field(repr=False, compare=False, default="")


@dataclass(frozen=True)
class LocatorDescriptor:
    role: str
    name_digest: str = ""
    placeholder: str = ""
    parent_roles: tuple[str, ...] = ()
    state_tokens: tuple[str, ...] = ()
    relative_position: int | None = None

    def safe_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"role": self.role[:40]}
        if self.name_digest:
            value["name_digest"] = self.name_digest[:128]
        if self.placeholder:
            value["placeholder"] = self.placeholder[:40]
        if self.parent_roles:
            value["parent_roles"] = list(self.parent_roles[:8])
        if self.state_tokens:
            value["state_tokens"] = list(self.state_tokens[:8])
        if self.relative_position is not None:
            value["relative_position"] = max(0, int(self.relative_position))
        return value


@dataclass(frozen=True)
class SnapshotCandidate:
    ref: str
    role: str
    name: str = field(repr=False, default="")
    parent_roles: tuple[str, ...] = ()
    state_tokens: tuple[str, ...] = ()
    relative_position: int = 0


@dataclass(frozen=True)
class BrowserCacheEntry:
    entry_id: str
    exact_key: str
    scope_key: str
    template: dict[str, Any]
    created_at: float
    updated_at: float
    disabled_until: float = 0.0
    success_count: int = 0
    failure_count: int = 0


@dataclass(frozen=True)
class CacheLookup:
    status: str
    entry: BrowserCacheEntry | None = None


def _unquote(value: str) -> str:
    text = str(value or "").strip()
    match = _QUOTED.search(text)
    if match:
        return match.group("double") or match.group("single") or ""
    if ":" in text:
        text = text.split(":", 1)[1]
    return text.strip().strip("\"'")[:200]


def parse_snapshot_candidates(snapshot: Any) -> list[SnapshotCandidate]:
    """Parse only role/ref/name metadata from an accessibility snapshot."""
    if isinstance(snapshot, dict):
        snapshot = snapshot.get("content", snapshot.get("text", ""))
    if isinstance(snapshot, str) and snapshot.lstrip().startswith(("[{", "{")):
        try:
            decoded = json.loads(snapshot, strict=False)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, (list, dict)):
            snapshot = decoded
    if isinstance(snapshot, (list, tuple)):
        snapshot = "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in snapshot
        )
    text = str(snapshot or "")
    stack: list[tuple[int, str]] = []
    role_counts: dict[str, int] = {}
    candidates: list[SnapshotCandidate] = []
    for line in text.splitlines():
        match = _SNAPSHOT_LINE.match(line)
        if not match:
            continue
        ref_match = _REF.search(match.group("rest"))
        if not ref_match:
            continue
        role = match.group("role").strip().casefold()
        indent = len(match.group("indent").replace("\t", "    "))
        while stack and indent <= stack[-1][0]:
            stack.pop()
        rest = match.group("rest")
        quoted = _QUOTED.search(rest)
        name = (quoted.group("double") or quoted.group("single")) if quoted else ""
        if not name and ":" in rest:
            name = _unquote(rest.split(":", 1)[1])
        states = []
        for state in _STATE.findall(rest):
            normalized = _normalize(state)
            if normalized.startswith("ref="):
                continue
            if normalized:
                states.append(normalized)
        parent_roles = tuple(item[1] for item in stack[-8:])
        position = role_counts.get(role, 0)
        role_counts[role] = position + 1
        candidates.append(SnapshotCandidate(
            ref=ref_match.group("ref")[:80], role=role, name=name[:500],
            parent_roles=parent_roles, state_tokens=tuple(states[:8]),
            relative_position=position,
        ))
        stack.append((indent, role))
    return candidates


def locator_for_candidate(candidate: SnapshotCandidate, *, key: bytes | None = None,
                          placeholder: str = "") -> LocatorDescriptor:
    cache_key = key or b"deskorb-process-locator-key"
    return LocatorDescriptor(
        role=candidate.role,
        name_digest=_digest(cache_key, _normalize(candidate.name)) if candidate.name else "",
        placeholder=placeholder,
        parent_roles=tuple(candidate.parent_roles),
        state_tokens=tuple(candidate.state_tokens),
        relative_position=candidate.relative_position,
    )


def resolve_locator(descriptor: LocatorDescriptor, candidates: Iterable[SnapshotCandidate],
                    variables: dict[str, str] | None = None, *, key: bytes | None = None,
                    min_score: float = LOCATOR_MIN_SCORE,
                    min_margin: float = LOCATOR_MIN_MARGIN) -> str | None:
    """Resolve a semantic locator only when the best candidate is unambiguous."""
    variables = variables or {}
    expected = str(variables.get(descriptor.placeholder.lstrip("$") or "") or "")
    cache_key = key or b"deskorb-process-locator-key"
    matching_candidates = [candidate for candidate in candidates
                           if candidate.role == descriptor.role.casefold()]
    scored: list[tuple[float, SnapshotCandidate]] = []
    for candidate in matching_candidates:
        if candidate.role != descriptor.role.casefold():
            continue
        score = 0.90 if len(matching_candidates) == 1 else 0.50
        if expected:
            if _normalize(candidate.name) != _normalize(expected):
                score -= 0.30
            else:
                score += 0.50
        elif descriptor.name_digest and candidate.name:
            if hmac.compare_digest(descriptor.name_digest, _digest(cache_key, _normalize(candidate.name))):
                score += 0.50
            else:
                score -= 0.20
        if descriptor.parent_roles:
            if tuple(candidate.parent_roles[-len(descriptor.parent_roles):]) == descriptor.parent_roles:
                score += 0.08
            else:
                score -= 0.08
        if descriptor.state_tokens and set(descriptor.state_tokens).issubset(set(candidate.state_tokens)):
            score += 0.05
        if descriptor.relative_position is not None and candidate.relative_position == descriptor.relative_position:
            score += 0.02
        scored.append((max(0.0, min(1.0, score)), candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1].ref))
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < min_score or best_score - second_score < min_margin:
        return None
    return best.ref


def parse_browser_task_intent(text: str, *, key: bytes | None = None) -> BrowserTaskIntent | None:
    """Recognize the narrow, read-only search workflow eligible for caching."""
    raw = " ".join(str(text or "").strip().split())
    url_match = _URL.search(raw)
    query_match = _QUERY.search(raw)
    target_match = _TARGET.search(raw)
    if not url_match or not query_match or not target_match:
        return None
    start_url = url_match.group(0).rstrip("，。；;,)）")
    query = (query_match.group("quoted") or query_match.group("plain") or "").strip()
    target = target_match.group("label").strip()
    if not query or not target or len(query) > 120 or len(target) > 160:
        return None
    parsed = urlsplit(start_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    fields = ("title", "source") if any(marker in raw for marker in ("来源", "source")) else ("title",)
    cache_key = key or _default_key()
    hostname = (parsed.hostname or "").casefold()
    authority = hostname if hostname in {"127.0.0.1", "localhost", "::1"} else parsed.netloc.casefold()
    origin = f"{parsed.scheme.lower()}://{authority}{parsed.path or '/'}"
    normalized = _normalize(raw)
    scope_value = "search|" + origin + "|" + ",".join(fields)
    return BrowserTaskIntent(
        kind="read_only_search",
        origin_key=_digest(cache_key, origin),
        scope_key=_digest(cache_key, scope_value),
        exact_key=_digest(cache_key, "v1|" + normalized),
        query_key=_digest(cache_key, _normalize(query)),
        target_key=_digest(cache_key, _normalize(target)),
        fields=fields,
        start_url=start_url,
        query=query,
        target_label=target,
    )


def build_parameterized_search_template(*, fields: Iterable[str] = ("title", "source")) -> dict[str, Any]:
    requested = [str(field).strip()[:80] for field in fields if str(field).strip()][:16]
    return {
        "version": CACHE_SCHEMA_VERSION,
        "kind": "read_only_search",
        "read_only": True,
        "steps": [
            {"action": "navigate", "url": "$start_url"},
            {"action": "snapshot"},
            {"action": "fill_ref", "locator": {"role": "combobox", "placeholder": "$query"},
             "value": "$query"},
            {"action": "wait_for_options"},
            {"action": "click_ref", "locator": {"role": "option", "placeholder": "$target_label"}},
            {"action": "extract", "locator": {"role": "article", "placeholder": "$result_root"},
             "fields": requested},
            {"action": "verify", "required_fields": requested},
        ],
        "postconditions": ["options_observed", "structured_evidence_verified"],
    }


def _validate_template(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("browser workflow template must be an object")
    def walk(item: Any, key_name: str = "") -> Any:
        if isinstance(item, dict):
            output = {}
            for key, child in item.items():
                name = str(key).casefold()
                if name in _FORBIDDEN_TEMPLATE_KEYS and not (
                    isinstance(child, str) and child.startswith("$")
                ):
                    raise ValueError("browser workflow template contains disallowed data")
                output[str(key)] = walk(child, name)
            return output
        if isinstance(item, list):
            return [walk(child, key_name) for child in item[:32]]
        if isinstance(item, str):
            allowed = _SAFE_TEMPLATE_LITERALS.get(key_name, set())
            safe_digest = key_name == "name_digest" and bool(_HMAC_HEX.fullmatch(item))
            safe_structure = key_name in {"parent_roles", "state_tokens"} and bool(
                _SAFE_STRUCTURE_TOKEN.fullmatch(item)
            )
            if len(item) > 160 or not (item.startswith("$") or item == "read_only_search"
                                       or item in allowed or safe_digest or safe_structure):
                raise ValueError("browser workflow template contains raw data")
            return item
        if isinstance(item, (bool, int, float)) or item is None:
            return item
        raise ValueError("browser workflow template contains unsupported data")
    result = walk(value)
    if result.get("version") != CACHE_SCHEMA_VERSION or not result.get("read_only"):
        raise ValueError("unsupported browser workflow template")
    return result


class BrowserActionCache:
    """Small SQLite cache with bounded retention and failure cooldowns."""

    def __init__(self, path: str | Path, *, key: bytes | None = None,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = bytes(key or _default_key())
        if len(self.key) < 16:
            raise ValueError("browser cache key must be at least 16 bytes")
        self.clock = clock
        self._initialize()

    @classmethod
    def default(cls) -> "BrowserActionCache":
        import os
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return cls(root / "deskorb-agent" / "browser-action-cache.sqlite3")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA busy_timeout=3000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS browser_workflows (
                    entry_id TEXT PRIMARY KEY,
                    exact_key TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    template_key TEXT NOT NULL,
                    template_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    disabled_until REAL NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_browser_workflows_exact ON browser_workflows(exact_key)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_browser_workflows_scope ON browser_workflows(scope_key)")

    def lookup(self, intent: BrowserTaskIntent | None) -> CacheLookup:
        if intent is None:
            return CacheLookup("miss")
        now = float(self.clock())
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT entry_id, exact_key, scope_key, template_json, created_at, updated_at, "
                "disabled_until, success_count, failure_count FROM browser_workflows "
                "WHERE (exact_key = ? OR scope_key = ?) AND schema_version = ? "
                "ORDER BY CASE WHEN exact_key = ? THEN 0 ELSE 1 END, updated_at DESC LIMIT 1",
                (intent.exact_key, intent.scope_key, CACHE_SCHEMA_VERSION, intent.exact_key),
            ).fetchone()
        if not row:
            return CacheLookup("miss")
        entry = self._entry(row)
        if entry.disabled_until > now:
            return CacheLookup("disabled", entry)
        if now - entry.updated_at > CACHE_TTL_SECONDS:
            return CacheLookup("expired", entry)
        return CacheLookup("exact_hit" if entry.exact_key == intent.exact_key else "template_hit", entry)

    def record_success(self, intent: BrowserTaskIntent, template: dict[str, Any]) -> BrowserCacheEntry:
        safe = _validate_template(template)
        now = float(self.clock())
        entry_id = str(uuid.uuid4())
        serialized = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT entry_id, success_count FROM browser_workflows WHERE exact_key = ? LIMIT 1",
                (intent.exact_key,),
            ).fetchone()
            if existing:
                entry_id = str(existing[0])
                success_count = int(existing[1]) + 1
                connection.execute(
                    "UPDATE browser_workflows SET scope_key=?, template_key=?, template_json=?, "
                    "schema_version=?, updated_at=?, disabled_until=0, success_count=?, failure_count=0 "
                    "WHERE entry_id=?",
                    (intent.scope_key, intent.scope_key, serialized, CACHE_SCHEMA_VERSION, now,
                     success_count, entry_id),
                )
            else:
                connection.execute(
                    "INSERT INTO browser_workflows(entry_id, exact_key, scope_key, template_key, template_json, "
                    "schema_version, created_at, updated_at, success_count, failure_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0)",
                    (entry_id, intent.exact_key, intent.scope_key, intent.scope_key, serialized,
                     CACHE_SCHEMA_VERSION, now, now),
                )
            connection.execute(
                "DELETE FROM browser_workflows WHERE entry_id IN (SELECT entry_id FROM browser_workflows "
                "ORDER BY updated_at DESC LIMIT -1 OFFSET ?)", (CACHE_MAX_ENTRIES,)
            )
        found = self.lookup(intent)
        if not found.entry:
            raise RuntimeError("browser cache entry could not be read after write")
        return found.entry

    def record_failure(self, entry_id: str) -> None:
        now = float(self.clock())
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE browser_workflows SET failure_count=failure_count+1, updated_at=?, disabled_until=? WHERE entry_id=?",
                (now, now + CACHE_FAILURE_COOLDOWN_SECONDS, str(entry_id)),
            )

    @staticmethod
    def _entry(row: tuple[Any, ...]) -> BrowserCacheEntry:
        try:
            template = json.loads(str(row[3]))
        except (TypeError, ValueError, json.JSONDecodeError):
            template = {}
        return BrowserCacheEntry(
            entry_id=str(row[0]), exact_key=str(row[1]), scope_key=str(row[2]),
            template=template if isinstance(template, dict) else {},
            created_at=float(row[4]), updated_at=float(row[5]), disabled_until=float(row[6]),
            success_count=int(row[7]), failure_count=int(row[8]),
        )


__all__ = [
    "BrowserActionCache", "BrowserCacheEntry", "BrowserTaskIntent", "CacheLookup",
    "LocatorDescriptor", "SnapshotCandidate", "build_parameterized_search_template",
    "locator_for_candidate", "parse_browser_task_intent", "parse_snapshot_candidates",
    "resolve_locator",
]
