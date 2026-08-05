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
TASK_STATUS_WAITING_APPROVAL = "waiting_approval"
TASK_STATUS_WAITING_HUMAN = "waiting_human"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUS_FAILED = "failed"

_SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|auth(?:orization)?|cookie|credential|pass(?:word)?|secret|token)", re.I)
_PRIVATE_TEXT_KEY = re.compile(r"(?:^text$|clipboard|message|content|prompt|body|input)", re.I)


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
    if any(marker in text for marker in ("captcha", "验证码", "人机验证", "快速验证身份", "我是人类")):
        return "human_verification"
    if any(marker in text for marker in ("login", "sign in", "登录", "账户", "账号")):
        return "login_required"
    if any(marker in text for marker in ("timed out", "timeout", "网络", "connection", "http 408", "http 429", "http 502", "http 503", "http 504")):
        return "transient_network"
    if any(marker in text for marker in ("permission", "access denied", "权限", "unauthorized", "forbidden")):
        return "permission_denied"
    if any(marker in text for marker in ("verify", "verification", "not found", "验收", "postcondition")):
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
                checkpoint_json TEXT NOT NULL DEFAULT '{}'
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

    def recoverable(self, *, max_age_seconds: float = 3600.0) -> list[dict[str, Any]]:
        """Return active checkpoints; authorization is intentionally not restored."""
        cutoff = time.time() - max(60.0, float(max_age_seconds))
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute("""SELECT task_id, goal, status, updated_at, capability_json, checkpoint_json
                                        FROM tasks WHERE status IN (?, ?) AND updated_at >= ?
                                        ORDER BY updated_at DESC""",
                                      (TASK_STATUS_ACTIVE, TASK_STATUS_WAITING_APPROVAL, cutoff)).fetchall()
        result = []
        for row in rows:
            try:
                capabilities = json.loads(row[4] or "[]")
                checkpoint = json.loads(row[5] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                capabilities, checkpoint = [], {}
            result.append({"task_id": row[0], "goal": row[1], "status": row[2],
                           "updated_at": row[3], "capabilities": capabilities,
                           "checkpoint": checkpoint, "requires_reauthorization": True})
        return result

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute("SELECT task_id, goal, status, created_at, updated_at, failure_kind, capability_json FROM tasks WHERE task_id = ?",
                                     (task_id,)).fetchone()
        if row is None:
            return None
        return {"task_id": row[0], "goal": row[1], "status": row[2], "created_at": row[3], "updated_at": row[4],
                "failure_kind": row[5], "capabilities": json.loads(row[6] or "[]")}

    @staticmethod
    def _event(connection: sqlite3.Connection, task_id: str, kind: str, data: Any) -> None:
        connection.execute("INSERT INTO task_events(task_id, created_at, kind, data_json) VALUES (?, ?, ?, ?)",
                           (task_id, time.time(), str(kind), json.dumps(safe_event_data(data), ensure_ascii=False,
                                                                         separators=(",", ":"))))
