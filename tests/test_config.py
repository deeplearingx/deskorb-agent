import unittest

from config import SHOW_IN_SCREEN_SHARE_DEFAULT, SYSTEM_APPEND


class VisualCompatibilityDefaultsTests(unittest.TestCase):
    def test_overlay_is_visible_in_screen_shares_by_default(self):
        self.assertTrue(SHOW_IN_SCREEN_SHARE_DEFAULT)

    def test_disk_office_workflows_prefer_officecli_over_python_libraries(self):
        prompt = SYSTEM_APPEND.lower()
        self.assertIn("officecli", prompt)
        self.assertIn("do not use python-docx", prompt)
        self.assertIn("python-pptx/openpyxl", prompt)


if __name__ == "__main__":
    unittest.main()
