"""SQLite-backed local storage for generated workflow records only."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


_SOURCE_SUMMARY_KEYS = frozenset({"kind", "title", "name", "window_title", "character_count"})
_UNSET = object()


@dataclass(frozen=True)
class ActionItem:
    id: int
    record_id: int
    task: str
    owner: str
    due_date: str
    priority: str
    status: str


@dataclass(frozen=True)
class WorkflowRecord:
    id: int
    workflow_id: str
    markdown: str
    structured_data: dict[str, Any] | None
    created_at: datetime
    source_summaries: list[dict[str, str]]
    action_items: list[ActionItem]


def default_database_path() -> Path:
    """Return the per-user database path without creating it."""
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "codex-overlay"
    return root / "workflow-records.sqlite3"


class WorkflowRecordRepository:
    """Persist generated results and editable action items without source bodies."""

    def __init__(self, database_path: str | Path | None = None):
        self.database_path = Path(database_path or default_database_path())
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def create_record(
        self,
        *,
        workflow_id: str,
        markdown: str,
        structured_data: Mapping[str, Any] | None,
        source_summaries: Iterable[Mapping[str, Any]],
        created_at: datetime | None = None,
    ) -> WorkflowRecord:
        """Create a record from generated output and compact source descriptions."""
        timestamp = _as_utc(created_at or datetime.now(timezone.utc))
        summaries = _sanitize_source_summaries(source_summaries)
        data = dict(structured_data) if structured_data is not None else None
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO records (workflow_id, markdown, structured_json, created_at, source_summaries_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    markdown,
                    json.dumps(data, ensure_ascii=False) if data is not None else None,
                    timestamp.isoformat(),
                    json.dumps(summaries, ensure_ascii=False),
                ),
            )
            record_id = int(cursor.lastrowid)
            self._replace_action_items(record_id, data)
        return self.get_record(record_id)  # type: ignore[return-value]

    def get_record(self, record_id: int) -> WorkflowRecord | None:
        row = self._connection.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return self._record_from_row(row) if row else None

    def list_records(
        self,
        filter_name: str = "all",
        *,
        today: date | None = None,
        near_due_days: int = 7,
    ) -> list[WorkflowRecord]:
        """List newest records, optionally based on open action-item due dates."""
        if filter_name not in {"all", "open", "near_due", "overdue"}:
            raise ValueError(f"Unknown record filter: {filter_name}")
        reference_day = today or date.today()
        params: list[str] = []
        where = ""
        if filter_name == "open":
            where = "WHERE EXISTS (SELECT 1 FROM action_items a WHERE a.record_id = records.id AND a.status != 'completed')"
        elif filter_name == "near_due":
            where = """WHERE EXISTS (
                SELECT 1 FROM action_items a
                WHERE a.record_id = records.id AND a.status != 'completed'
                  AND a.due_date >= ? AND a.due_date <= ?
            )"""
            params = [reference_day.isoformat(), (reference_day + timedelta(days=near_due_days)).isoformat()]
        elif filter_name == "overdue":
            where = """WHERE EXISTS (
                SELECT 1 FROM action_items a
                WHERE a.record_id = records.id AND a.status != 'completed' AND a.due_date != ''
                  AND a.due_date < ?
            )"""
            params = [reference_day.isoformat()]
        rows = self._connection.execute(
            f"SELECT * FROM records {where} ORDER BY created_at DESC, id DESC", params
        ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def update_record(
        self,
        record_id: int,
        *,
        markdown: str | object = _UNSET,
        structured_data: Mapping[str, Any] | None | object = _UNSET,
        source_summaries: Iterable[Mapping[str, Any]] | object = _UNSET,
    ) -> WorkflowRecord:
        """Update generated record fields, retaining only safe source summaries."""
        current = self.get_record(record_id)
        if current is None:
            raise ValueError(f"Unknown record: {record_id}")
        values = {
            "markdown": current.markdown if markdown is _UNSET else str(markdown),
            "structured_json": (
                json.dumps(current.structured_data, ensure_ascii=False)
                if structured_data is _UNSET and current.structured_data is not None
                else (json.dumps(dict(structured_data), ensure_ascii=False) if structured_data is not _UNSET and structured_data is not None else None)
            ),
            "source_summaries_json": (
                json.dumps(current.source_summaries, ensure_ascii=False)
                if source_summaries is _UNSET
                else json.dumps(_sanitize_source_summaries(source_summaries), ensure_ascii=False)
            ),
        }
        with self._connection:
            self._connection.execute(
                """UPDATE records SET markdown = ?, structured_json = ?, source_summaries_json = ?
                   WHERE id = ?""",
                (values["markdown"], values["structured_json"], values["source_summaries_json"], record_id),
            )
            if structured_data is not _UNSET:
                self._replace_action_items(record_id, None if structured_data is None else dict(structured_data))
        return self.get_record(record_id)  # type: ignore[return-value]

    def delete_record(self, record_id: int) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM records WHERE id = ?", (record_id,))

    def update_action_item(
        self,
        action_item_id: int,
        *,
        task: str | object = _UNSET,
        owner: str | object = _UNSET,
        due_date: str | object = _UNSET,
        priority: str | object = _UNSET,
        status: str | object = _UNSET,
    ) -> ActionItem:
        """Edit a user-owned action item without regenerating the whole workflow result."""
        row = self._connection.execute("SELECT * FROM action_items WHERE id = ?", (action_item_id,)).fetchone()
        if not row:
            raise ValueError(f"Unknown action item: {action_item_id}")
        values = {
            "task": row["task"] if task is _UNSET else str(task).strip(),
            "owner": row["owner"] if owner is _UNSET else str(owner).strip(),
            "due_date": row["due_date"] if due_date is _UNSET else _normalize_due_date(str(due_date)),
            "priority": row["priority"] if priority is _UNSET else str(priority).strip(),
            "status": row["status"] if status is _UNSET else _normalize_status(str(status)),
        }
        if not values["task"]:
            raise ValueError("Action item task cannot be empty")
        with self._connection:
            self._connection.execute(
                """UPDATE action_items
                   SET task = ?, owner = ?, due_date = ?, priority = ?, status = ? WHERE id = ?""",
                (*values.values(), action_item_id),
            )
            self._sync_record_action_items(row["record_id"])
        updated = self._connection.execute("SELECT * FROM action_items WHERE id = ?", (action_item_id,)).fetchone()
        return _action_from_row(updated)

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    markdown TEXT NOT NULL,
                    structured_json TEXT,
                    created_at TEXT NOT NULL,
                    source_summaries_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_items (
                    id INTEGER PRIMARY KEY,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    task TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    due_date TEXT NOT NULL DEFAULT '',
                    priority TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open'
                );
                CREATE INDEX IF NOT EXISTS idx_action_items_filter
                    ON action_items(record_id, status, due_date);
                """
            )

    def _replace_action_items(self, record_id: int, structured_data: Mapping[str, Any] | None) -> None:
        self._connection.execute("DELETE FROM action_items WHERE record_id = ?", (record_id,))
        if not structured_data:
            return
        items = structured_data.get("action_items", [])
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, Mapping):
                continue
            task = str(item.get("task", "")).strip()
            if not task:
                continue
            self._connection.execute(
                """INSERT INTO action_items (record_id, task, owner, due_date, priority, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record_id,
                    task,
                    str(item.get("owner", "")).strip(),
                    _normalize_due_date(str(item.get("due_date", ""))),
                    str(item.get("priority", "")).strip(),
                    _normalize_status(str(item.get("status", "open"))),
                ),
            )

    def _sync_record_action_items(self, record_id: int) -> None:
        """Mirror editable task fields back to the generated structured record atomically."""
        row = self._connection.execute(
            "SELECT structured_json FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        if not row or not row["structured_json"]:
            return
        structured_data = json.loads(row["structured_json"])
        if not isinstance(structured_data, dict) or "action_items" not in structured_data:
            return
        action_rows = self._connection.execute(
            "SELECT * FROM action_items WHERE record_id = ? ORDER BY id", (record_id,)
        ).fetchall()
        structured_data["action_items"] = [
            {
                "task": action["task"],
                "owner": action["owner"],
                "due_date": action["due_date"],
                "priority": action["priority"],
                "status": action["status"],
            }
            for action in action_rows
        ]
        self._connection.execute(
            "UPDATE records SET structured_json = ? WHERE id = ?",
            (json.dumps(structured_data, ensure_ascii=False), record_id),
        )

    def _record_from_row(self, row: sqlite3.Row) -> WorkflowRecord:
        actions = self._connection.execute(
            "SELECT * FROM action_items WHERE record_id = ? ORDER BY id", (row["id"],)
        ).fetchall()
        return WorkflowRecord(
            id=row["id"],
            workflow_id=row["workflow_id"],
            markdown=row["markdown"],
            structured_data=json.loads(row["structured_json"]) if row["structured_json"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            source_summaries=json.loads(row["source_summaries_json"]),
            action_items=[_action_from_row(action) for action in actions],
        )


def _sanitize_source_summaries(summaries: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {key: str(summary[key]) for key in _SOURCE_SUMMARY_KEYS if key in summary and str(summary[key]).strip()}
        for summary in summaries
    ]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_due_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("Due date must use YYYY-MM-DD") from exc


def _normalize_status(value: str) -> str:
    value = value.strip().lower()
    if not value:
        raise ValueError("Action item status cannot be empty")
    return value


def _action_from_row(row: sqlite3.Row) -> ActionItem:
    return ActionItem(
        id=row["id"],
        record_id=row["record_id"],
        task=row["task"],
        owner=row["owner"],
        due_date=row["due_date"],
        priority=row["priority"],
        status=row["status"],
    )
