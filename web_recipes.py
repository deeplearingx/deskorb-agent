"""Local, privacy-safe storage for verified browser workflow recipes."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


class WebRecipeStore:
    """Persist only origin, stable locators, preconditions and verifier data."""

    DISABLE_AFTER_FAILURES = 2

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS web_recipes (
                recipe_id TEXT PRIMARY KEY, origin TEXT NOT NULL, kind TEXT NOT NULL,
                locators_json TEXT NOT NULL, preconditions_json TEXT NOT NULL,
                verifier_json TEXT NOT NULL, version INTEGER NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )""")

    def save_verified(self, recipe_id: str, *, origin: str, kind: str, locators: list[dict[str, Any]],
                      preconditions: dict[str, Any], verifier: dict[str, Any], version: int = 1,
                      validators_passed: bool) -> bool:
        if not validators_passed or not self._safe_origin(origin) or not self._safe_locators(locators):
            return False
        now = time.time()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""INSERT INTO web_recipes(recipe_id, origin, kind, locators_json, preconditions_json,
                                  verifier_json, version, created_at, updated_at)
                                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                  ON CONFLICT(recipe_id) DO UPDATE SET origin=excluded.origin, kind=excluded.kind,
                                  locators_json=excluded.locators_json, preconditions_json=excluded.preconditions_json,
                                  verifier_json=excluded.verifier_json, version=excluded.version, failures=0,
                                  disabled=0, updated_at=excluded.updated_at""",
                               (str(recipe_id), origin, str(kind)[:80], json.dumps(locators, ensure_ascii=False),
                                json.dumps(preconditions, ensure_ascii=False), json.dumps(verifier, ensure_ascii=False),
                                int(version), now, now))
        return True

    def candidate(self, recipe_id: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT origin, kind, locators_json, preconditions_json, verifier_json, version, failures, disabled FROM web_recipes WHERE recipe_id=?", (str(recipe_id),)).fetchone()
        if row is None or row[7]:
            return None
        return {"recipe_id": str(recipe_id), "origin": row[0], "kind": row[1], "locators": json.loads(row[2]),
                "preconditions": json.loads(row[3]), "verifier": json.loads(row[4]), "version": row[5],
                "failures": row[6], "requires_revalidation": True}

    def record_revalidation(self, recipe_id: str, passed: bool) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            if passed:
                connection.execute("UPDATE web_recipes SET failures=0, updated_at=? WHERE recipe_id=?", (time.time(), str(recipe_id)))
            else:
                connection.execute("UPDATE web_recipes SET failures=failures+1, disabled=CASE WHEN failures+1 >= ? THEN 1 ELSE 0 END, updated_at=? WHERE recipe_id=?", (self.DISABLE_AFTER_FAILURES, time.time(), str(recipe_id)))

    @staticmethod
    def _safe_origin(origin: str) -> bool:
        return str(origin).startswith(("http://", "https://")) and len(str(origin)) <= 300

    @staticmethod
    def _safe_locators(locators: Any) -> bool:
        if not isinstance(locators, list) or not locators or len(locators) > 24:
            return False
        return all(isinstance(item, dict) and set(item).issubset({"role", "name", "selector", "ref"})
                   and any(str(value).strip() for value in item.values()) for item in locators)
