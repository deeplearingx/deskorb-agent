import unittest
from pathlib import Path


class MeetingButtonBindingTests(unittest.TestCase):
    def test_record_label_has_a_left_click_binding(self):
        source = (Path(__file__).resolve().parents[1] / "deskorb_agent.py").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'self.meeting_button.bind("<Button-1>", lambda _event: self._toggle_meeting_recording())',
            source,
        )


if __name__ == "__main__":
    unittest.main()
