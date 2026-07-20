import unittest

from deskorb_agent import _approval_parts, _state_text


class StateTests(unittest.TestCase):
    def test_null_state_values_use_defaults(self):
        state = {"model": None, "url": "None", "proxy": " null "}
        self.assertEqual(_state_text(state, "model", "fallback-model"), "fallback-model")
        self.assertEqual(_state_text(state, "url", "https://fallback"), "https://fallback")
        self.assertEqual(_state_text(state, "proxy", ""), "")

    def test_approval_parser_extracts_summary_and_token(self):
        value = ("⚠ Confirmation required: Launch application: edge\n"
                 "Reply exactly: 确认 A1B2C3\nReply 取消 to stop.")
        self.assertEqual(_approval_parts(value), ("Launch application: edge", "A1B2C3"))
        self.assertIsNone(_approval_parts("ordinary system message"))

    def test_approval_parser_accepts_multiline_task_summary(self):
        value = ("⚠ Confirmation required: Authorize task: [Attached: screen]\n\n打开 QQ\n"
                 "Reply exactly: 确认 D3AE3F\nReply 取消 to stop.")
        self.assertEqual(_approval_parts(value),
                         ("Authorize task: [Attached: screen]\n\n打开 QQ", "D3AE3F"))
