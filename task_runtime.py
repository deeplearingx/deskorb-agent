"""Local, privacy-aware task records for DeskOrb's action runtime.

The journal deliberately records evidence about execution, not full model
transcripts or user content.  It makes a stopped task explainable without
turning the task log into another copy of the user's screen, clipboard, or
messages.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TASK_STATUS_ACTIVE = "active"
TASK_STATUS_PAUSED = "paused"
TASK_STATUS_WAITING_APPROVAL = "waiting_approval"
TASK_STATUS_WAITING_HUMAN = "waiting_human"
TASK_STATUS_WAITING_VERIFICATION = "waiting_verification"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUS_FAILED = "failed"

_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|auth(?:orization)?|cookie|credential|pass(?:word)?|secret|token)", re.I)
_PRIVATE_TEXT_KEY = re.compile(r"(?:^text$|clipboard|message|content|prompt|body|input|url)", re.I)


@dataclass(frozen=True)
class TaskLease:
    """A short-lived authorization for a named set of action capabilities."""

    task_id: str
    capabilities: frozenset[str]
    issued_at: float
    expires_at: float

    def active(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) < self.expires_at

    def allows(self, capability: str, now: float | None = None) -> bool:
        return self.active(now) and capability in self.capabilities


def new_task_id() -> str:
    return "T" + secrets.token_hex(5).upper()


def classify_failure(value: Any) -> str:
    """Return a stable recovery category without exposing raw upstream text."""
    text = str(value or "").lower()
    if any(marker in text for marker in ("api circuit open", "circuit open", "熔断")):
        return "provider_circuit_open"
    if any(marker in text for marker in ("duplicate side effect", "duplicate_prevented", "重复副作用")):
        return "duplicate_prevented"
    if any(marker in text for marker in ("captcha", "验证码", "人机验证", "快速验证身份", "我是人类")):
        return "human_verification"
    if any(marker in text for marker in ("login", "sign in", "登录", "账户", "账号")):
        return "login_required"
    if any(marker in text for marker in ("timed out", "timeout", "网络", "connection", "bad gateway",
                                         "service unavailable", "temporarily unavailable", "try again",
                                         "http 408", "http 425", "http 429", "http 500", "http 502", "http 503", "http 504")):
        return "transient_network"
    if any(marker in text for marker in ("permission", "access denied", "权限", "unauthorized", "forbidden")):
        return "permission_denied"
    if any(marker in text for marker in ("verify", "verification", "not found", "验收", "postcondition", "not verified", "待验证")):
        return "verification_failed"
    if any(marker in text for marker in ("mcp", "tool", "function call")):
        return "tool_failure"
    return "unknown"


def safe_event_data(value: Any) -> Any:
    """Retain structural evidence while redacting secrets and free-form user text."""
    if isinstance(value, dict):
        return {str(key): _safe_value(str(key), item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_value("", item) for item in value[:32]]
    return _safe_value("", value)


def effect_fingerprint(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    """Build a stable, redacted identifier for a potentially side-effecting call."""
    safe = safe_event_data({"tool": str(tool_name), "arguments": arguments or {}})
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _safe_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _safe_value(str(item_key), item) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_safe_value(key, item) for item in value[:32]]
    if isinstance(value, str) and (_PRIVATE_TEXT_KEY.search(key) or len(value) > 240):
        return {"redacted": True, "length": len(value), "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]}
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:240]


def _evidence_snapshot_from_records(task: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Project journal records into the same bounded, structural overlay view."""
    allowed = {
        "task_started", "status", "authorization_granted", "workflow_node",
        "tool_result", "checkpoint", "browser_checkpoint", "browser_space_created",
        "browser_space_closed", "browser_space_broken", "browser_space_reconnected",
        "task_paused", "task_resumed", "human_handoff", "browser_control_resumed",
        "workflow_finished", "effect_recorded", "approval_requested",
    }
    projected: list[dict[str, Any]] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind not in allowed:
            continue
        value = safe_event_data(event.get("data") or {})
        if not isinstance(value, dict):
            value = {}
        compact = {key: value[key] for key in (
            "status", "failure_kind", "tool", "ok", "verified", "evidence",
            "evidence_schema", "terminal", "steps", "space_id", "marker",
            "last_tool", "last_status", "duplicate_prevented", "recovery_reason",
        ) if key in value}
        projected.append({"at": float(event.get("created_at") or 0), "kind": kind, "data": compact})
    return {
        "ok": True, "task_id": task["task_id"], "goal": str(task["goal"])[:240],
        "status": str(task["status"]), "updated_at": float(task["updated_at"]),
        "failure_kind": task.get("failure_kind"), "events": projected,
    }


class InMemoryTaskJournal:
    """Non-persistent task records for privacy-bounded live acceptance checks.

    This intentionally implements the same narrow runtime surface as
    :class:`TaskJournal` without creating a database, recovery record, or
    filesystem artifact. It is suitable only for short-lived tasks whose
    process-restart recovery is deliberately disabled.
    """

    path: None = None

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}

    def start(self, goal: str) -> str:
        task_id = new_task_id()
        now = time.time()
        safe_goal = " ".join(str(goal or "Desktop task").split())[:240]
        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id, "goal": safe_goal, "status": TASK_STATUS_ACTIVE,
                "created_at": now, "updated_at": now, "failure_kind": None,
                "capabilities": [], "checkpoint": {}, "contract": {},
            }
            self._append_event(task_id, "task_started", {"goal": safe_goal})
        return task_id

    def event(self, task_id: str | None, kind: str, data: Any | None = None) -> None:
        if not task_id:
            return
        with self._lock:
            if task_id not in self._tasks:
                return
            self._append_event(task_id, kind, data or {})
            self._tasks[task_id]["updated_at"] = time.time()

    def set_status(self, task_id: str | None, status: str, *, failure: Any | None = None,
                   capabilities: set[str] | frozenset[str] | None = None) -> None:
        if not task_id:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            failure_kind = classify_failure(failure) if failure else None
            task.update(status=status, updated_at=time.time(), failure_kind=failure_kind,
                        capabilities=sorted(capabilities or ()))
            self._append_event(task_id, "status", {
                "status": status, "failure_kind": failure_kind,
                "capabilities": sorted(capabilities or ()),
            })

    def checkpoint(self, task_id: str | None, data: dict[str, Any] | None = None) -> None:
        if not task_id:
            return
        safe = safe_event_data(data or {})
        if not isinstance(safe, dict):
            safe = {}
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.update(checkpoint=safe, updated_at=time.time())
            self._append_event(task_id, "checkpoint", safe)

    def set_contract(self, task_id: str | None, contract: dict[str, Any] | None = None) -> None:
        if not task_id:
            return
        safe = safe_event_data(contract or {})
        if not isinstance(safe, dict):
            safe = {}
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.update(contract=safe, updated_at=time.time())
            self._append_event(task_id, "task_contract", safe)

    def record_effect(self, task_id: str | None, tool_name: str,
                      arguments: dict[str, Any] | None = None) -> str | None:
        if not task_id:
            return None
        fingerprint = effect_fingerprint(tool_name, arguments)
        self.event(task_id, "effect_recorded", {"tool": str(tool_name), "fingerprint": fingerprint})
        return fingerprint

    def effect_seen(self, task_id: str | None, tool_name: str,
                    arguments: dict[str, Any] | None = None) -> bool:
        if not task_id:
            return False
        fingerprint = effect_fingerprint(tool_name, arguments)
        with self._lock:
            return any(event["kind"] == "effect_recorded"
                       and event["data"].get("fingerprint") == fingerprint
                       for event in self._events.get(task_id, ()))

    def recoverable(self, *, max_age_seconds: float = 3600.0) -> list[dict[str, Any]]:
        """Memory-only tasks cannot survive a process restart."""
        return []

    def evidence_snapshot(self, task_id: str | None, *, limit: int = 40) -> dict[str, Any]:
        wanted = str(task_id or "").strip()
        with self._lock:
            task = self._tasks.get(wanted)
            if task is None:
                return {"ok": False, "error": "Task was not found."}
            try:
                count = max(1, min(int(limit or 40), 80))
            except (TypeError, ValueError):
                count = 40
            events = list(self._events.get(wanted, ())[-count:])
            return _evidence_snapshot_from_records(task, events)

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task is not None else None

    def _append_event(self, task_id: str, kind: str, data: Any) -> None:
        self._events.setdefault(task_id, []).append({
            "created_at": time.time(), "kind": str(kind), "data": safe_event_data(data),
        })


class TaskJournal:
    """Small SQLite event journal, safe to use from the overlay worker thread."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                failure_kind TEXT,
                capability_json TEXT NOT NULL DEFAULT '[]',
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                contract_json TEXT NOT NULL DEFAULT '{}'
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                kind TEXT NOT NULL,
                data_json TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(task_id)
            )""")
            # Existing installations created before checkpoint support need a
            # non-destructive schema migration on first use.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
            if "checkpoint_json" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN checkpoint_json TEXT NOT NULL DEFAULT '{}' ")
            if "contract_json" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN contract_json TEXT NOT NULL DEFAULT '{}' ")

    def start(self, goal: str) -> str:
        task_id = new_task_id()
        now = time.time()
        safe_goal = " ".join(str(goal or "Desktop task").split())[:240]
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("INSERT INTO tasks(task_id, goal, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                               (task_id, safe_goal, TASK_STATUS_ACTIVE, now, now))
            self._event(connection, task_id, "task_started", {"goal": safe_goal})
        return task_id

    def event(self, task_id: str | None, kind: str, data: Any | None = None) -> None:
        if not task_id:
            return
        with self._lock, closing(self._connect()) as connection, connection:
            self._event(connection, task_id, kind, data or {})
            connection.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (time.time(), task_id))

    def set_status(self, task_id: str | None, status: str, *, failure: Any | None = None,
                   capabilities: set[str] | frozenset[str] | None = None) -> None:
        if not task_id:
            return
        now = time.time()
        failure_kind = classify_failure(failure) if failure else None
        capability_json = json.dumps(sorted(capabilities or ()), ensure_ascii=False)
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("""UPDATE tasks SET status = ?, updated_at = ?, failure_kind = ?, capability_json = ?
                                  WHERE task_id = ?""",
                               (status, now, failure_kind, capability_json, task_id))
            self._event(connection, task_id, "status", {"status": status, "failure_kind": failure_kind,
                                                          "capabilities": sorted(capabilities or ())})

    def checkpoint(self, task_id: str | None, data: dict[str, Any] | None = None) -> None:
        """Save structural resume state, never a transcript or free-form payload."""
        if not task_id:
            return
        safe = safe_event_data(data or {})
        if not isinstance(safe, dict):
            safe = {}
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("UPDATE tasks SET checkpoint_json = ?, updated_at = ? WHERE task_id = ?",
                               (json.dumps(safe, ensure_ascii=False, separators=(",", ":")), time.time(), task_id))
            self._event(connection, task_id, "checkpoint", safe)

    def set_contract(self, task_id: str | None, contract: dict[str, Any] | None = None) -> None:
        """Persist only the structural task contract, never model prompt text."""
        if not task_id:
            return
        safe = safe_event_data(contract or {})
        if not isinstance(safe, dict):
            safe = {}
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("UPDATE tasks SET contract_json = ?, updated_at = ? WHERE task_id = ?",
                               (json.dumps(safe, ensure_ascii=False, separators=(",", ":")), time.time(), task_id))
            self._event(connection, task_id, "task_contract", safe)

    def record_effect(self, task_id: str | None, tool_name: str, arguments: dict[str, Any] | None = None) -> str | None:
        """Record a successful side-effect fingerprint for resume-time deduplication."""
        if not task_id:
            return None
        fingerprint = effect_fingerprint(tool_name, arguments)
        self.event(task_id, "effect_recorded", {"tool": str(tool_name), "fingerprint": fingerprint})
        return fingerprint

    def effect_seen(self, task_id: str | None, tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
        if not task_id:
            return False
        fingerprint = effect_fingerprint(tool_name, arguments)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute("SELECT data_json FROM task_events WHERE task_id = ? AND kind = 'effect_recorded'",
                                      (task_id,)).fetchall()
        for (raw,) in rows:
            try:
                if json.loads(raw).get("fingerprint") == fingerprint:
                    return True
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return False

    def recoverable(self, *, max_age_seconds: float = 3600.0) -> list[dict[str, Any]]:
        """Return active checkpoints; authorization is intentionally not restored."""
        cutoff = time.time() - max(60.0, float(max_age_seconds))
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute("""SELECT task_id, goal, status, updated_at, capability_json, checkpoint_json, contract_json
                                        FROM tasks WHERE status IN (?, ?, ?, ?, ?) AND updated_at >= ?
                                        ORDER BY updated_at DESC""",
                                      (TASK_STATUS_ACTIVE, TASK_STATUS_PAUSED, TASK_STATUS_WAITING_APPROVAL,
                                       TASK_STATUS_WAITING_HUMAN, TASK_STATUS_WAITING_VERIFICATION, cutoff)).fetchall()
        result = []
        for row in rows:
            try:
                capabilities = json.loads(row[4] or "[]")
                checkpoint = json.loads(row[5] or "{}")
                contract = json.loads(row[6] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                capabilities, checkpoint, contract = [], {}, {}
            result.append({"task_id": row[0], "goal": row[1], "status": row[2],
                           "updated_at": row[3], "capabilities": capabilities,
                           "checkpoint": checkpoint, "contract": contract,
                           "requires_reauthorization": True})
        return result

    def evidence_snapshot(self, task_id: str | None, *, limit: int = 40) -> dict[str, Any]:
        """Return bounded structural evidence for a task without private payloads.

        Task events are already redacted at write time, but this method applies a
        second structural projection before exposing them to the overlay.  It is
        intentionally useful for a user-facing audit trail, not a transcript
        viewer: tool arguments, page text, messages, screenshots, and secrets
        remain redacted or omitted.
        """
        wanted = str(task_id or "").strip()
        if not wanted:
            return {"ok": False, "error": "No task is active."}
        try:
            count = max(1, min(int(limit or 40), 80))
        except (TypeError, ValueError):
            count = 40
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT task_id, goal, status, updated_at, failure_kind FROM tasks WHERE task_id = ?",
                (wanted,),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "Task was not found."}
            events = connection.execute(
                "SELECT created_at, kind, data_json FROM task_events WHERE task_id = ? "
                "ORDER BY event_id DESC LIMIT ?", (wanted, count),
            ).fetchall()
        task = {
            "task_id": row[0], "goal": row[1], "status": row[2],
            "updated_at": row[3], "failure_kind": row[4],
        }
        records: list[dict[str, Any]] = []
        for created_at, kind, raw in reversed(events):
            try:
                value = json.loads(raw or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                value = {}
            records.append({"created_at": created_at, "kind": kind, "data": value})
        return _evidence_snapshot_from_records(task, records)

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute("SELECT task_id, goal, status, created_at, updated_at, failure_kind, capability_json, contract_json FROM tasks WHERE task_id = ?",
                                     (task_id,)).fetchone()
        if row is None:
            return None
        return {"task_id": row[0], "goal": row[1], "status": row[2], "created_at": row[3], "updated_at": row[4],
                "failure_kind": row[5], "capabilities": json.loads(row[6] or "[]"),
                "contract": json.loads(row[7] or "{}")}

    @staticmethod
    def _event(connection: sqlite3.Connection, task_id: str, kind: str, data: Any) -> None:
        connection.execute("INSERT INTO task_events(task_id, created_at, kind, data_json) VALUES (?, ?, ?, ?)",
                           (task_id, time.time(), str(kind), json.dumps(safe_event_data(data), ensure_ascii=False,
                                                                         separators=(",", ":"))))
