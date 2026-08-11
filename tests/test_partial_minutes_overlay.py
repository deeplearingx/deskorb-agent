import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from deskorb_agent import Overlay


class PartialMinutesOverlayTests(unittest.TestCase):
    def test_batch_error_saves_partial_summaries(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "meeting.wav"
            transcript = root / "meeting.txt"
            audio.write_bytes(b"wav")
            transcript.write_text("transcript", encoding="utf-8")

            overlay = Overlay.__new__(Overlay)
            overlay._meeting_minutes_pending = True
            overlay._meeting_minutes_audio_path = audio
            overlay._meeting_result = SimpleNamespace(text_path=transcript)
            messages = []
            overlay.add_err = messages.append
            overlay.add_sys = messages.append

            overlay._handle_meeting_minutes_event(
                "error",
                {
                    "stage": "merge",
                    "chunk_count": 2,
                    "partials": [{"summary": "part one"}],
                    "message": "provider unavailable",
                },
            )

            path = root / "minutes" / "meeting.partial.json"
            self.assertTrue(path.is_file())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["partial_summaries"][0]["summary"], "part one")
            self.assertFalse(overlay._meeting_minutes_pending)
            self.assertTrue(any("provider unavailable" in str(item) for item in messages))


if __name__ == "__main__":
    unittest.main()
