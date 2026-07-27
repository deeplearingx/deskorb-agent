import unittest
from unittest.mock import Mock, patch

from deskorb_agent import Overlay
from word_sources import WordMaterial


class ChatWordAttachmentTests(unittest.TestCase):
    def _overlay(self):
        overlay = Overlay.__new__(Overlay)
        overlay.busy = False
        overlay._chat_word_read_request = None
        overlay._chat_word_read_sequence = 0
        overlay._chat_word_read_timeout_after = None
        overlay.chat_word_attachment = None
        overlay.add_err = Mock()
        overlay.add_user = Mock()
        overlay._dispatch_turn = Mock()
        overlay._refresh_chat_word_attachment = Mock()
        overlay._refresh_send = Mock()
        overlay.busy_lbl = Mock()
        return overlay

    def test_word_attachment_is_sent_once_without_images_or_chat_display_leak(self):
        overlay = self._overlay()
        overlay.chat_word_attachment = WordMaterial(
            document_id="word-window:101", name="Project brief.docx", text="Secret full document.",
            character_count=21, has_unsaved_changes=True, window_title="Project brief.docx",
        )

        overlay._send_chat_word_attachment("Summarize the document risks.")

        prompt, shots, images = overlay._dispatch_turn.call_args.args
        self.assertIn("Secret full document.", prompt)
        self.assertIn("Summarize the document risks.", prompt)
        self.assertEqual(shots, [])
        self.assertEqual(images, [])
        self.assertTrue(overlay._dispatch_turn.call_args.kwargs["ephemeral"])
        self.assertIsNone(overlay.chat_word_attachment)
        display = overlay.add_user.call_args.args[0]
        self.assertIn("Project brief.docx", display)
        self.assertNotIn("Secret full document.", display)

    def test_chat_word_read_ignores_a_late_result(self):
        overlay = self._overlay()
        material = WordMaterial(
            document_id="word-window:101", name="Late.docx", text="Late draft.",
            character_count=11, has_unsaved_changes=False, window_title="Late.docx",
        )

        overlay._finish_chat_word_read(7, material=material)

        self.assertIsNone(overlay.chat_word_attachment)

    def test_failed_replacement_clears_previous_word_attachment(self):
        overlay = self._overlay()
        overlay._chat_word_read_request = 7
        overlay.chat_word_attachment = WordMaterial(
            document_id="word-window:101", name="Old.docx", text="Old private text.",
            character_count=17, has_unsaved_changes=False, window_title="Old.docx",
        )

        overlay._finish_chat_word_read(7, error=RuntimeError("Word closed"))

        self.assertIsNone(overlay.chat_word_attachment)

    def test_send_with_word_attachment_skips_automatic_screen_capture(self):
        overlay = self._overlay()
        overlay.chat_word_attachment = WordMaterial(
            document_id="word-window:101", name="Project brief.docx", text="Private text.",
            character_count=13, has_unsaved_changes=False, window_title="Project brief.docx",
        )
        overlay._entry_text = Mock(return_value="What are the risks?")
        overlay._send_chat_word_attachment = Mock()
        overlay.capture = Mock()
        overlay.auto_shot = True
        overlay._precaptured = ([{"path": "C:/temp/window.png"}], 0)
        overlay.entry = Mock()

        overlay._send_or_stop()

        overlay.capture.assert_not_called()
        overlay._send_chat_word_attachment.assert_called_once_with("What are the risks?")

    def test_window_style_uses_standard_decorations_by_default(self):
        overlay = Overlay.__new__(Overlay)
        overlay.root = Mock()

        with patch("deskorb_agent.FRAMELESS_WINDOW", False):
            overlay._apply_window_style()

        overlay.root.overrideredirect.assert_called_once_with(False)

    def test_standard_window_style_does_not_apply_custom_region(self):
        overlay = Overlay.__new__(Overlay)
        overlay.root = Mock()

        with patch("deskorb_agent.CUSTOM_WINDOW_REGION", False):
            overlay._apply_region()

        overlay.root.update_idletasks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
