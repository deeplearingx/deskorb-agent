import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from meeting_minutes import save_partial_minutes


class PartialMinutesPersistenceTests(unittest.TestCase):
    def test_partial_summaries_are_saved_for_retry_diagnostics(self):
        with TemporaryDirectory() as tmp:
            path = save_partial_minutes(
                Path(tmp),
                "meeting",
                [{"summary": "part one"}],
                stage="merge",
                chunk_count=2,
            )

            self.assertEqual(path.name, "meeting.partial.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["stage"], "merge")
            self.assertEqual(payload["chunk_count"], 2)
            self.assertEqual(payload["partial_summaries"][0]["summary"], "part one")


if __name__ == "__main__":
    unittest.main()
