import unittest
from unittest.mock import Mock

from desktop_activity_indicator import (
    DESKTOP_ACTIVITY_TOOLS,
    ActivityTransition,
    DesktopActivityIndicator,
    DesktopActivityEvent,
    DesktopActivityLifecycle,
    indicator_window_geometry,
)
from win32utils import (
    GWL_EXSTYLE,
    HWND_TOPMOST,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_SHOWWINDOW,
    WDA_EXCLUDEFROMCAPTURE,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TRANSPARENT,
    configure_indicator_window,
)
from deskorb_agent import Overlay


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
            handle, (due_at, callback) = min(due, key=lambda pair: pair[1][0])
            self._callbacks.pop(handle, None)
            self.now = due_at
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


class DesktopActivityGeometryTests(unittest.TestCase):
    def test_geometry_stays_inside_negative_and_positive_monitor_rects(self):
        monitors = [
            {"rect": (-1920, -120, 0, 960)},
            {"rect": (0, 0, 1920, 1080)},
        ]

        layout = indicator_window_geometry(
            monitors, edge_band=64, pill_width=360, pill_height=44, top_margin=20,
        )

        self.assertEqual(len(layout), 10)
        for item in layout:
            left, top, right, bottom = item["rect"]
            monitor = monitors[item["monitor_index"]]["rect"]
            self.assertLess(left, right)
            self.assertLess(top, bottom)
            self.assertGreaterEqual(left, monitor[0])
            self.assertGreaterEqual(top, monitor[1])
            self.assertLessEqual(right, monitor[2])
            self.assertLessEqual(bottom, monitor[3])

    def test_each_monitor_has_four_edges_and_centered_pill(self):
        monitor = {"rect": (-100, 40, 900, 740)}

        layout = indicator_window_geometry(
            [monitor], edge_band=60, pill_width=300, pill_height=48, top_margin=24,
        )
        by_kind = {item["kind"]: item["rect"] for item in layout}

        self.assertEqual(set(by_kind), {"top", "bottom", "left", "right", "pill"})
        self.assertEqual(by_kind["top"], (-100, 40, 900, 100))
        self.assertEqual(by_kind["bottom"], (-100, 680, 900, 740))
        self.assertEqual(by_kind["left"], (-100, 100, -40, 680))
        self.assertEqual(by_kind["right"], (840, 100, 900, 680))
        self.assertEqual(by_kind["pill"], (250, 64, 550, 112))


class DesktopActivityWindowStyleTests(unittest.TestCase):
    class FakeUser32:
        def __init__(self, *, affinity=True, position=True, top_level=None):
            self.style = 0
            self.affinity = affinity
            self.position = position
            self.top_level = top_level
            self.calls = []

        def GetAncestor(self, hwnd, root):
            self.calls.append(("ancestor", hwnd, root))
            return self.top_level or hwnd

        def GetWindowLongW(self, hwnd, index):
            self.calls.append(("get_style", hwnd, index))
            return self.style

        def SetWindowLongW(self, hwnd, index, value):
            self.calls.append(("set_style", hwnd, index, value))
            old = self.style
            self.style = value
            return old

        def SetWindowPos(self, hwnd, insert_after, x, y, width, height, flags):
            self.calls.append(("position", hwnd, insert_after, flags))
            return self.position

        def SetWindowDisplayAffinity(self, hwnd, affinity):
            self.calls.append(("affinity", hwnd, affinity))
            return self.affinity

    def test_configure_indicator_window_sets_passive_and_capture_safe_flags(self):
        api = self.FakeUser32()

        configured = configure_indicator_window(
            42, api=api, hit_test_installer=lambda hwnd, api: True,
        )

        self.assertTrue(configured)
        required = WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        self.assertEqual(api.style & required, required)
        position_call = next(call for call in api.calls if call[0] == "position")
        self.assertIs(position_call[2], HWND_TOPMOST)
        self.assertEqual(position_call[3], SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.assertIn(("affinity", 42, WDA_EXCLUDEFROMCAPTURE), api.calls)
        self.assertIn(("get_style", 42, GWL_EXSTYLE), api.calls)

    def test_configure_indicator_window_fails_closed_when_hit_test_or_capture_fails(self):
        for installer, api in (
            (lambda hwnd, api: False, self.FakeUser32()),
            (lambda hwnd, api: True, self.FakeUser32(affinity=False)),
            (lambda hwnd, api: True, self.FakeUser32(position=False)),
        ):
            self.assertFalse(configure_indicator_window(42, api=api, hit_test_installer=installer))

    def test_configure_indicator_window_does_not_require_capture_when_explicitly_disabled(self):
        api = self.FakeUser32(affinity=False)

        self.assertTrue(configure_indicator_window(
            42, api=api, capture_excluded=False,
            hit_test_installer=lambda hwnd, api: True,
        ))
        self.assertFalse(any(call[0] == "affinity" for call in api.calls))

    def test_configure_indicator_window_promotes_tk_child_to_top_level_hwnd(self):
        api = self.FakeUser32(top_level=99)
        installed = []

        self.assertTrue(configure_indicator_window(
            42, api=api, hit_test_installer=lambda hwnd, api: installed.append(hwnd) or True,
        ))
        self.assertEqual(installed, [99])
        self.assertIn(("get_style", 99, GWL_EXSTYLE), api.calls)
        self.assertIn(("affinity", 99, WDA_EXCLUDEFROMCAPTURE), api.calls)


class FakeIndicatorWindow:
    _next_id = 100

    def __init__(self):
        self.hwnd = FakeIndicatorWindow._next_id
        FakeIndicatorWindow._next_id += 1
        self.destroyed = False
        self.visible = False
        self.alpha = None
        self.geometries = []
        self.attributes_log = []

    def withdraw(self):
        self.visible = False

    def deiconify(self):
        self.visible = True

    def destroy(self):
        self.destroyed = True
        self.visible = False

    def overrideredirect(self, value):
        return None

    def attributes(self, name, value=None):
        self.attributes_log.append((name, value))
        if name == "-alpha" and value is not None:
            self.alpha = value

    wm_attributes = attributes

    def configure(self, **kwargs):
        return None

    def geometry(self, value):
        self.geometries.append(value)

    def update_idletasks(self):
        return None

    def winfo_id(self):
        return self.hwnd


class DesktopActivityIndicatorRendererTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = FakeScheduler()
        self.windows = []
        self.configured = []
        self.uninstalled = []

    def make_indicator(self, configure=None):
        def factory(_root):
            window = FakeIndicatorWindow()
            self.windows.append(window)
            return window

        return DesktopActivityIndicator(
            object(),
            monitor_provider=lambda: [{"rect": (0, 0, 1280, 720)}],
            window_factory=factory,
            configure_window=configure or self._configure,
            uninstall_window=lambda hwnd: self.uninstalled.append(hwnd),
            content_builder=lambda window, kind, width, height: None,
            clock=lambda: self.scheduler.now,
            schedule=self.scheduler.after,
            cancel=self.scheduler.after_cancel,
        )

    def _configure(self, hwnd, **_kwargs):
        self.configured.append(hwnd)
        return True

    def test_begin_builds_four_edges_and_one_pill_then_fades_naturally(self):
        indicator = self.make_indicator()

        self.assertTrue(indicator.handle_event({
            "phase": "begin", "tool": "desktop_click", "activity_id": 1,
        }))
        self.assertEqual(len(self.windows), 5)
        self.assertEqual(len(self.configured), 5)
        self.assertTrue(all(window.visible for window in self.windows))
        self.assertTrue(all(window.alpha == 0.0 for window in self.windows))

        self.scheduler.advance(0.2)
        self.assertEqual(indicator.lifecycle.state, "visible")
        self.assertTrue(all(window.visible for window in self.windows))
        self.assertTrue(all(window.alpha is not None and window.alpha > 0 for window in self.windows))

        self.assertTrue(indicator.handle_event({
            "phase": "end", "tool": "desktop_click", "activity_id": 1,
        }))
        self.scheduler.advance(0.75)
        self.assertEqual(indicator.lifecycle.state, "hidden")
        self.assertTrue(all(not window.visible for window in self.windows))

    def test_continuous_action_does_not_rebuild_or_flash_windows(self):
        indicator = self.make_indicator()
        indicator.handle_event({"phase": "begin", "tool": "desktop_click", "activity_id": 1})
        self.scheduler.advance(0.2)
        indicator.handle_event({"phase": "end", "tool": "desktop_click", "activity_id": 1})
        self.scheduler.advance(0.2)
        indicator.handle_event({"phase": "begin", "tool": "desktop_type", "activity_id": 2})

        self.assertEqual(len(self.windows), 5)
        self.assertTrue(all(window.visible for window in self.windows))

    def test_action_during_exit_reverses_from_current_opacity(self):
        indicator = self.make_indicator()
        indicator.handle_event({"phase": "begin", "tool": "desktop_click", "activity_id": 1})
        self.scheduler.advance(0.2)
        indicator.handle_event({"phase": "end", "tool": "desktop_click", "activity_id": 1})
        self.scheduler.advance(0.55)
        current = indicator._render_alpha
        self.assertLess(current, indicator.MAX_ALPHA)

        indicator.handle_event({"phase": "begin", "tool": "desktop_type", "activity_id": 2})

        self.assertGreater(indicator._animation_from, 0.0)
        self.assertAlmostEqual(indicator._animation_from, current)

    def test_configuration_failure_destroys_every_window_and_disables_session(self):
        indicator = self.make_indicator(configure=lambda hwnd, **kwargs: False)

        self.assertFalse(indicator.handle_event({
            "phase": "begin", "tool": "desktop_click", "activity_id": 1,
        }))
        self.assertTrue(indicator.disabled)
        self.assertTrue(all(window.destroyed for window in self.windows))
        self.assertEqual(indicator.window_count, 0)

    def test_force_hide_and_destroy_are_idempotent(self):
        indicator = self.make_indicator()
        indicator.handle_event({"phase": "begin", "tool": "desktop_type", "activity_id": 1})
        indicator.force_hide()
        indicator.force_hide()
        indicator.destroy()
        indicator.destroy()

        self.assertTrue(all(window.destroyed for window in self.windows))
        self.assertEqual(indicator.window_count, 0)


class OverlayDesktopActivityIntegrationTests(unittest.TestCase):
    def make_overlay(self):
        overlay = Overlay.__new__(Overlay)
        overlay.desktop_activity_indicator = Mock()
        overlay._set_busy = Mock()
        overlay._refresh_statusline = Mock()
        overlay._maybe_flag_done = Mock()
        overlay._remember_word_attachment_turn = Mock()
        overlay._md_finalize = Mock()
        overlay._finish_turn_copy = Mock()
        overlay.add_sys = Mock()
        overlay.add_err = Mock()
        overlay._format_turn_error = Mock(return_value="provider error")
        overlay._office_plan_active = False
        overlay._active_word_attachment_question = None
        overlay._active_word_attachment_document_id = None
        overlay._active_office_question = None
        return overlay

    def test_poll_handler_forwards_only_bounded_desktop_activity_payload(self):
        overlay = self.make_overlay()
        payload = {"phase": "begin", "tool": "desktop_click", "activity_id": 3}

        overlay._handle("desktop_activity", payload)

        overlay.desktop_activity_indicator.handle_event.assert_called_once_with(payload)

    def test_terminal_events_force_hide_activity_indicator(self):
        for kind, payload in (
            ("turn_done", None),
            ("error", "failed"),
            ("result", {"is_error": True}),
            ("reset_done", None),
        ):
            overlay = self.make_overlay()
            if kind == "reset_done":
                overlay.add_sys = Mock()
            overlay._handle(kind, payload)
            overlay.desktop_activity_indicator.force_hide.assert_called_once_with()

    def test_approval_and_human_handoff_also_hide_any_stale_indicator(self):
        overlay = self.make_overlay()
        overlay.add_approval = Mock()
        overlay.add_human_verification = Mock()

        overlay._handle("approval", "confirmation")
        overlay._handle("human_verification", {"marker": "验证码"})

        self.assertEqual(overlay.desktop_activity_indicator.force_hide.call_count, 2)

    def test_quit_destroys_indicator_before_root_teardown(self):
        overlay = self.make_overlay()
        overlay._quitting = False
        overlay.meeting_recorder = None
        overlay._keyboard = None
        overlay.worker = Mock()
        overlay.worker.join.return_value = None
        overlay.root = Mock()

        with self.assertRaises(SystemExit):
            with unittest.mock.patch("deskorb_agent.os._exit", side_effect=SystemExit(0)):
                overlay.quit()

        overlay.desktop_activity_indicator.destroy.assert_called_once_with()

    def test_stop_action_forces_activity_indicator_hidden_immediately(self):
        overlay = self.make_overlay()
        overlay.busy = True
        overlay.worker = Mock()
        overlay._set_status = Mock()

        overlay._send_or_stop()

        overlay.desktop_activity_indicator.force_hide.assert_called_once_with()
        overlay.worker.interrupt.assert_called_once_with()


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
