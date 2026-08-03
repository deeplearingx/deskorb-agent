import unittest
from unittest.mock import Mock, patch

from deskorb_agent import Overlay


class LiveContextConsentTests(unittest.TestCase):
    class _Widget:
        instances = []

        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.text = kwargs.get("text", "")
            self.command = kwargs.get("command")
            self.state = "normal"
            self.__class__.instances.append(self)

        def pack(self, *args, **kwargs):
            return None

        def bind(self, *args, **kwargs):
            return None

        def configure(self, **kwargs):
            self.state = kwargs.get("state", self.state)

        def lift(self):
            return None

        def invoke(self):
            if self.command:
                return self.command()
            return None

    def _overlay(self, text="分析当前桌面和本地 DeskOrb 日志"):
        overlay = Overlay.__new__(Overlay)
        overlay.busy = False
        overlay.chat_word_attachment = None
        overlay._entry_text = Mock(return_value=text)
        overlay._live_context_consent = False
        overlay._live_context_card = None
        overlay._precaptured = ([{"path": "C:/private/current.png"}], 0)
        overlay.pending_shot = [{"path": "C:/private/pending.png"}]
        overlay.pending_images = []
        overlay.add_live_context_consent = Mock()
        overlay.capture = Mock()
        return overlay

    def test_diagnostic_text_requires_consent_before_reusing_a_capture(self):
        overlay = self._overlay()

        overlay._send_or_stop()

        overlay.add_live_context_consent.assert_called_once_with("分析当前桌面和本地 DeskOrb 日志")
        overlay.capture.assert_not_called()
        self.assertIsNone(overlay._precaptured)
        self.assertIsNone(overlay.pending_shot)

    def test_regular_action_does_not_trigger_privacy_card(self):
        overlay = self._overlay("打开浏览器并搜索天气")
        self.assertFalse(overlay._requires_live_context_consent(overlay._entry_text()))

    def test_explicit_consent_forces_one_active_window_capture(self):
        overlay = self._overlay()
        overlay._live_context_consent = True
        overlay.auto_shot = False
        overlay.pending_shot = None
        overlay._refresh_attach = Mock()
        overlay.entry = Mock()
        overlay._ph_active = False
        overlay.add_user = Mock()
        overlay._dispatch_turn = Mock()
        overlay._set_busy = Mock()
        overlay.capture.return_value = [{
            "path": "C:/private/active-window.png", "primary": True,
            "index": 1, "window": "Browser",
        }]

        overlay._send_or_stop()

        overlay.capture.assert_called_once_with(announce=False, window_only=True)
        overlay._dispatch_turn.assert_called_once()
        self.assertFalse(overlay._live_context_consent)

    def test_failed_consented_capture_does_not_send_diagnostic(self):
        overlay = self._overlay()
        overlay._live_context_consent = True
        overlay.auto_shot = False
        overlay.pending_shot = None
        overlay.pending_images = []
        overlay.add_err = Mock()
        overlay.capture.return_value = None
        overlay._dispatch_turn = Mock()

        overlay._send_or_stop()

        overlay.add_err.assert_called_once()
        overlay._dispatch_turn.assert_not_called()

    def test_consented_window_capture_does_not_fallback_to_full_screen(self):
        overlay = Overlay.__new__(Overlay)
        overlay.window_shot = False
        overlay._grab_window_shot = Mock(return_value=(None, None))
        overlay._grab_shots = Mock(return_value=([{"path": "C:/private/full.png"}], None))

        shots, error = overlay._grab_shots_scoped([], window_only=True)

        self.assertEqual(shots, [])
        self.assertIsNotNone(error)
        overlay._grab_shots.assert_not_called()

    def test_precapture_is_suppressed_while_typing_diagnostic_request(self):
        overlay = self._overlay()
        overlay.auto_shot = True
        overlay._capture_busy = False
        overlay._precapture_after = None
        overlay.root = Mock()

        overlay._precapture_soon()

        overlay.root.after.assert_not_called()

    def test_privacy_card_exposes_clickable_allow_and_cancel_buttons(self):
        overlay = Overlay.__new__(Overlay)
        overlay._live_context_card = None
        overlay._live_context_consent = False
        overlay.busy = False
        overlay._md_finalize = Mock()
        overlay._prune_chat = Mock()
        overlay._fwd_wheel = Mock()
        overlay.px = lambda value: value
        overlay.f_chip = object()
        overlay.f_small = object()
        overlay.chat = Mock()
        overlay.chat.yview.return_value = (0.0, 1.0)
        overlay.add_user = Mock()
        overlay._send_or_stop = Mock()
        self._Widget.instances = []

        with patch("deskorb_agent.tk.Frame", self._Widget), \
             patch("deskorb_agent.tk.Label", self._Widget), \
             patch("deskorb_agent.tk.Button", self._Widget):
            overlay.add_live_context_consent("分析当前桌面")

        buttons = {widget.text: widget for widget in self._Widget.instances
                   if widget.command is not None}
        self.assertIn("允许本次读取", buttons)
        self.assertIn("取消", buttons)
        buttons["允许本次读取"].invoke()
        self.assertTrue(overlay.add_user.called)
        self.assertTrue(overlay._send_or_stop.called)
        self.assertFalse(overlay._live_context_card)


if __name__ == "__main__":
    unittest.main()
