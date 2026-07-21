import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from workflow_records import WorkflowRecordRepository


class WorkflowRecordRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name, "records.db")
        self.repository = WorkflowRecordRepository(self.database_path)

    def tearDown(self):
        self.repository.close()
        self.directory.cleanup()

    def _create_record(self, due_date="2030-01-05"):
        return self.repository.create_record(
            workflow_id="meeting_minutes",
            markdown="# Minutes\n\nGenerated result.",
            structured_data={
                "summary": "Generated result",
                "action_items": [
                    {
                        "task": "Send recap",
                        "owner": "Ari",
                        "due_date": due_date,
                        "priority": "high",
                        "status": "open",
                    }
                ],
            },
            source_summaries=[
                {
                    "kind": "text",
                    "title": "Pasted notes",
                    "content": "This source body must never be persisted.",
                },
                {"kind": "file", "name": "meeting.md", "text": "also private"},
            ],
            created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )

    def test_creates_and_reads_record_without_persisting_source_bodies(self):
        record = self._create_record()
        loaded = self.repository.get_record(record.id)

        self.assertEqual(loaded.markdown, "# Minutes\n\nGenerated result.")
        self.assertEqual(loaded.source_summaries, [
            {"kind": "text", "title": "Pasted notes"},
            {"kind": "file", "name": "meeting.md"},
        ])
        connection = sqlite3.connect(self.database_path)
        try:
            dump = " ".join(str(value) for row in connection.execute("SELECT * FROM records") for value in row)
        finally:
            connection.close()
        self.assertNotIn("This source body", dump)
        self.assertNotIn("also private", dump)

    def test_preserves_only_word_document_name_and_character_count(self):
        record = self.repository.create_record(
            workflow_id="meeting_minutes", markdown="# Minutes", structured_data=None,
            source_summaries=[
                {
                    "kind": "word_document", "name": "Private proposal.docx", "character_count": "1234",
                    "path": "C:/sensitive/Private proposal.docx", "window_handle": "999", "content": "secret",
                }
            ],
        )

        loaded = self.repository.get_record(record.id)

        self.assertEqual(loaded.source_summaries, [
            {"kind": "word_document", "name": "Private proposal.docx", "character_count": "1234"},
        ])

    def test_lists_records_by_all_open_near_due_and_overdue_filters(self):
        open_record = self._create_record("2030-01-05")
        overdue_record = self._create_record("2029-12-31")
        completed_record = self._create_record("2030-01-02")
        self.repository.update_action_item(completed_record.action_items[0].id, status="completed")

        today = date(2030, 1, 1)
        self.assertEqual(
            [record.id for record in self.repository.list_records("all", today=today)],
            [completed_record.id, overdue_record.id, open_record.id],
        )
        self.assertEqual(
            [record.id for record in self.repository.list_records("open", today=today)],
            [overdue_record.id, open_record.id],
        )
        self.assertEqual(
            [record.id for record in self.repository.list_records("near_due", today=today)],
            [open_record.id],
        )
        self.assertEqual(
            [record.id for record in self.repository.list_records("overdue", today=today)],
            [overdue_record.id],
        )

    def test_updates_editable_action_item_fields_and_deletes_record(self):
        record = self._create_record()
        action = self.repository.update_action_item(
            record.action_items[0].id,
            task="Send revised recap",
            owner="Bo",
            due_date="2030-01-08",
            priority="low",
            status="completed",
        )

        self.assertEqual(
            (action.task, action.owner, action.due_date, action.priority, action.status),
            ("Send revised recap", "Bo", "2030-01-08", "low", "completed"),
        )
        self.repository.delete_record(record.id)
        self.assertIsNone(self.repository.get_record(record.id))

    def test_action_item_edits_are_persisted_in_the_record_structured_data(self):
        record = self._create_record()

        self.repository.update_action_item(
            record.action_items[0].id,
            task="Send corrected recap",
            owner="Chen",
            due_date="2030-01-09",
            priority="medium",
            status="in_progress",
        )
        reloaded = self.repository.get_record(record.id)

        self.assertEqual(
            reloaded.structured_data["action_items"],
            [{
                "task": "Send corrected recap",
                "owner": "Chen",
                "due_date": "2030-01-09",
                "priority": "medium",
                "status": "in_progress",
            }],
        )


if __name__ == "__main__":
    unittest.main()
