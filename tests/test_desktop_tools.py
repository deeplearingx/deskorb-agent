import unittest
import time

from desktop_tools import DesktopSnapshot, DesktopTools


class FakeUser32:
    def __init__(self):
        self.key_events = []
        self.send_count = 0

    def SendInput(self, count, inputs, size):
        self.send_count = count
        return count

    def keybd_event(self, vk, scan, flags, extra):
        self.key_events.append((vk, flags))


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
        tools.snapshot = DesktopSnapshot("FRESH", time.monotonic(), 0, 0, "target", "digest")
        return tools

    def test_unicode_typing_uses_paired_input_events(self):
        tools = self.ready_tools()
        result = tools.type_text("FRESH", "A中")
        self.assertTrue(result["ok"])
        self.assertEqual(tools.user32.send_count, 4)

    def test_hotkey_releases_keys_in_reverse_order(self):
        tools = self.ready_tools()
        result = tools.hotkey("FRESH", ["ctrl", "shift", "s"])
        self.assertTrue(result["ok"])
        self.assertEqual([event[0] for event in tools.user32.key_events[-3:]], [ord("S"), 0x10, 0x11])

    def test_unsupported_hotkey_is_rejected_without_events(self):
        tools = self.ready_tools()
        result = tools.hotkey("FRESH", ["ctrl", "volume_up"])
        self.assertFalse(result["ok"])
        self.assertEqual(tools.user32.key_events, [])

    def test_scroll_uses_fresh_snapshot(self):
        tools = self.ready_tools()
        tools.user32.mouse_event = lambda *args: setattr(tools.user32, "scroll_event", args)
        result = tools.scroll("FRESH", -2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["delta"], -2)


if __name__ == "__main__":
    unittest.main()
