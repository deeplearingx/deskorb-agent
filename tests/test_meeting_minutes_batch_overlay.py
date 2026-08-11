import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from deskorb_agent import Overlay


class _BatchWorker:
    def __init__(self):
        self.transcripts = []

    def ask_meeting_minutes_batch(self, transcript):
        self.transcripts.append(transcript)


class MeetingMinutesBatchOverlayTests(unittest.TestCase):
    def test_transcript_completion_queues_batch_worker_with_raw_transcript(self):
        with TemporaryDirectory() as tmp:
            audio = Path(tmp) / "meeting-001.wav"
            transcript = Path(tmp) / "meeting-001.txt"
            audio.write_bytes(b"wav")
            transcript.write_text("[00:00] speaker: one", encoding="utf-8")
            result = SimpleNamespace(
                audio_path=audio,
                text_path=transcript,
                json_path=None,
                transcript="[00:00] speaker: one",
            )
            overlay = Overlay.__new__(Overlay)
            overlay.worker = _BatchWorker()
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

            self.assertEqual(overlay.worker.transcripts, [result.transcript])
            self.assertTrue(overlay._meeting_minutes_pending)


if __name__ == "__main__":
    unittest.main()
