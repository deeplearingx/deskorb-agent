import importlib.util
import unittest
from unittest.mock import Mock, patch

import quick_actions
from claude_overlay import Overlay


class QuickActionsModuleTests(unittest.TestCase):
    def test_quick_actions_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("quick_actions"))

    def test_actions_have_stable_ids_and_labels(self):
        actions = getattr(quick_actions, "QUICK_ACTIONS", ())
        self.assertEqual(
            [(action.id, action.label) for action in actions],
            [
                ("summarize", "总结"),
                ("extract_tasks", "提取待办"),
                ("draft_reply", "草拟回复"),
                ("explain", "解释内容"),
            ],
        )

    def test_lookup_returns_the_requested_action(self):
        get_action = getattr(quick_actions, "get_quick_action", None)
        self.assertTrue(callable(get_action))
        if not callable(get_action):
            return
        self.assertEqual(get_action("draft_reply").label, "草拟回复")
        with self.assertRaises(ValueError):
            get_action("missing")

    def test_prompt_includes_window_context_and_visible_content_limit(self):
        build_prompt = getattr(quick_actions, "build_quick_action_prompt", None)
        self.assertTrue(callable(build_prompt))
        if not callable(build_prompt):
            return
        prompt = build_prompt("extract_tasks", "Quarterly plan")
        self.assertIn("Quarterly plan", prompt)
        self.assertIn("visible", prompt.lower())
        self.assertIn("未说明", prompt)


class QuickActionDispatchTests(unittest.TestCase):
    def _overlay(self):
        overlay = Overlay.__new__(Overlay)
        overlay.busy = False
        overlay.worker = Mock()
        overlay.add_user = Mock()
        overlay.add_err = Mock()
        overlay.add_sys = Mock()
        overlay._set_busy = Mock()
        overlay._grab_shots = Mock()
        return overlay

    def test_dispatch_uses_only_active_window_capture(self):
        run_action = getattr(Overlay, "_run_quick_action", None)
        self.assertTrue(callable(run_action))
        if not callable(run_action):
            return

        overlay = self._overlay()
        overlay._grab_window_shot = Mock(return_value=([
            {"path": "C:/temp/window.png", "primary": True, "index": 1, "window": "Mail"}
        ], None))

        run_action(overlay, "summarize")

        overlay._grab_window_shot.assert_called_once_with()
        overlay._grab_shots.assert_not_called()
        overlay.worker.ask.assert_called_once()
        overlay.add_user.assert_called_once()
        self.assertIn("总结", overlay.add_user.call_args.args[0])
        self.assertIn("Mail", overlay.add_user.call_args.args[0])

    def test_capture_failure_does_not_submit_a_turn(self):
        overlay = self._overlay()
        overlay._grab_window_shot = Mock(return_value=(None, None))

        overlay._run_quick_action("explain")

        overlay.add_err.assert_called_once()
        overlay.worker.ask.assert_not_called()
        overlay.add_user.assert_not_called()

    def test_busy_overlay_does_not_capture_or_submit(self):
        overlay = self._overlay()
        overlay.busy = True
        overlay._grab_window_shot = Mock()

        overlay._run_quick_action("summarize")

        overlay._grab_window_shot.assert_not_called()
        overlay.worker.ask.assert_not_called()
        overlay.add_sys.assert_called_once()

    def test_codex_path_uses_attachment_prompt_without_inline_images(self):
        overlay = self._overlay()
        overlay._grab_window_shot = Mock(return_value=([
            {"path": "C:/temp/window.png", "primary": True, "index": 1, "window": "Document"}
        ], None))

        with patch("claude_overlay.IMAGE_INPUT", "path"):
            overlay._run_quick_action("extract_tasks")

        prompt, paths = overlay.worker.ask.call_args.args
        self.assertIn("[ATTACHMENTS]", prompt)
        self.assertIn("Document", prompt)
        self.assertEqual(paths, [])

    def test_inline_path_passes_the_window_image_to_the_worker(self):
        overlay = self._overlay()
        overlay._grab_window_shot = Mock(return_value=([
            {"path": "C:/temp/window.png", "primary": True, "index": 1, "window": "Chat"}
        ], None))

        with patch("claude_overlay.IMAGE_INPUT", "inline"):
            overlay._run_quick_action("draft_reply")

        prompt, paths = overlay.worker.ask.call_args.args
        self.assertIn("ACTIVE WINDOW", prompt)
        self.assertIn("Draft one concise", prompt)
        self.assertEqual(paths, ["C:/temp/window.png"])

    def test_busy_state_disables_all_quick_action_buttons(self):
        overlay = Overlay.__new__(Overlay)
        buttons = [Mock(), Mock(), Mock(), Mock()]
        overlay.quick_action_buttons = {str(index): button for index, button in enumerate(buttons)}
        overlay.busy = True

        overlay._refresh_quick_actions()

        for button in buttons:
            button.configure.assert_called_once_with(state="disabled")


if __name__ == "__main__":
    unittest.main()
