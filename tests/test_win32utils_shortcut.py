import os
import tempfile
import unittest
from unittest import mock

import win32utils


class EnsureTaskbarShortcutTests(unittest.TestCase):
    def test_missing_builder_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(win32utils.sys, "platform", "win32"), \
                 mock.patch.dict(os.environ, {"APPDATA": td}, clear=False), \
                 mock.patch.object(win32utils, "_pythonw_exe", return_value=r"C:\Python\pythonw.exe"):
                result = win32utils.ensure_taskbar_shortcut(
                    os.path.join(td, "deskorb_agent.py"), app_id="test.app")
        self.assertNotEqual(result, "error")
        self.assertIn("builder script missing", result)

    def test_builder_prefers_pwsh_and_creates_marker(self):
        with tempfile.TemporaryDirectory() as td:
            repo = os.path.join(td, "repo")
            os.makedirs(repo)
            script = os.path.join(repo, "deskorb_agent.py")
            with open(script, "w", encoding="utf-8"):
                pass
            with open(os.path.join(repo, "install-startmenu-shortcut.ps1"), "w", encoding="utf-8"):
                pass
            with mock.patch.object(win32utils.sys, "platform", "win32"), \
                 mock.patch.dict(os.environ, {"APPDATA": td}, clear=False), \
                 mock.patch.object(win32utils.os.path, "expanduser", return_value=td), \
                 mock.patch.object(win32utils, "_pythonw_exe", return_value=r"C:\Python\pythonw.exe"), \
                 mock.patch.object(win32utils.shutil, "which", side_effect=lambda name: {
                     "pwsh": r"C:\Program Files\PowerShell\7\pwsh.exe",
                     "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                 }.get(name)), \
                 mock.patch.object(win32utils.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"", stderr=b"")) as run:
                result = win32utils.ensure_taskbar_shortcut(script, app_id="test.app")
                self.assertEqual(result, "created")
                self.assertEqual(run.call_args.args[0][0], r"C:\Program Files\PowerShell\7\pwsh.exe")
            marker = os.path.join(td, ".claude-overlay", "startmenu_shortcut.json")
            self.assertTrue(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()
