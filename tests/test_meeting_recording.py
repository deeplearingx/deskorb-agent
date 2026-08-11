import json
import os
import subprocess
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from meeting_recording import (
    MeetingRecorder,
    RecordingState,
    WhisperXCommandBuilder,
    WhisperXResult,
    WhisperXTranscriber,
    parse_whisperx_json,
)


class WhisperXCommandBuilderTests(unittest.TestCase):
    def test_builds_local_whisperx_command_without_shell_interpolation(self):
        builder = WhisperXCommandBuilder(
            root=Path(r"C:\path\to\whisperX-main"),
            python_executable=Path(r"C:\whisperx-venv\Scripts\python.exe"),
            model="small",
            device="cpu",
            compute_type="int8",
            language="zh",
            diarize=True,
        )

        command = builder.build(Path(r"C:\recordings\meeting 01.wav"), Path(r"C:\recordings\out"))

        self.assertEqual(command[:3], [
            r"C:\whisperx-venv\Scripts\python.exe",
            "-m",
            "whisperx",
        ])
        self.assertIn(r"C:\recordings\meeting 01.wav", command)
        self.assertIn("--output_format", command)
        self.assertIn("--no_align", command)
        self.assertIn("json", command)
        self.assertIn("--diarize", command)
        self.assertIn("--language", command)
        self.assertIn("zh", command)


    @unittest.skipUnless(os.name == "nt", "Windows-only console behavior")
    @patch("meeting_recording.subprocess.run")
    def test_transcription_hides_windows_console_window(self, run):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "whisperX-main"
            root.mkdir()
            audio = Path(tmp) / "meeting-001.wav"
            audio.write_bytes(b"wav")
            output_dir = audio.parent / "whisperx"
            output_dir.mkdir()
            (output_dir / "meeting-001.json").write_text(
                json.dumps({"segments": [{"start": 0, "text": "hello"}]}),
                encoding="utf-8",
            )
            python_executable = Path(tmp) / "python.exe"
            python_executable.write_bytes(b"python")
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            transcriber = WhisperXTranscriber(
                WhisperXCommandBuilder(root=root, python_executable=python_executable)
            )
            transcriber.transcribe(audio)

        expected = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        self.assertEqual(run.call_args.kwargs["creationflags"], expected)
class WhisperXJsonTests(unittest.TestCase):
    def test_parses_speaker_segments_into_readable_transcript(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting.json"
            path.write_text(json.dumps({
                "segments": [
                    {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.2, "text": " Hello"},
                    {"speaker": "SPEAKER_01", "start": 1.2, "end": 2.8, "text": "World"},
                ]
            }), encoding="utf-8")

            result = parse_whisperx_json(path)

        self.assertEqual(result, "[00:00] SPEAKER_00: Hello\n[00:01] SPEAKER_01: World")


class _FakeCapture:
    def __init__(self, audio_path):
        self.audio_path = Path(audio_path)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.audio_path.write_bytes(b"wav")
        return self.audio_path


class _FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio_path):
        self.calls.append(Path(audio_path))
        return WhisperXResult(
            audio_path=Path(audio_path),
            json_path=None,
            transcript="meeting text",
        )


class MeetingRecorderTests(unittest.TestCase):
    def test_stop_transcribes_in_background_and_returns_to_idle(self):
        with TemporaryDirectory() as tmp:
            events = []
            completed = threading.Event()
            transcriber = _FakeTranscriber()

            def on_event(kind, payload):
                events.append((kind, payload))
                if kind == "completed":
                    completed.set()

            recorder = MeetingRecorder(
                output_dir=Path(tmp),
                capture_factory=_FakeCapture,
                transcriber=transcriber,
                on_event=on_event,
            )

            recorder.start()
            self.assertEqual(recorder.state, RecordingState.RECORDING)
            recorder.stop()

            self.assertTrue(completed.wait(2.0))
            self.assertEqual(recorder.state, RecordingState.IDLE)
            self.assertEqual(transcriber.calls, [Path(tmp) / "meeting-001.wav"])
            self.assertEqual([kind for kind, _ in events], [
                "recording_started",
                "transcribing",
                "completed",
            ])

    def test_cannot_start_a_second_recording_while_transcribing(self):
        gate = threading.Event()

        class SlowTranscriber(_FakeTranscriber):
            def transcribe(self, audio_path):
                gate.wait(2.0)
                return super().transcribe(audio_path)

        with TemporaryDirectory() as tmp:
            recorder = MeetingRecorder(
                output_dir=Path(tmp),
                capture_factory=_FakeCapture,
                transcriber=SlowTranscriber(),
            )
            recorder.start()
            recorder.stop()
            time.sleep(0.05)
            with self.assertRaises(RuntimeError):
                recorder.start()
            gate.set()


if __name__ == "__main__":
    unittest.main()
