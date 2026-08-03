import unittest
from unittest.mock import Mock, patch

from deskorb_agent import Overlay
from office_sources import OfficeSnapshot, OfficeTarget


class ChatOfficeAttachmentTests(unittest.TestCase):
    def _overlay(self):
        overlay = Overlay.__new__(Overlay)
        overlay.chat_office_snapshot = None
        overlay._office_plan_active = False
        overlay._office_plan_raw = []
        overlay._pending_office_plan = None
        overlay._office_apply_active = False
        overlay.add_user = Mock()
        overlay.add_err = Mock()
        overlay.add_delta = Mock()
        overlay.add_sys = Mock()
        overlay._set_busy = Mock()
        overlay.worker = Mock()
        return overlay

    def test_office_attachment_uses_ephemeral_plan_without_chat_content_leak(self):
        overlay = self._overlay()
        overlay.chat_office_snapshot = OfficeSnapshot(
            kind="excel", expected_root=202, identity="excel:202:Plan.xlsx", name="Plan.xlsx",
            rendered_text="[Budget!C3] value=120 formula='=SUM(C1:C2)'", fingerprint="fingerprint",
            targets=(OfficeTarget("Budget!C3", "Budget!C3", 120, "=SUM(C1:C2)"),),
            has_unsaved_changes=False,
        )

        overlay._send_chat_office_attachment("Update the total.")

        prompt = overlay.worker.ask_office_plan.call_args.args[0]
        self.assertIn("Budget!C3", prompt)
        self.assertIn("Update the total.", prompt)
        self.assertIn('"plan": null', prompt)
        self.assertIn("word_replace_text", prompt)
        self.assertIn("excel_set_cell", prompt)
        self.assertTrue(overlay._office_plan_active)
        display = overlay.add_user.call_args.args[0]
        self.assertIn("Plan.xlsx", display)
        self.assertNotIn("Budget!C3", display)

    def test_office_plan_delta_is_buffered_not_rendered(self):
        overlay = self._overlay()
        overlay._office_plan_active = True

        overlay._handle("delta", '{"answer":"private"}')

        self.assertEqual(overlay._office_plan_raw, ['{"answer":"private"}'])
        overlay.add_delta.assert_not_called()

    def test_office_preview_places_actions_below_long_preview_text(self):
        from office_edits import OfficeEditPlan, WordTextEdit

        overlay = self._overlay()
        overlay.chat = Mock()
        overlay.px = lambda value: value
        overlay.f_small = "small"
        plan = OfficeEditPlan("word", "fingerprint", (
            WordTextEdit("paragraph:3", "A long original paragraph", "A long replacement paragraph"),
        ))

        with patch("deskorb_agent.tk.Frame", side_effect=[Mock(), Mock()]), \
                patch("deskorb_agent.tk.Label") as label, \
                patch("deskorb_agent.tk.Button"):
            overlay._add_office_plan_actions(plan)

        label.return_value.pack.assert_called_once_with(side="top", fill="x")


if __name__ == "__main__":
    unittest.main()
