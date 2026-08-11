import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from task_runtime import (InMemoryTaskJournal, TASK_STATUS_COMPLETED, TASK_STATUS_PAUSED, TASK_STATUS_WAITING_VERIFICATION,
                          TaskJournal, TaskLease, classify_failure, safe_event_data)


class TaskRuntimeTests(unittest.TestCase):
    def test_lease_requires_the_named_capability_and_expires(self):
        lease = TaskLease("TTEST", frozenset({"desktop_control"}), 100.0, 110.0)
        self.assertTrue(lease.allows("desktop_control", now=109.9))
        self.assertFalse(lease.allows("mcp:playwright", now=109.9))
        self.assertFalse(lease.allows("desktop_control", now=110.0))

    def test_event_data_redacts_secrets_and_free_form_text(self):
        value = safe_event_data({"api_key": "sk-private", "text": "send this private message",
                                 "url": "https://example.test/a"})
        self.assertEqual(value["api_key"], "[redacted]")
        self.assertTrue(value["text"]["redacted"])
        self.assertNotIn("private message", json.dumps(value, ensure_ascii=False))
        self.assertTrue(value["url"]["redacted"])
        self.assertNotIn("example.test", json.dumps(value, ensure_ascii=False))

    def test_event_data_redacts_urls(self):
        value = safe_event_data({"url": "https://example.test/private", "nested": {"urls": ["https://example.test/two"]}})
        self.assertTrue(value["url"]["redacted"])
        self.assertTrue(value["nested"]["urls"][0]["redacted"])
        self.assertNotIn("example.test", json.dumps(value, ensure_ascii=False))

    def test_journal_persists_status_without_tool_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal(Path(directory) / "tasks.sqlite3")
            task_id = journal.start("Open the browser and find a shirt")
            journal.event(task_id, "tool_result", {"tool": "desktop_type", "arguments": {
                "text": "this must not be saved", "token": "private"}, "ok": True})
            journal.set_status(task_id, TASK_STATUS_COMPLETED, capabilities={"desktop_control"})
            task = journal.task(task_id)
            self.assertEqual(task["status"], TASK_STATUS_COMPLETED)
            self.assertEqual(task["capabilities"], ["desktop_control"])
            with closing(sqlite3.connect(journal.path)) as connection:
                stored = connection.execute("SELECT data_json FROM task_events WHERE kind = 'tool_result'").fetchone()[0]
            self.assertNotIn("this must not be saved", stored)
            self.assertNotIn("private\"", stored)

    def test_in_memory_journal_keeps_goal_and_events_off_disk(self):
        journal = InMemoryTaskJournal()
        task_id = journal.start("public browser prompt must remain in memory")
        journal.event(task_id, "tool_result", {"arguments": {"url": "https://example.test/private"}})
        journal.set_status(task_id, TASK_STATUS_COMPLETED, capabilities={"mcp:playwright"})

        task = journal.task(task_id)
        self.assertEqual(task["goal"], "public browser prompt must remain in memory")
        self.assertEqual(task["status"], TASK_STATUS_COMPLETED)
        self.assertEqual(task["capabilities"], ["mcp:playwright"])
        self.assertIsNone(journal.path)
        self.assertEqual(journal.recoverable(), [])
        evidence = journal.evidence_snapshot(task_id)
        self.assertTrue(evidence["ok"])
        self.assertNotIn("example.test", json.dumps(evidence, ensure_ascii=False))

    def test_failure_categories_are_stable(self):
        self.assertEqual(classify_failure("MCP timed out"), "transient_network")
        self.assertEqual(classify_failure("API HTTP 500: temporary gateway"), "transient_network")
        self.assertEqual(classify_failure("API HTTP 425: too early"), "transient_network")
        self.assertEqual(classify_failure("upstream service unavailable"), "transient_network")
        self.assertEqual(classify_failure("快速验证身份"), "human_verification")
        self.assertEqual(classify_failure("permission denied"), "permission_denied")
        self.assertEqual(classify_failure("API circuit open"), "provider_circuit_open")

    def test_e2e_failure_categories_are_normalized_before_generic_fallback(self):
        cases = {
            "empty_model_input": "empty_model_input",
            "API Key is not configured": "model_not_configured",
            "provider_timeout": "provider_timeout",
            "stale locator ref": "locator_failure",
            "page_state_failure": "page_state_failure",
            "desktop_focus_failure": "desktop_focus_failure",
            "Target window is not the foreground window": "desktop_focus_failure",
            "file_verification_failed": "file_verification_failure",
            "safety_boundary": "safety_boundary",
            "human_handoff_timeout": "human_handoff_timeout",
            "permission_blocked": "permission_blocked",
            "environment_not_ready": "environment_not_ready",
            "tool_failure": "tool_failure",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(classify_failure(value), expected)

    def test_checkpoint_is_structural_and_recoverable_requires_reauthorization(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal(Path(directory) / "tasks.sqlite3")
            task_id = journal.start("Open the browser")
            journal.set_status(task_id, "active", capabilities={"desktop_control"})
            journal.checkpoint(task_id, {"last_tool": "application_launch", "text": "private text must not persist"})
            item = journal.recoverable()
            self.assertEqual(len(item), 1)
            self.assertTrue(item[0]["requires_reauthorization"])
            self.assertEqual(item[0]["checkpoint"]["last_tool"], "application_launch")
            stored = json.dumps(item[0], ensure_ascii=False)
            self.assertNotIn("private text", stored)

    def test_contract_and_waiting_verification_are_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal(Path(directory) / "tasks.sqlite3")
            task_id = journal.start("打开浏览器搜索商品")
            journal.set_contract(task_id, {"version": 1, "requires_verification": True,
                                           "required_evidence_schemas": ["browser_structured_verification"]})
            journal.set_status(task_id, TASK_STATUS_WAITING_VERIFICATION, capabilities={"mcp:playwright"})
            item = journal.recoverable()
            self.assertEqual(item[0]["status"], TASK_STATUS_WAITING_VERIFICATION)
            self.assertTrue(item[0]["contract"]["requires_verification"])

    def test_paused_tasks_are_recoverable_and_evidence_is_structural(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal(Path(directory) / "tasks.sqlite3")
            task_id = journal.start("打开记事本并输入私密消息")
            journal.set_status(task_id, TASK_STATUS_PAUSED, capabilities={"desktop_control"})
            journal.checkpoint(task_id, {"last_tool": "desktop_type", "text": "private body"})
            journal.event(task_id, "workflow_node", {
                "tool": "desktop_type", "ok": True, "evidence": False,
                "evidence_schema": "desktop_state_delta", "arguments": {"text": "private body"},
            })
            item = journal.recoverable()
            self.assertEqual(item[0]["status"], TASK_STATUS_PAUSED)
            evidence = journal.evidence_snapshot(task_id)
            self.assertTrue(evidence["ok"])
            self.assertTrue(any(event["kind"] == "workflow_node" for event in evidence["events"]))
            self.assertNotIn("private body", json.dumps(evidence, ensure_ascii=False))

    def test_legacy_task_database_migrates_contract_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("""CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, failure_kind TEXT,
                    capability_json TEXT NOT NULL DEFAULT '[]', checkpoint_json TEXT NOT NULL DEFAULT '{}'
                )""")
                connection.execute("""CREATE TABLE task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    created_at REAL NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL
                )""")
            journal = TaskJournal(path)
            task_id = journal.start("迁移测试")
            journal.set_contract(task_id, {"version": 1, "requires_verification": True})
            self.assertTrue(journal.task(task_id)["contract"]["requires_verification"])


if __name__ == "__main__":
    unittest.main()
