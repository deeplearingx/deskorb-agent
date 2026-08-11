import unittest
import time
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
