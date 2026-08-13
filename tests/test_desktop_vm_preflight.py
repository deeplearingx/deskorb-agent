import unittest
import sys
import types
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

    def test_deferred_target_mode_allows_fixture_to_be_started_after_host_preflight(self):
        checks = {
            "signed_in_user": True, "desktop_window": True,
            "foreground_window": False, "screen_size": True,
            "powershell7": True, "uia_provider": True,
            "uia_window_count": 0, "interactive_uia_desktop": False,
        }
        with patch.object(preflight.os, "name", "nt"), patch.object(preflight, "_windows_checks", return_value=checks):
            strict = preflight.run()
            deferred = preflight.run(require_foreground=False)
        self.assertFalse(strict["ok"])
        self.assertTrue(deferred["ok"])
        self.assertFalse(deferred["checks"]["interactive_uia_desktop"])

    def test_uia_probe_targets_foreground_window_without_enumerating_desktop(self):
        calls = []

        class FakeWindow:
            def exists(self, timeout=0):
                calls.append(("exists", timeout))
                return True

        class FakeDesktop:
            def __init__(self, backend):
                calls.append(("desktop", backend))

            def windows(self):
                raise AssertionError("the preflight must not enumerate every desktop window")

            def window(self, **kwargs):
                calls.append(("window", kwargs))
                return FakeWindow()

        fake_pywinauto = types.SimpleNamespace(Desktop=FakeDesktop)
        with patch.dict(sys.modules, {"pywinauto": fake_pywinauto}):
            result = preflight._uia_foreground_check(1234)

        self.assertEqual(result, (True, True))
        self.assertIn(("window", {"handle": 1234}), calls)
        self.assertIn(("exists", 2), calls)


if __name__ == "__main__":
    unittest.main()
