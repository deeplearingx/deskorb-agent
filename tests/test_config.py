import unittest

from config import SHOW_IN_SCREEN_SHARE_DEFAULT


class VisualCompatibilityDefaultsTests(unittest.TestCase):
    def test_overlay_is_visible_in_screen_shares_by_default(self):
        self.assertTrue(SHOW_IN_SCREEN_SHARE_DEFAULT)


if __name__ == "__main__":
    unittest.main()

