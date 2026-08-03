import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from task_runtime import (TASK_STATUS_COMPLETED, TaskJournal, TaskLease, classify_failure,
                          safe_event_data)


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
        self.assertEqual(value["url"], "https://example.test/a")

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

    def test_failure_categories_are_stable(self):
        self.assertEqual(classify_failure("MCP timed out"), "transient_network")
        self.assertEqual(classify_failure("快速验证身份"), "human_verification")
        self.assertEqual(classify_failure("permission denied"), "permission_denied")

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


if __name__ == "__main__":
    unittest.main()
