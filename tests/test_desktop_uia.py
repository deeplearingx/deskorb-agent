import unittest
from unittest.mock import patch

from desktop_uia import DesktopUIA


class Rect:
    left, top, right, bottom = 10, 20, 110, 50


class Wrapper:
    class Info:
        name = "Search"
        automation_id = "SearchBox"
        control_type = "Edit"

    element_info = Info()

    def __init__(self):
        self.invoked = False
        self.value = None

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def rectangle(self):
        return Rect()

    def invoke(self):
        self.invoked = True

    def set_edit_text(self, value):
        self.value = value

    def get_value(self):
        return self.value or ""


class DesktopUIATests(unittest.TestCase):
    def _observer(self):
        wrapper = Wrapper()

        class Window:
            def descendants(self):
                return [wrapper]

        class FakeDesktop:
            def __init__(self, backend):
                self.backend = backend

            def window(self, handle):
                self.handle = handle
                return Window()

        return DesktopUIA(desktop_factory=FakeDesktop), wrapper

    def test_observe_returns_semantic_control_and_invokes_it(self):
        observer, wrapper = self._observer()
        result = observer.observe_active_window(42)
        self.assertTrue(result["ok"])
        control = result["controls"][0]
        self.assertEqual(control["name"], "Search")
        self.assertEqual(control["automation_id"], "SearchBox")
        self.assertEqual(control["actions"], ["invoke", "set_value"])
        self.assertEqual(result["recommended_actions"][0]["control_id"], control["control_id"])
        self.assertEqual(result["recommended_actions"][0]["action"], "set_value")
        invoked = observer.invoke(control["control_id"], 42)
        self.assertTrue(invoked["ok"])
        self.assertTrue(wrapper.invoked)
        self.assertFalse(invoked["verified"])
        self.assertTrue(invoked["verification"]["requires_reobserve"])

    def test_invoke_verifies_a_toggle_state_change(self):
        class Toggle(Wrapper):
            class Info:
                name = "Remember"
                automation_id = "Remember"
                control_type = "CheckBox"
            element_info = Info()

            def __init__(self):
                super().__init__()
                self.checked = False

            def is_checked(self):
                return self.checked

            def invoke(self):
                self.invoked = True
                self.checked = not self.checked

        wrapper = Toggle()

        class Window:
            def descendants(self):
                return [wrapper]

        class FakeDesktop:
            def __init__(self, backend):
                self.backend = backend

            def window(self, handle):
                return Window()

        observer = DesktopUIA(desktop_factory=FakeDesktop)
        control = observer.observe_active_window(42)["controls"][0]
        result = observer.invoke(control["control_id"], 42)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"]["kind"], "uia_state_change")

    def test_set_value_rejects_foreground_window_changes(self):
        observer, wrapper = self._observer()
        control = observer.observe_active_window(42)["controls"][0]
        rejected = observer.set_value(control["control_id"], 77, "private text")
        self.assertFalse(rejected["ok"])
        self.assertIsNone(wrapper.value)

    def test_set_value_reports_readback_verification(self):
        observer, wrapper = self._observer()
        control = observer.observe_active_window(42)["controls"][0]
        result = observer.set_value(control["control_id"], 42, "hello")
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["verification"]["passed"])

    def test_missing_backend_explains_safe_fallback(self):
        observer = DesktopUIA(desktop_factory=None)
        # The test environment may have pywinauto installed, so override the
        # factory explicitly after construction.
        observer._desktop_factory = None
        result = observer.observe_active_window(42)
        self.assertFalse(result["ok"])
        self.assertIn("unavailable", result["error"].lower())

    def test_observation_reports_dialog_attention(self):
        class Dialog(Wrapper):
            class Info:
                name = "Confirm"
                automation_id = "ConfirmDialog"
                control_type = "Dialog"
            element_info = Info()

        dialog = Dialog()

        class Window:
            def descendants(self):
                return [dialog]

        class FakeDesktop:
            def __init__(self, backend):
                self.backend = backend

            def window(self, handle):
                return Window()

        result = DesktopUIA(desktop_factory=FakeDesktop).observe_active_window(42)
        self.assertTrue(result["requires_user_attention"])
        self.assertEqual(result["dialogs"][0]["control_type"], "Dialog")

    def test_observed_high_risk_profile_control_is_remembered_for_runtime_policy(self):
        class SendButton(Wrapper):
            class Info:
                name = "发送"
                automation_id = "Send"
                control_type = "Button"
            element_info = Info()

        button = SendButton()

        class Window:
            def descendants(self):
                return [button]

        class FakeDesktop:
            def __init__(self, backend):
                self.backend = backend

            def window(self, handle):
                return Window()

        observer = DesktopUIA(desktop_factory=FakeDesktop)
        with patch("desktop_uia.window_process_name", return_value="qq.exe"):
            result = observer.observe_active_window(42)
        control_id = result["controls"][0]["control_id"]
        self.assertTrue(any(item["risk_level"] == "high" for item in result["recommended_actions"]))
        self.assertTrue(observer.is_high_risk(control_id))

    def test_qq_message_delivery_requires_fresh_changed_tree_and_marker(self):
        class SendButton(Wrapper):
            class Info:
                name = "发送"
                automation_id = "Send"
                control_type = "Button"
            element_info = Info()

        class SentMarker(Wrapper):
            class Info:
                name = "已发送"
                automation_id = "DeliveryState"
                control_type = "Text"
            element_info = Info()

        button = SendButton()
        controls = [button]

        class Window:
            def descendants(self):
                return list(controls)

        class FakeDesktop:
            def __init__(self, backend):
                self.backend = backend

            def window(self, handle):
                return Window()

        observer = DesktopUIA(desktop_factory=FakeDesktop)
        with patch("desktop_uia.window_process_name", return_value="qqnt.exe"):
            before = observer.observe_active_window(42)
            self.assertFalse(before["verification"]["passed"])
            control_id = before["controls"][0]["control_id"]
            dispatched = observer.invoke(control_id, 42)
            self.assertTrue(dispatched["ok"])
            self.assertFalse(dispatched["verification"]["passed"])
            controls.append(SentMarker())
            after = observer.observe_active_window(42)
        self.assertTrue(after["verification"]["passed"])
        self.assertEqual(after["verification"]["kind"], "message_delivery")
        self.assertTrue(after["verification"]["state_changed"])


if __name__ == "__main__":
    unittest.main()
