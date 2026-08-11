import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from meeting_recording import resolve_whisperx_python


class WhisperXPathTests(unittest.TestCase):
    def test_resolves_whisperx_venv_without_hardcoding_a_user_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / ".venv" / "Scripts" / "python.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"python")

            self.assertEqual(resolve_whisperx_python(root), executable)


if __name__ == "__main__":
    unittest.main()
