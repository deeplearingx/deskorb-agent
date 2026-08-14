import json
import unittest

from fla_ui_backend import FlaUIBackend
from desktop_uia import DesktopUIA


class FakeFlaUIBridge:
    def __init__(self, *, server_name="windows", available=True):
        self.server_name = server_name
        self.available = available
        self.calls = []
        self.snapshot_text = (
            '- window "Calculator" [ref=w1]\n'
            '  - button "Seven" [ref=w1e1]\n'
            '  - button "Equals" [ref=w1e2]\n'
            '  - text "Display is 0" [ref=w1e3]\n'
        )
        self.values = {"w1e4": ""}

    @property
    def available_servers(self):
        return (self.server_name,) if self.available else ()

    def schemas(self, server_names=None):
        if not self.available or (server_names and self.server_name not in set(server_names)):
            return []
        return [
            {"name": f"mcp_{self.server_name}_windows_snapshot"},
            {"name": f"mcp_{self.server_name}_windows_click"},
            {"name": f"mcp_{self.server_name}_windows_fill"},
            {"name": f"mcp_{self.server_name}_windows_get_text"},
            {"name": f"mcp_{self.server_name}_windows_focus"},
            {"name": f"mcp_{self.server_name}_windows_launch"},
            {"name": f"mcp_{self.server_name}_windows_batch"},
            {"name": f"mcp_{self.server_name}_windows_screenshot"},
        ]

    def call(self, exposed_name, arguments):
        self.calls.append((exposed_name, dict(arguments)))
        operation = exposed_name.split("mcp_windows_", 1)[-1].removeprefix("windows_")
        if operation == "snapshot":
            return {"ok": True, "content": [{"type": "text", "text": self.snapshot_text}]}
        if operation == "click":
            return {"ok": True, "content": [{"type": "text", "text": "Invoked Seven"}]}
        if operation == "fill":
            self.values[str(arguments["ref"])] = str(arguments["text"])
            return {"ok": True, "content": [{"type": "text", "text": "Filled"}]}
        if operation == "get_text":
            value = self.values.get(str(arguments["ref"]), "hello")
            return {"ok": True, "content": [{"type": "text", "text": json.dumps({"text": value})}]}
        if operation == "focus":
            return {"ok": True, "content": [{"type": "text", "text": "Focused"}]}
        if operation == "launch":
            return {"ok": True, "content": [{"type": "text", "text": "Window handle: w1"}]}
        raise AssertionError(f"unexpected raw FlaUI operation: {operation}")


class FlaUIBackendTests(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeFlaUIBridge()
        self.backend = FlaUIBackend(self.bridge, server_name="windows")
        self.backend.register_window_handle(42, "w1")

    def test_snapshot_maps_fla_ui_refs_to_short_lived_deskorb_controls(self):
        result = self.backend.observe_active_window(42)
        self.assertTrue(result["ok"])
        self.assertTrue(result["uia_observation_id"].startswith("uia-obs-"))
        self.assertEqual(result["window_handle"], 42)
        self.assertEqual(result["controls"][0]["name"], "Seven")
        self.assertEqual(result["controls"][0]["control_type"], "button")
        self.assertNotEqual(result["controls"][0]["control_id"], "w1e1")
        self.assertNotIn("windows_batch", " ".join(self.backend.internal_tool_names))

    def test_old_observation_and_control_are_rejected_after_action(self):
        observed = self.backend.observe_active_window(42)
        control = next(item for item in observed["controls"] if item["name"] == "Seven")
        missing_observation = self.backend.invoke(control["control_id"], 42)
        self.assertFalse(missing_observation["ok"])
        self.assertEqual(missing_observation["failure_kind"], "desktop_uia_stale_control")
        invoked = self.backend.invoke(control["control_id"], 42, observed["uia_observation_id"])
        self.assertTrue(invoked["ok"])
        stale = self.backend.invoke(control["control_id"], 42, observed["uia_observation_id"])
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["failure_kind"], "desktop_uia_stale_control")
        self.assertEqual(len([call for call in self.bridge.calls if call[0].endswith("windows_click")]), 1)

    def test_set_value_uses_fill_then_exact_readback(self):
        self.bridge.snapshot_text = (
            '- window "Notepad" [ref=w1]\n'
            '  - textbox "Editor" [ref=w1e4]\n'
        )
        observed = self.backend.observe_active_window(42)
        control = observed["controls"][0]
        result = self.backend.set_value(control["control_id"], 42, "hello", observed["uia_observation_id"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"]["kind"], "uia_value_readback")
        names = [name for name, _args in self.bridge.calls]
        self.assertIn("mcp_windows_windows_fill", names)
        self.assertIn("mcp_windows_windows_get_text", names)

    def test_missing_backend_fails_closed_and_disallows_coordinate_fallback(self):
        bridge = FakeFlaUIBridge(available=False)
        backend = FlaUIBackend(bridge, server_name="windows")
        observed = backend.observe_active_window(42)
        self.assertFalse(observed["ok"])
        self.assertEqual(observed["failure_kind"], "desktop_backend_unavailable")
        self.assertFalse(backend.coordinate_fallback_eligible("uia-obs-1"))

    def test_launch_returns_internal_window_handle_without_exposing_raw_operation(self):
        result = self.backend.launch_application("calculator")
        self.assertTrue(result["ok"])
        self.assertLess(result["window_handle"], 0)
        self.assertEqual(result["application"], "calculator")
        self.assertEqual(self.backend.application_process_name(result["window_handle"]), "calc.exe")
        self.assertNotIn("windows_batch", self.backend.internal_tool_names)
        self.assertNotIn("windows_screenshot", self.backend.internal_tool_names)

    def test_desktop_uia_delegates_to_fla_ui_and_fail_closed_coordinate_policy(self):
        observer = DesktopUIA(desktop_factory=None, semantic_backend=self.backend)
        observed = observer.observe_active_window(42)
        self.assertTrue(observer.available)
        self.assertTrue(observed["ok"])
        self.assertFalse(observer.coordinate_fallback_eligible(observed["uia_observation_id"]))

    def test_configured_fla_ui_failure_is_not_replaced_by_pywinauto(self):
        backend = FlaUIBackend(FakeFlaUIBridge(available=False), server_name="windows")
        observer = DesktopUIA(semantic_backend=backend)
        result = observer.observe_active_window(42)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "desktop_backend_unavailable")
        self.assertFalse(observer.coordinate_fallback_eligible("missing"))


if __name__ == "__main__":
    unittest.main()
