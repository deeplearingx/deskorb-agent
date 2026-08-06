import unittest
from unittest.mock import patch

import desktop_vm_preflight as preflight


class DesktopVMPreflightTests(unittest.TestCase):
    def test_non_windows_is_explicitly_unsupported(self):
        with patch.object(preflight.os, "name", "posix"):
            result = preflight.run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "unsupported_platform")

    def test_interactive_checks_fail_closed_when_any_capability_is_missing(self):
        checks = {
            "signed_in_user": True, "desktop_window": True,
            "foreground_window": False, "screen_size": True,
            "powershell7": True, "uia_provider": True,
            "uia_window_count": 0, "interactive_uia_desktop": False,
        }
        with patch.object(preflight.os, "name", "nt"), patch.object(preflight, "_windows_checks", return_value=checks):
            result = preflight.run()
        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["uia_window_count"], 0)

    def test_interactive_checks_require_all_boolean_capabilities(self):
        checks = {
            "signed_in_user": True, "desktop_window": True,
            "foreground_window": True, "screen_size": True,
            "powershell7": True, "uia_provider": True,
            "uia_window_count": 3, "interactive_uia_desktop": True,
        }
        with patch.object(preflight.os, "name", "nt"), patch.object(preflight, "_windows_checks", return_value=checks):
            result = preflight.run()
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
