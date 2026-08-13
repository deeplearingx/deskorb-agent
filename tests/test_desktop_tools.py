import unittest
import time
from unittest.mock import Mock, patch

from desktop_tools import DesktopSnapshot, DesktopTools, WindowSnapshot


class FakeUser32:
    def __init__(self):
        self.key_events = []
        self.mouse_events = []
        self.send_count = 0
        self.send_result = None
        self.cursor_positions = []
        self.foreground = 0

    def SendInput(self, count, inputs, size):
        self.send_count = count
        return count if self.send_result is None else self.send_result

    def keybd_event(self, vk, scan, flags, extra):
        self.key_events.append((vk, flags))

    def SetCursorPos(self, x, y):
        self.cursor_positions.append((x, y))
        return 1

    def mouse_event(self, *args):
        self.mouse_events.append(args)

    def GetForegroundWindow(self):
        return self.foreground

    def GetSystemMetrics(self, index):
        return {76: 0, 77: 0, 78: 1920, 79: 1080}.get(index, 0)

    def IsWindow(self, hwnd):
        return hwnd == 101

    def ShowWindow(self, hwnd, command):
        self.last_show = (hwnd, command)

    def SetForegroundWindow(self, hwnd):
        self.foreground = hwnd

    def GetWindowRect(self, hwnd, rect_ptr):
        rect = rect_ptr._obj
        rect.left, rect.top, rect.right, rect.bottom = 20, 30, 620, 430
        return 1


class DesktopToolsTests(unittest.TestCase):
    def test_invalid_snapshot_refuses_click_without_moving_mouse(self):
        tools = DesktopTools()
        result = tools.click("NOT_A_SNAPSHOT", 0, 0, "left")
        self.assertFalse(result["ok"])
        self.assertIn("snapshot", result["error"].lower())

    def test_state_snapshot_has_short_lived_identifier(self):
        result = DesktopTools().capture_state()
        if result["ok"]:
            self.assertTrue(result["snapshot_id"])
            self.assertIn("cursor", result)
        else:
            self.assertIn("Windows", result["error"])

    def ready_tools(self):
        tools = DesktopTools()
        tools.user32 = FakeUser32()
        tools.snapshot = DesktopSnapshot("FRESH", time.monotonic(), 0, 0, 0, "target", "digest")
        return tools

    def test_unicode_typing_uses_paired_input_events(self):
        tools = self.ready_tools()
        result = tools.type_text("FRESH", "A中")
        self.assertTrue(result["ok"])
        self.assertEqual(tools.user32.send_count, 4)

    def test_partial_unicode_input_reports_failure(self):
        tools = self.ready_tools()
        tools.user32.send_result = 1
        result = tools.type_text("FRESH", "A")
        self.assertFalse(result["ok"])
        self.assertEqual(result["events_sent"], 1)
        self.assertIn("part", result["error"].lower())

    def test_click_supports_double_middle_click_inside_virtual_desktop(self):
        tools = self.ready_tools()
        result = tools.click("FRESH", 500, 300, "middle", 2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["clicked"]["count"], 2)
        self.assertEqual(tools.user32.cursor_positions, [(500, 300)])
        self.assertEqual(len(tools.user32.mouse_events), 4)

    def test_click_rejects_coordinates_outside_virtual_desktop(self):
        tools = self.ready_tools()
        result = tools.click("FRESH", 5000, 300, "left")
        self.assertFalse(result["ok"])
        self.assertEqual(tools.user32.cursor_positions, [])

    def test_click_rejects_coordinate_outside_target_client_area(self):
        tools = self.ready_tools()
        tools.set_target_window(101)
        tools.snapshot = DesktopSnapshot("FRESH", time.monotonic(), 0, 0, 101, "target", "digest")
        tools.user32.foreground = 101
        result = tools.click("FRESH", 700, 300, "left")
        self.assertFalse(result["ok"])
        self.assertIn("target window", result["error"].lower())
        self.assertEqual(tools.user32.cursor_positions, [])

    def test_click_rejects_coordinate_owned_by_another_top_level_window(self):
        class ForeignPoint(FakeUser32):
            def WindowFromPoint(self, _point):
                return 202

            def GetAncestor(self, hwnd, _flags):
                return int(hwnd)

        tools = self.ready_tools()
        tools.user32 = ForeignPoint()
        tools.set_target_window(101)
        tools.snapshot = DesktopSnapshot("FRESH", time.monotonic(), 0, 0, 101, "target", "digest")
        tools.user32.foreground = 101
        result = tools.click("FRESH", 100, 100, "left")
        self.assertFalse(result["ok"])
        self.assertIn("target window", result["error"].lower())

    def test_coordinate_input_is_blocked_when_observation_reports_modal_dialog(self):
        tools = self.ready_tools()
        tools.set_modal_blocked(True)
        result = tools.type_text("FRESH", "hello")
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "desktop_modal_dialog")

    def test_coordinate_fallback_requires_and_consumes_one_time_token(self):
        tools = self.ready_tools()
        tools.set_target_window(101)
        tools.require_coordinate_token = True
        tools.snapshot = DesktopSnapshot("FRESH", time.monotonic(), 0, 0, 101, "target", "digest")
        tools.user32.foreground = 101
        issued = tools.issue_coordinate_fallback("FRESH", "click", "low risk selection")
        self.assertTrue(issued["ok"])
        token = issued["fallback_token"]
        first = tools.click("FRESH", 100, 100, "left", fallback_token=token)
        self.assertTrue(first["ok"])
        second = tools.click("FRESH", 100, 100, "left", fallback_token=token)
        self.assertFalse(second["ok"])
        self.assertIn("fallback", second["error"].lower())

    def test_coordinate_fallback_rejects_high_risk_reason(self):
        tools = self.ready_tools()
        tools.set_target_window(101)
        tools.snapshot = DesktopSnapshot("FRESH", time.monotonic(), 0, 0, 101, "target", "digest")
        tools.user32.foreground = 101
        result = tools.issue_coordinate_fallback("FRESH", "click", "click send and publish message")
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "desktop_coordinate_fallback_denied")

    def test_input_refuses_changed_foreground_window(self):
        tools = self.ready_tools()
        tools.snapshot = DesktopSnapshot("FRESH", time.monotonic(), 0, 0, 55, "target", "digest")
        tools.user32.foreground = 99
        result = tools.type_text("FRESH", "hello")
        self.assertFalse(result["ok"])
        self.assertIn("active window", result["error"].lower())

    def test_target_window_boundary_rejects_snapshot_from_another_window(self):
        tools = self.ready_tools()
        tools.set_target_window(101)
        tools.snapshot = DesktopSnapshot("FRESH", time.monotonic(), 0, 0, 55, "other", "digest")
        result = tools.type_text("FRESH", "hello")
        self.assertFalse(result["ok"])
        self.assertIn("target window", result["error"].lower())

        tools.snapshot = DesktopSnapshot("FRESH", time.monotonic(), 0, 0, 101, "target", "digest")
        tools.user32.foreground = 101
        self.assertTrue(tools.type_text("FRESH", "hello")["ok"])

    def test_capture_accepts_a_child_foreground_handle_of_the_locked_target(self):
        class ForegroundChild(FakeUser32):
            foreground = 202

            def GetCursorPos(self, point_ptr):
                point_ptr._obj.x, point_ptr._obj.y = 10, 20

            def GetAncestor(self, hwnd, _flags):
                return 101 if int(hwnd) == 202 else int(hwnd)

            def IsWindowVisible(self, hwnd):
                return int(hwnd) in {101, 202}

        tools = DesktopTools()
        tools.user32 = ForegroundChild()
        tools.user32.foreground = 202
        self.assertTrue(tools.set_target_window(101)["ok"])
        with patch("desktop_tools.foreground_capture_window", return_value=None), \
             patch("desktop_tools.window_title", return_value="Fixture Window"):
            result = tools.capture_state()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["active_window"], "Fixture Window")

    def test_capture_recovers_focus_from_the_known_overlay_before_observing(self):
        class OverlayForeground(FakeUser32):
            def GetCursorPos(self, point_ptr):
                point_ptr._obj.x, point_ptr._obj.y = 10, 20

        tools = DesktopTools()
        tools.user32 = OverlayForeground()
        tools.user32.foreground = 99
        self.assertTrue(tools.set_target_window(101)["ok"])
        tools.set_overlay_window(99)
        with patch("desktop_tools.foreground_capture_window", side_effect=lambda: tools.user32.foreground), \
             patch("desktop_tools.window_title", return_value="Fixture Window"):
            result = tools.capture_state()
        self.assertTrue(result["ok"], result)
        self.assertEqual(tools.user32.foreground, 101)

    def test_focus_target_only_releases_test_overlay_and_verifies_foreground(self):
        tools = self.ready_tools()
        tools.set_target_window(101)
        tools.set_overlay_window(99)
        tools.user32.foreground = 99

        focused = tools.focus_target(101)

        self.assertTrue(focused["ok"])
        self.assertTrue(focused["verified"])
        self.assertEqual(tools.user32.foreground, 101)

        tools.user32.foreground = 77
        rejected = tools.focus_target(101)
        self.assertFalse(rejected["ok"])
        self.assertIn("active window", rejected["error"].lower())

    def test_focus_target_allows_unknown_foreground_only_after_explicit_desktop_authorization(self):
        tools = self.ready_tools()
        tools.set_target_window(101)
        tools.user32.foreground = 77

        tools.set_current_desktop_authorization(True)
        focused = tools.focus_target(101)

        self.assertTrue(focused["ok"], focused)
        self.assertEqual(tools.user32.foreground, 101)

    def test_verify_uses_action_baseline_after_runtime_replaces_snapshot(self):
        tools = self.ready_tools()
        tools.set_target_window(101)
        tools.user32.foreground = 101
        tools.snapshot = DesktopSnapshot("BEFORE", time.monotonic(), 0, 0, 101, "target", "before")
        self.assertTrue(tools.type_text("BEFORE", "hello")["ok"])
        tools.snapshot = DesktopSnapshot("AFTER", time.monotonic(), 0, 0, 101, "target*", "after")

        with patch.object(tools, "capture_state", return_value={
            "ok": True, "active_window": "target*", "screen_digest": "after",
        }):
            result = tools.verify_state("AFTER")

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["screen_changed"])
        self.assertTrue(result["active_window_changed"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "waiting_verification")

    def test_focus_target_accepts_a_second_known_runner_overlay(self):
        tools = self.ready_tools()
        tools.set_target_window(101)
        tools.set_overlay_window(99)
        tools.add_overlay_window(98)
        tools.user32.foreground = 98

        focused = tools.focus_target(101)

        self.assertTrue(focused["ok"])
        self.assertEqual(tools.user32.foreground, 101)

    def test_focus_target_accepts_child_foreground_handle_after_activation(self):
        class ChildReportingForeground(FakeUser32):
            def GetAncestor(self, hwnd, _flags):
                return 101 if int(hwnd) == 202 else int(hwnd)

            def IsWindow(self, hwnd):
                return int(hwnd) in {101, 202}

            def IsWindowVisible(self, hwnd):
                return int(hwnd) in {101, 202}

            def SetForegroundWindow(self, hwnd):
                self.foreground = 202 if int(hwnd) == 101 else int(hwnd)

        tools = self.ready_tools()
        tools.user32 = ChildReportingForeground()
        tools.set_target_window(101)
        tools.set_overlay_window(99)
        tools.user32.foreground = 99

        focused = tools.focus_target(101)

        self.assertTrue(focused["ok"], focused)
        self.assertTrue(focused["verified"])
        self.assertEqual(tools.user32.foreground, 202)

    def test_focus_target_uses_thread_input_when_windows_rejects_direct_focus(self):
        class ForegroundLocked(FakeUser32):
            def __init__(self):
                super().__init__()
                self.attached = False
                self.message_queue = False
                self.GetWindowThreadProcessId = Mock(side_effect=self._get_window_thread)
                self.AttachThreadInput = Mock(side_effect=self._attach_thread_input)
                self.PeekMessageW = Mock(side_effect=self._peek_message)

            def _get_window_thread(self, hwnd, pid_ptr):
                pid_ptr._obj.value = 11 if int(hwnd) == 99 else 22
                return 11 if int(hwnd) == 99 else 22

            def _attach_thread_input(self, _current_thread, _target_thread, attach):
                self.attached = bool(attach)
                return int(self.message_queue)

            def _peek_message(self, *_args):
                self.message_queue = True
                return 1

            def SetForegroundWindow(self, hwnd):
                if self.attached:
                    self.foreground = int(hwnd)
                    return 1
                return 0

        tools = self.ready_tools()
        tools.user32 = ForegroundLocked()
        tools.kernel32 = Mock()
        tools.kernel32.GetCurrentThreadId = Mock(return_value=33)
        tools.set_target_window(101)
        tools.set_overlay_window(99)
        tools.user32.foreground = 99

        focused = tools.focus_target(101)

        self.assertTrue(focused["ok"], focused)
        self.assertTrue(focused["verified"])
        self.assertEqual(tools.user32.foreground, 101)
        self.assertTrue(tools.user32.PeekMessageW.called)
        self.assertEqual(tools.user32.AttachThreadInput.call_args_list[0].args[:2], (33, 22))

    def test_focus_target_uses_alt_wakeup_after_thread_attachment_is_rejected(self):
        class AltWakeup(FakeUser32):
            def __init__(self):
                super().__init__()
                self.alt_woken = False
                self.GetWindowThreadProcessId = Mock(side_effect=self._get_window_thread)
                self.AttachThreadInput = Mock(return_value=False)
                self.PeekMessageW = Mock(return_value=1)
                self.keybd_event = Mock(side_effect=self._key_event)

            def _get_window_thread(self, hwnd, pid_ptr):
                pid_ptr._obj.value = 22
                return 22

            def _key_event(self, _vk, _scan, flags, _extra):
                self.alt_woken = flags == 0x0002

            def SetForegroundWindow(self, hwnd):
                if self.alt_woken:
                    self.foreground = int(hwnd)
                    return 1
                return 0

        tools = self.ready_tools()
        tools.user32 = AltWakeup()
        tools.kernel32 = Mock()
        tools.kernel32.GetCurrentThreadId = Mock(return_value=33)
        tools.set_target_window(101)
        tools.set_overlay_window(99)
        tools.user32.foreground = 99

        focused = tools.focus_target(101)

        self.assertTrue(focused["ok"], focused)
        self.assertTrue(tools.user32.keybd_event.called)

    def test_hotkey_releases_keys_in_reverse_order(self):
        tools = self.ready_tools()
        result = tools.hotkey("FRESH", ["ctrl", "shift", "s"])
        self.assertTrue(result["ok"])
        self.assertEqual([event[0] for event in tools.user32.key_events[-3:]], [ord("S"), 0x10, 0x11])

    def test_hotkey_accepts_common_aliases_and_extended_function_keys(self):
        tools = self.ready_tools()
        result = tools.hotkey("FRESH", ["control", "option", "f24"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["keys"], ["ctrl", "alt", "f24"])

    def test_unsupported_hotkey_is_rejected_without_events(self):
        tools = self.ready_tools()
        result = tools.hotkey("FRESH", ["ctrl", "volume_up"])
        self.assertFalse(result["ok"])
        self.assertEqual(tools.user32.key_events, [])

    def test_scroll_uses_fresh_snapshot(self):
        tools = self.ready_tools()
        result = tools.scroll("FRESH", -2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["delta"], -2)

    def test_horizontal_scroll_uses_hwheel_flag(self):
        tools = self.ready_tools()
        result = tools.scroll("FRESH", 3, "horizontal")
        self.assertTrue(result["ok"])
        self.assertEqual(result["axis"], "horizontal")
        self.assertEqual(tools.user32.mouse_events[-1][0], 0x1000)

    def test_window_control_requires_fresh_listed_window_and_returns_bounds(self):
        tools = self.ready_tools()
        tools._window_snapshot = {
            101: WindowSnapshot(101, "Editor", 99, {"x": 20, "y": 30, "width": 600, "height": 400}, False, False)
        }
        tools._window_snapshot_at = time.monotonic()
        with patch("desktop_tools.window_title", return_value="Editor"):
            result = tools.control_window(101, "focus")
        self.assertTrue(result["ok"])
        self.assertEqual(tools.user32.last_show, (101, 9))
        self.assertEqual(result["after"]["width"], 600)

    def test_window_control_rejects_changed_window_title(self):
        tools = self.ready_tools()
        tools._window_snapshot = {
            101: WindowSnapshot(101, "Editor", 99, {"x": 20, "y": 30, "width": 600, "height": 400}, False, False)
        }
        tools._window_snapshot_at = time.monotonic()
        with patch("desktop_tools.window_title", return_value="Different window"):
            result = tools.control_window(101, "minimize")
        self.assertFalse(result["ok"])
        self.assertIn("changed", result["error"])

    def test_window_control_does_not_claim_maximize_without_placement_evidence(self):
        tools = self.ready_tools()
        tools._window_snapshot = {
            101: WindowSnapshot(101, "Editor", 99, {"x": 20, "y": 30, "width": 600, "height": 400}, False, False)
        }
        tools._window_snapshot_at = time.monotonic()
        tools.user32.PostMessageW = Mock()
        with patch("desktop_tools.window_title", return_value="Editor"):
            result = tools.control_window(101, "maximize")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "waiting_verification")
        self.assertEqual(result["verification"]["kind"], "window_placement")

    def test_window_control_does_not_claim_close_before_window_disappears(self):
        tools = self.ready_tools()
        tools._window_snapshot = {
            101: WindowSnapshot(101, "Editor", 99, {"x": 20, "y": 30, "width": 600, "height": 400}, False, False)
        }
        tools._window_snapshot_at = time.monotonic()
        tools.user32.PostMessageW = Mock()
        with patch("desktop_tools.window_title", return_value="Editor"):
            result = tools.control_window(101, "close")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "waiting_verification")

    def test_window_control_topmost_uses_before_and_after_style(self):
        class Topmost(FakeUser32):
            def __init__(self):
                super().__init__()
                self.topmost = False

            def GetWindowLongW(self, _hwnd, _index):
                return 0x00000008 if self.topmost else 0

            def SetWindowPos(self, *_args):
                self.topmost = not self.topmost

        tools = self.ready_tools()
        tools.user32 = Topmost()
        tools._window_snapshot = {
            101: WindowSnapshot(101, "Editor", 99, {"x": 20, "y": 30, "width": 600, "height": 400}, False, False)
        }
        tools._window_snapshot_at = time.monotonic()
        with patch("desktop_tools.window_title", return_value="Editor"):
            result = tools.control_window(101, "toggle_topmost")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["verification"]["passed"])


if __name__ == "__main__":
    unittest.main()
