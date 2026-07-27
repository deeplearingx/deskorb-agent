import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from powertoys_mcp import PowerToysDsc, PowerToysError, PowerToysMcpServer


class PowerToysMcpTests(unittest.TestCase):
    def _adapter(self):
        dsc = PowerToysDsc()
        dsc.executable = Path("C:/PowerToys/PowerToys.DSC.exe")
        dsc._modules = ["AlwaysOnTop", "Awake", "KeyboardManager"]
        return dsc

    def test_apply_merges_partial_properties_and_verifies_readback(self):
        dsc = self._adapter()
        before = {"settings": {"name": "Awake", "version": "1.0", "properties": {
            "keepDisplayOn": False, "mode": 0, "nested": {"existing": 1},
        }}}
        after = {"settings": {"name": "Awake", "version": "1.0", "properties": {
            "keepDisplayOn": True, "mode": 0, "nested": {"existing": 1, "new": 2},
        }}}
        dsc._run_json = Mock(side_effect=[before, {"_inDesiredState": False}, {"changed": True}, after])

        result = dsc.apply_settings("awake", {"keepDisplayOn": True, "nested": {"new": 2}})

        self.assertTrue(result["verified"])
        self.assertEqual(result["module"], "Awake")
        self.assertRegex(result["backup_id"], r"^[0-9A-F]{12}$")
        test_args = dsc._run_json.call_args_list[1].args
        candidate = __import__("json").loads(test_args[-1])
        self.assertTrue(candidate["settings"]["properties"]["keepDisplayOn"])
        self.assertEqual(candidate["settings"]["properties"]["nested"], {"existing": 1, "new": 2})

    def test_write_allowlist_rejects_keyboard_manager_before_set(self):
        dsc = self._adapter()
        dsc._run_json = Mock(return_value={"settings": {"name": "KeyboardManager", "version": "1.0", "properties": {}}})

        with self.assertRaises(PowerToysError):
            dsc.apply_settings("KeyboardManager", {"enabled": True})

        self.assertEqual(dsc._run_json.call_count, 1)  # only the read needed to prepare the candidate

    def test_mcp_exposes_safe_read_tools_and_two_mutation_tools(self):
        tools = {item["name"]: item for item in PowerToysMcpServer._tools()}
        self.assertIn("powertoys_get_settings", tools)
        self.assertIn("powertoys_apply_settings", tools)
        self.assertIn("powertoys_restore_backup", tools)
        self.assertIn("properties", tools["powertoys_apply_settings"]["inputSchema"]["properties"])

    def test_server_reports_known_tool_errors_as_mcp_content_errors(self):
        server = PowerToysMcpServer()
        server.dsc.status = Mock(side_effect=PowerToysError("not installed"))
        result = server._call("powertoys_status", {})
        self.assertFalse(result["ok"])
        self.assertIn("not installed", result["error"])

    def test_dsc_test_output_accepts_only_its_known_empty_array_trailer(self):
        dsc = self._adapter()
        completed = Mock(returncode=0, stdout='{"_inDesiredState":true}\n[]\n', stderr="")
        with patch("powertoys_mcp.subprocess.run", return_value=completed):
            self.assertEqual(dsc._run_json("test"), {"_inDesiredState": True})

    def test_profile_preflights_every_change_before_applying(self):
        dsc = self._adapter()
        before = {
            "Awake": {"settings": {"name": "Awake", "version": "1", "properties": {"keepDisplayOn": False}}},
            "AlwaysOnTop": {"settings": {"name": "AlwaysOnTop", "version": "1", "properties": {"enabled": False}}},
        }
        candidate = {
            "Awake": {"settings": {"name": "Awake", "version": "1", "properties": {"keepDisplayOn": True}}},
            "AlwaysOnTop": {"settings": {"name": "AlwaysOnTop", "version": "1", "properties": {"enabled": True}}},
        }
        dsc._candidate = Mock(side_effect=lambda module, props: (module, before[module], candidate[module]))
        dsc._run_json = Mock(side_effect=[{"_inDesiredState": False}, {"_inDesiredState": False}, {"set": "Awake"}, {"set": "AlwaysOnTop"}])
        dsc.get_settings = Mock(side_effect=[candidate["Awake"], candidate["AlwaysOnTop"]])

        result = dsc.apply_profile([
            {"module": "Awake", "properties": {"keepDisplayOn": True}},
            {"module": "AlwaysOnTop", "properties": {"enabled": True}},
        ])

        self.assertTrue(result["verified"])
        self.assertEqual([item["module"] for item in result["changes"]], ["Awake", "AlwaysOnTop"])
        self.assertEqual(dsc._run_json.call_args_list[0].args[0], "test")
        self.assertEqual(dsc._run_json.call_args_list[1].args[0], "test")
        self.assertEqual(dsc._run_json.call_args_list[2].args[0], "set")

    def test_profile_rolls_back_the_current_and_prior_change_after_failed_verification(self):
        dsc = self._adapter()
        before = {
            "Awake": {"settings": {"name": "Awake", "version": "1", "properties": {"keepDisplayOn": False}}},
            "AlwaysOnTop": {"settings": {"name": "AlwaysOnTop", "version": "1", "properties": {"enabled": False}}},
        }
        candidate = {
            "Awake": {"settings": {"name": "Awake", "version": "1", "properties": {"keepDisplayOn": True}}},
            "AlwaysOnTop": {"settings": {"name": "AlwaysOnTop", "version": "1", "properties": {"enabled": True}}},
        }
        dsc._candidate = Mock(side_effect=lambda module, props: (module, before[module], candidate[module]))
        dsc._run_json = Mock(side_effect=[
            {"_inDesiredState": False}, {"_inDesiredState": False},  # preflights
            {"set": "Awake"}, {"set": "AlwaysOnTop"},              # attempted writes
            {"restore": "AlwaysOnTop"}, {"restore": "Awake"},       # rollback writes
        ])
        dsc.get_settings = Mock(side_effect=[
            candidate["Awake"],
            {"settings": {"name": "AlwaysOnTop", "version": "1", "properties": {"enabled": False}}},
            before["AlwaysOnTop"], before["Awake"],
        ])

        with self.assertRaisesRegex(PowerToysError, "Automatic rollback restored: AlwaysOnTop, Awake"):
            dsc.apply_profile([
                {"module": "Awake", "properties": {"keepDisplayOn": True}},
                {"module": "AlwaysOnTop", "properties": {"enabled": True}},
            ])


if __name__ == "__main__":
    unittest.main()
