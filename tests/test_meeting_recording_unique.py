import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from meeting_recording import MeetingRecorder, RecordingState


class _Capture:
    def __init__(self, audio_path):
        self.audio_path = Path(audio_path)

    def start(self):
        return None

    def stop(self):
        self.audio_path.write_bytes(b"wav")
        return self.audio_path


class MeetingFilenameTests(unittest.TestCase):
    def test_does_not_overwrite_an_existing_recording_after_restart(self):
        with TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "meeting-001.wav").write_bytes(b"previous")
            recorder = MeetingRecorder(output, capture_factory=_Capture)

            recorder.start()

            self.assertEqual(recorder.state, RecordingState.RECORDING)
            self.assertEqual(recorder._capture.audio_path.name, "meeting-002.wav")
            recorder.abort()


if __name__ == "__main__":
    unittest.main()
