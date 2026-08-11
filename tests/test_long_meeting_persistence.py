import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from meeting_minutes import save_minutes


class LongMeetingPersistenceTests(unittest.TestCase):
    def test_saved_minutes_records_batch_metadata(self):
        with TemporaryDirectory() as tmp:
            saved = save_minutes(
                Path(tmp),
                "meeting",
                {"title": "Demo", "summary": "merged"},
                chunk_count=3,
                summary_mode="hierarchical",
            )

            payload = json.loads(saved.json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["chunk_count"], 3)
            self.assertEqual(payload["summary_mode"], "hierarchical")


if __name__ == "__main__":
    unittest.main()
