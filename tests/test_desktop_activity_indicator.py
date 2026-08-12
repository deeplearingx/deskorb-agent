import unittest

from desktop_activity_indicator import (
    DESKTOP_ACTIVITY_TOOLS,
    ActivityTransition,
    DesktopActivityEvent,
    DesktopActivityLifecycle,
)


class FakeScheduler:
    def __init__(self):
        self.now = 0.0
        self._next_handle = 1
        self._callbacks = {}

    def after(self, delay_ms, callback):
        handle = self._next_handle
        self._next_handle += 1
        self._callbacks[handle] = (self.now + max(0, delay_ms) / 1000.0, callback)
        return handle

    def after_cancel(self, handle):
        self._callbacks.pop(handle, None)

    def advance(self, seconds):
        target = self.now + seconds
        while True:
            due = [(handle, item) for handle, item in self._callbacks.items()
                   if item[0] <= target]
            if not due:
                break
            handle, (_, callback) = min(due, key=lambda pair: pair[1][0])
            self._callbacks.pop(handle, None)
            self.now = min(target, self.now if self.now > target else target)
            callback()
        self.now = target


class DesktopActivityEventTests(unittest.TestCase):
    def test_payload_round_trip_contains_only_safe_fields(self):
        event = DesktopActivityEvent("begin", "desktop_click", 7)

        self.assertEqual(event.to_payload(), {
            "phase": "begin",
            "tool": "desktop_click",
            "activity_id": 7,
        })
        self.assertEqual(DesktopActivityEvent.from_payload(event.to_payload()), event)

    def test_malformed_payload_is_rejected(self):
        self.assertIsNone(DesktopActivityEvent.from_payload(None))
        self.assertIsNone(DesktopActivityEvent.from_payload({"phase": "begin"}))
        self.assertIsNone(DesktopActivityEvent.from_payload({
            "phase": "begin", "tool": "desktop_capture_state", "activity_id": 1,
        }))
        self.assertIsNone(DesktopActivityEvent.from_payload({
            "phase": "begin", "tool": "desktop_click", "activity_id": True,
        }))


class DesktopActivityLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = FakeScheduler()
        self.transitions = []
        self.lifecycle = DesktopActivityLifecycle(
            clock=lambda: self.scheduler.now,
            schedule=self.scheduler.after,
            cancel=self.scheduler.after_cancel,
            on_transition=self.transitions.append,
        )

    def test_supported_desktop_action_enters_and_completes(self):
        self.assertTrue(self.lifecycle.begin(1, "desktop_click"))
        self.assertEqual(self.lifecycle.state, "entering")
        self.assertEqual([item.name for item in self.transitions], ["enter"])

        self.scheduler.advance(0.2)

        self.assertEqual(self.lifecycle.state, "visible")
        self.assertEqual([item.name for item in self.transitions], ["enter", "visible"])

    def test_only_supported_actions_can_start_an_activity(self):
        for index, tool in enumerate(DESKTOP_ACTIVITY_TOOLS, start=1):
            self.assertTrue(self.lifecycle.begin(index, tool))
            self.assertTrue(self.lifecycle.end(index, tool))
            self.lifecycle.force_hide()

        self.assertFalse(self.lifecycle.begin(100, "desktop_capture_state"))
        self.assertFalse(self.lifecycle.begin(101, "filesystem_write"))
        self.assertEqual(self.lifecycle.state, "hidden")

    def test_end_waits_for_minimum_visible_time_and_idle_grace(self):
        self.lifecycle.begin(1, "desktop_type")
        self.scheduler.advance(0.2)
        self.lifecycle.end(1, "desktop_type")

        self.scheduler.advance(0.249)
        self.assertEqual(self.lifecycle.state, "visible")
        self.scheduler.advance(0.051)
        self.assertEqual(self.lifecycle.state, "exiting")
        self.assertEqual([item.name for item in self.transitions], ["enter", "visible", "exit"])

        self.scheduler.advance(0.279)
        self.assertEqual(self.lifecycle.state, "exiting")
        self.scheduler.advance(0.001)
        self.assertEqual(self.lifecycle.state, "hidden")

    def test_contiguous_actions_share_one_visible_cycle(self):
        self.lifecycle.begin(1, "desktop_click")
        self.scheduler.advance(0.2)
        self.lifecycle.end(1, "desktop_click")
        self.scheduler.advance(0.2)
        self.lifecycle.begin(2, "desktop_hotkey")
        self.scheduler.advance(0.2)
        self.lifecycle.end(2, "desktop_hotkey")
        self.scheduler.advance(0.31)

        self.assertEqual([item.name for item in self.transitions], ["enter", "visible", "exit"])

    def test_new_action_revives_exiting_state_without_hidden_transition(self):
        self.lifecycle.begin(1, "desktop_scroll")
        self.scheduler.advance(0.2)
        self.lifecycle.end(1, "desktop_scroll")
        self.scheduler.advance(0.301)
        self.assertEqual(self.lifecycle.state, "exiting")

        self.lifecycle.begin(2, "window_focus")

        self.assertEqual(self.lifecycle.state, "entering")
        self.assertEqual([item.name for item in self.transitions], ["enter", "visible", "exit", "enter"])
        self.scheduler.advance(0.2)
        self.assertEqual(self.lifecycle.state, "visible")

    def test_unknown_or_repeated_end_cannot_close_new_activity(self):
        self.lifecycle.begin(1, "desktop_click")
        self.scheduler.advance(0.2)
        self.assertFalse(self.lifecycle.end(99, "desktop_click"))
        self.lifecycle.end(1, "desktop_click")
        self.lifecycle.begin(2, "desktop_type")
        self.assertFalse(self.lifecycle.end(1, "desktop_click"))
        self.assertEqual(self.lifecycle.state, "visible")
        self.assertEqual(self.lifecycle.active_activity_ids, frozenset({2}))
        self.assertTrue(self.lifecycle.end(2, "desktop_type"))

    def test_duplicate_begin_is_idempotent(self):
        self.assertTrue(self.lifecycle.begin(1, "desktop_click"))
        self.assertTrue(self.lifecycle.begin(1, "desktop_click"))
        self.assertEqual([item.name for item in self.transitions], ["enter"])

    def test_force_hide_cancels_pending_callbacks_and_is_idempotent(self):
        self.lifecycle.begin(1, "desktop_type")
        self.lifecycle.force_hide()
        self.lifecycle.force_hide()
        self.scheduler.advance(2.0)

        self.assertEqual(self.lifecycle.state, "hidden")
        self.assertEqual([item.name for item in self.transitions], ["enter", "hidden"])

    def test_transition_has_generation_for_stale_callback_protection(self):
        self.lifecycle.begin(1, "desktop_click")
        transition = self.transitions[0]

        self.assertIsInstance(transition, ActivityTransition)
        self.assertEqual(transition.generation, 1)


if __name__ == "__main__":
    unittest.main()
