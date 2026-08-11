import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from deskorb_agent import Overlay


class _Worker:
    def __init__(self):
        self.prompts = []

    def ask_meeting_minutes(self, prompt):
        self.prompts.append(prompt)


class MeetingMinutesOverlayTests(unittest.TestCase):
    def test_transcript_completion_queues_agent_and_saves_minutes(self):
        with TemporaryDirectory() as tmp:
            audio = Path(tmp) / "meeting-001.wav"
            transcript = Path(tmp) / "meeting-001.txt"
            audio.write_bytes(b"wav")
            transcript.write_text("[00:00] 周五发布", encoding="utf-8")
            result = SimpleNamespace(
                audio_path=audio,
                text_path=transcript,
                json_path=None,
                transcript="[00:00] 周五发布",
            )
            overlay = Overlay.__new__(Overlay)
            overlay.worker = _Worker()
            overlay._meeting_result = None
            overlay._meeting_transcript = ""
            overlay._meeting_minutes_parts = []
            overlay._meeting_minutes_pending = False
            overlay._meeting_minutes_audio_path = None
            messages = []
            overlay.add_sys = messages.append
            overlay.add_err = lambda value: messages.append("ERROR:" + str(value))
            overlay._refresh_meeting_button = lambda: None

            overlay._handle_meeting_event(("completed", result))
            overlay._handle_meeting_minutes_event(
                "delta",
                '{"title":"发布会","summary":"周五发布","decisions":["按计划发布"]}',
            )
            overlay._handle_meeting_minutes_event("turn_done", None)

            self.assertEqual(len(overlay.worker.prompts), 1)
            self.assertIn("周五发布", overlay.worker.prompts[0])
            self.assertTrue((Path(tmp) / "minutes" / "meeting-001.md").is_file())


if __name__ == "__main__":
    unittest.main()
