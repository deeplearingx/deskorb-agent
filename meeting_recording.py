"""Background meeting recording and WhisperX integration.

This module deliberately has no Tkinter or DeskOrb imports.  The overlay can
start/stop it from the UI thread while audio capture and transcription stay in
daemon workers.  The bundled WhisperX source lives in the repository's
``whisperX-main`` folder, while its large ML dependency set stays isolated in
that folder's ignored ``.venv`` and runs in a subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Optional
import wave

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
from text_normalization import to_simplified_chinese


class RecordingState(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    TRANSCRIBING = "transcribing"


class MeetingRecordingError(RuntimeError):
    """A meeting recording or transcription operation could not start/finish."""


@dataclass(frozen=True)
class WhisperXResult:
    audio_path: Path
    json_path: Optional[Path]
    transcript: str
    text_path: Optional[Path] = None
    srt_path: Optional[Path] = None


@dataclass(frozen=True)
class WhisperXCommandBuilder:
    """Build an argv list for the WhisperX CLI without shell interpolation."""

    root: Path
    python_executable: Optional[Path] = None
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    language: Optional[str] = None
    diarize: bool = False
    no_align: bool = True
    timeout_seconds: int = 3_600

    def build(self, audio_path: Path, output_dir: Path) -> list[str]:
        executable = self.python_executable or Path(sys.executable)
        command = [
            str(executable),
            "-m",
            "whisperx",
            str(audio_path),
            "--model",
            self.model,
            "--device",
            self.device,
            "--compute_type",
            self.compute_type,
            "--output_dir",
            str(output_dir),
            "--output_format",
            "json",
            "--verbose",
            "False",
        ]
        if self.no_align:
            command.append("--no_align")
        if self.language:
            command.extend(("--language", self.language))
        if self.diarize:
            command.append("--diarize")
        return command


def _timestamp(seconds: object) -> str:
    try:
        total = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _simplify_whisperx_payload(payload: object) -> bool:
    """Normalize segment and aligned-word text while retaining WhisperX metadata."""
    if not isinstance(payload, dict):
        return False
    segments = payload.get("segments", [])
    if not isinstance(segments, list):
        return False
    changed = False
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        value = segment.get("text")
        if isinstance(value, str):
            converted = to_simplified_chinese(value)
            if converted != value:
                segment["text"] = converted
                changed = True
        words = segment.get("words")
        if not isinstance(words, list):
            continue
        for word in words:
            if not isinstance(word, dict):
                continue
            value = word.get("word")
            if not isinstance(value, str):
                continue
            converted = to_simplified_chinese(value)
            if converted != value:
                word["word"] = converted
                changed = True
    return changed

def parse_whisperx_json(path: Path) -> str:
    """Convert WhisperX JSON segments into a compact, readable transcript."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeetingRecordingError(f"Could not read WhisperX output: {path}") from exc
    segments = payload.get("segments", []) if isinstance(payload, dict) else []
    if _simplify_whisperx_payload(payload):
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    lines: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        speaker = str(segment.get("speaker") or "Speaker").strip()
        lines.append(f"[{_timestamp(segment.get('start'))}] {speaker}: {text}")
    return "\n".join(lines)


def _prepare_whisperx_environment(env: dict[str, str], root: Path, executable: Path) -> None:
    """Expose a bundled imageio-ffmpeg binary to WhisperX's hard-coded ffmpeg call."""
    configured = os.environ.get("DESKORB_AGENT_FFMPEG", "").strip()
    candidate: Optional[Path] = Path(configured).expanduser() if configured else None
    if candidate is not None and not candidate.is_file():
        candidate = None
    if candidate is None:
        search_items = (
            Path(executable).parent / "ffmpeg.exe",
            root / ".venv" / "Scripts" / "ffmpeg.exe",
            root / ".venv" / "Lib" / "site-packages" / "imageio_ffmpeg" / "binaries",
        )
        for item in search_items:
            if item.is_file():
                candidate = item
                break
            if item.is_dir():
                matches = sorted(item.glob("ffmpeg*.exe"))
                if matches:
                    candidate = matches[0]
                    break
    if candidate is None:
        return

    candidate_dir = candidate.parent
    if candidate.name.lower() not in {"ffmpeg.exe", "ffmpeg.com", "ffmpeg"}:
        try:
            runtime_dir = Path(tempfile.gettempdir()) / "deskorb-agent-ffmpeg"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            alias = runtime_dir / "ffmpeg.exe"
            if not alias.is_file() or alias.stat().st_size != candidate.stat().st_size:
                shutil.copyfile(candidate, alias)
            candidate_dir = runtime_dir
        except OSError:
            return
    old_path = env.get("PATH", "")
    env["PATH"] = str(candidate_dir) + (os.pathsep + old_path if old_path else "")

class WhisperXTranscriber:
    """Run a local WhisperX checkout in its own Python environment."""

    def __init__(self, builder: WhisperXCommandBuilder):
        self.builder = builder

    def transcribe(self, audio_path: Path) -> WhisperXResult:
        root = self.builder.root.expanduser().resolve()
        if not root.is_dir():
            raise MeetingRecordingError(f"WhisperX directory was not found: {root}")
        executable = self.builder.python_executable or Path(sys.executable)
        if not Path(executable).is_file():
            raise MeetingRecordingError(f"WhisperX Python was not found: {executable}")
        output_dir = audio_path.parent / "whisperx"
        output_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        old_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(root) + (os.pathsep + old_pythonpath if old_pythonpath else "")
        _prepare_whisperx_environment(env, root, Path(executable))
        command = self.builder.build(audio_path, output_dir)
        try:
            completed = subprocess.run(
                command,
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.builder.timeout_seconds,
                check=False,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise MeetingRecordingError("WhisperX transcription timed out.") from exc
        except OSError as exc:
            raise MeetingRecordingError(f"Could not start WhisperX: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "WhisperX failed").strip()
            raise MeetingRecordingError(detail[-1_500:])

        json_path = output_dir / f"{audio_path.stem}.json"
        if not json_path.is_file():
            candidates = sorted(output_dir.glob("*.json"), key=lambda item: item.stat().st_mtime)
            json_path = candidates[-1] if candidates else None
        if json_path is None:
            raise MeetingRecordingError("WhisperX finished without producing a JSON transcript.")
        text_path = output_dir / f"{audio_path.stem}.txt"
        srt_path = output_dir / f"{audio_path.stem}.srt"
        transcript = parse_whisperx_json(json_path)
        try:
            text_path.write_text(transcript, encoding="utf-8")
        except OSError:
            text_path = None
        return WhisperXResult(
            audio_path=audio_path,
            json_path=json_path,
            transcript=transcript,
            text_path=text_path if text_path.is_file() else None,
            srt_path=srt_path if srt_path.is_file() else None,
        )


class SoundCardCapture:
    """Capture microphone and Windows speaker loopback into one mono WAV file.

    SoundCard is imported lazily so users who do not install the optional audio
    dependency can still launch and use every existing DeskOrb feature.
    """

    def __init__(self, audio_path: Path, *, samplerate: int = 16_000, blocksize: int = 1_024):
        self.audio_path = Path(audio_path)
        self.samplerate = samplerate
        self.blocksize = blocksize
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._queues = []
        self._chunks: list[bytes] = []
        self._started = False
        self._error: Optional[BaseException] = None

    def start(self) -> None:
        try:
            import numpy as np
            import soundcard as sc
        except ImportError as exc:
            raise MeetingRecordingError(
                "Audio recording needs the optional 'soundcard' and 'numpy' packages."
            ) from exc

        self._np = np
        self._stop.clear()
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        sources = []
        try:
            sources.append(sc.default_microphone())
        except Exception:
            pass
        try:
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(speaker.name, include_loopback=True)
            if loopback is not None:
                sources.append(loopback)
        except Exception:
            pass
        if not sources:
            raise MeetingRecordingError("No usable microphone or speaker loopback was found.")

        import queue

        self._queues = [queue.Queue(maxsize=8) for _ in sources]
        for index, source in enumerate(sources):
            thread = threading.Thread(
                target=self._capture_source,
                args=(source, self._queues[index]),
                name=f"meeting-audio-{index}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
        self._mixer_thread = threading.Thread(
            target=self._mix_sources,
            name="meeting-audio-mixer",
            daemon=True,
        )
        self._mixer_thread.start()
        self._started = True

    def _capture_source(self, source, output_queue) -> None:
        try:
            with source.recorder(samplerate=self.samplerate, blocksize=self.blocksize) as recorder:
                while not self._stop.is_set():
                    chunk = recorder.record(numframes=self.blocksize)
                    try:
                        output_queue.put(chunk, timeout=0.5)
                    except Exception:
                        if self._stop.is_set():
                            return
        except BaseException as exc:
            self._error = exc

    def _mix_sources(self) -> None:
        np = self._np
        import queue

        while not self._stop.is_set() or any(not item.empty() for item in self._queues):
            blocks = []
            for item in self._queues:
                try:
                    blocks.append(item.get(timeout=0.2))
                except queue.Empty:
                    blocks.append(None)
            usable = [self._mono(block) for block in blocks if block is not None]
            if not usable:
                if self._stop.is_set():
                    break
                continue
            length = min(len(block) for block in usable)
            mixed = np.zeros(length, dtype=np.float32)
            for block in usable:
                mixed += block[:length] / max(1, len(usable))
            pcm = (np.clip(mixed, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            self._chunks.append(pcm)

    @staticmethod
    def _mono(data):
        if data is None:
            return None
        import numpy as np

        array = np.asarray(data, dtype=np.float32)
        if array.ndim == 1:
            return array
        return array.mean(axis=1)

    def stop(self) -> Path:
        if not self._started:
            raise MeetingRecordingError("Recording has not started.")
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._mixer_thread.join(timeout=2.0)
        if self._error is not None and not self._chunks:
            raise MeetingRecordingError(f"Audio capture failed: {self._error}")
        with wave.open(str(self.audio_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(self.samplerate)
            output.writeframes(b"".join(self._chunks))
        self._started = False
        return self.audio_path


class MeetingRecorder:
    """Coordinate non-blocking capture and post-recording transcription."""

    def __init__(
        self,
        output_dir: Path,
        *,
        capture_factory: Callable[[Path], object] = SoundCardCapture,
        transcriber: Optional[object] = None,
        on_event: Optional[Callable[[str, object], None]] = None,
    ):
        self.output_dir = Path(output_dir)
        self.capture_factory = capture_factory
        self.transcriber = transcriber
        self.on_event = on_event or (lambda _kind, _payload: None)
        self.state = RecordingState.IDLE
        self._sequence = 0
        self._capture = None
        self._lock = threading.RLock()
        self._worker: Optional[threading.Thread] = None

    def start(self) -> int:
        with self._lock:
            if self.state is not RecordingState.IDLE:
                raise RuntimeError(f"Cannot start recording while state is {self.state.value}.")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._sequence += 1
            while True:
                audio_path = self.output_dir / f"meeting-{self._sequence:03d}.wav"
                if not audio_path.exists():
                    break
                self._sequence += 1
            capture = self.capture_factory(audio_path)
            try:
                capture.start()
            except Exception:
                self.state = RecordingState.IDLE
                raise
            self._capture = capture
            self.state = RecordingState.RECORDING
            self._emit("recording_started", audio_path)
            return self._sequence

    def stop(self) -> None:
        with self._lock:
            if self.state is not RecordingState.RECORDING or self._capture is None:
                raise RuntimeError("No active meeting recording.")
            capture = self._capture
            self.state = RecordingState.FINALIZING
            self._worker = threading.Thread(
                target=self._finish,
                args=(capture,),
                name="meeting-finalize",
                daemon=True,
            )
            self._worker.start()

    def _finish(self, capture) -> None:
        try:
            audio_path = Path(capture.stop())
            with self._lock:
                self.state = RecordingState.TRANSCRIBING
            self._emit("transcribing", audio_path)
            if self.transcriber is None:
                raise MeetingRecordingError("No WhisperX transcriber is configured.")
            result = self.transcriber.transcribe(audio_path)
            with self._lock:
                self.state = RecordingState.IDLE
                self._capture = None
            self._emit("completed", result)
        except Exception as exc:
            with self._lock:
                self.state = RecordingState.IDLE
                self._capture = None
            self._emit("error", exc)

    def abort(self) -> None:
        """Stop capture during application shutdown without starting transcription."""
        with self._lock:
            capture = self._capture
            self._capture = None
            self.state = RecordingState.IDLE
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass

    def _emit(self, kind: str, payload) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:
            # Event consumers must never take down the audio worker.
            pass
def resolve_whisperx_python(root: Path, configured: Optional[Path] = None) -> Path:
    """Resolve the Python executable belonging to a local WhisperX checkout.

    The path is intentionally supplied by configuration or discovered relative to
    root. No developer/user-specific absolute path is committed to DeskOrb.
    """
    if configured:
        return Path(configured).expanduser()
    root = Path(root).expanduser()
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / "venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Keep the error actionable while still allowing callers/tests to inspect the
    # resolved default before the external environment has been created.
    return candidates[0]


