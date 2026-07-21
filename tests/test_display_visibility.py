import unittest

import config


class DisplayVisibilityTests(unittest.TestCase):
    def test_overlay_is_visible_in_screen_capture_by_default(self):
        self.assertTrue(config.SHOW_IN_SCREEN_SHARE_DEFAULT)


if __name__ == "__main__":
    unittest.main()
