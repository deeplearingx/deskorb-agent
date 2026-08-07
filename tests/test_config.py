import os
import unittest
from unittest.mock import patch

import config
from config import SHOW_IN_SCREEN_SHARE_DEFAULT, SYSTEM_APPEND


class VisualCompatibilityDefaultsTests(unittest.TestCase):
    def test_overlay_is_visible_in_screen_shares_by_default(self):
        self.assertTrue(SHOW_IN_SCREEN_SHARE_DEFAULT)

    def test_disk_office_workflows_prefer_officecli_over_python_libraries(self):
        prompt = SYSTEM_APPEND.lower()
        self.assertIn("officecli", prompt)
        self.assertIn("do not use python-docx", prompt)
        self.assertIn("python-pptx/openpyxl", prompt)

    def test_agent_tool_round_limit_has_safe_default_and_bounds(self):
        self.assertEqual(config.API_MAX_TOOL_ROUNDS, 100)
        with patch.dict(os.environ, {"DESKORB_AGENT_MAX_TOOL_ROUNDS": "10"}):
            self.assertEqual(config._env_int("DESKORB_AGENT_MAX_TOOL_ROUNDS", 100, 20, 500), 20)
        with patch.dict(os.environ, {"DESKORB_AGENT_MAX_TOOL_ROUNDS": "9999"}):
            self.assertEqual(config._env_int("DESKORB_AGENT_MAX_TOOL_ROUNDS", 100, 20, 500), 500)


if __name__ == "__main__":
    unittest.main()
