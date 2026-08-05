import unittest

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
        invoked = observer.invoke(control["control_id"], 42)
        self.assertTrue(invoked["ok"])
        self.assertTrue(wrapper.invoked)

    def test_set_value_rejects_foreground_window_changes(self):
        observer, wrapper = self._observer()
        control = observer.observe_active_window(42)["controls"][0]
        rejected = observer.set_value(control["control_id"], 77, "private text")
        self.assertFalse(rejected["ok"])
        self.assertIsNone(wrapper.value)

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


if __name__ == "__main__":
    unittest.main()
