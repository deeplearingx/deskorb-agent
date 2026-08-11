import os
import unittest
from pathlib import Path


class ProjectWhisperXPathTests(unittest.TestCase):
    def test_default_whisperx_checkout_is_inside_the_deskorb_project(self):
        previous = os.environ.pop("DESKORB_AGENT_WHISPERX_ROOT", None)
        try:
            import config

            expected = Path(config.__file__).resolve().parent / "whisperX-main"
            self.assertEqual(config.WHISPERX_ROOT, expected)
            self.assertTrue(expected.is_dir())
        finally:
            if previous is not None:
                os.environ["DESKORB_AGENT_WHISPERX_ROOT"] = previous


if __name__ == "__main__":
    unittest.main()
