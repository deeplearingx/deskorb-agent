import unittest

from claude_overlay import _state_text


class StateTests(unittest.TestCase):
    def test_null_state_values_use_defaults(self):
        state = {"model": None, "url": "None", "proxy": " null "}
        self.assertEqual(_state_text(state, "model", "fallback-model"), "fallback-model")
        self.assertEqual(_state_text(state, "url", "https://fallback"), "https://fallback")
        self.assertEqual(_state_text(state, "proxy", ""), "")
