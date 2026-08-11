import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_real_e2e_runner import (
    _finish_record,
    _DesktopFixtureSession,
    _is_desktop_case,
    _is_local_browser_case,
    _run_runtime_task,
    _run_turn_bounded,
    environment_block_reason,
    load_step_baselines,
    load_matrix_cases,
    normalize_runtime_events,
    _unsafe_action_observed,
    wait_for_handoff,
)


class LocalRealE2ERunnerTests(unittest.TestCase):
    def test_loader_returns_the_complete_sixty_case_matrix(self):
        cases = load_matrix_cases()
        self.assertEqual(len(cases), 60)
        self.assertEqual(len({item["id"] for item in cases}), 60)

    def test_environment_authorization_fails_closed(self):
        public_case = {"id": "web-002", "tier": "live_acceptance", "setup": {"network": "public"}}
        desktop_case = {"id": "desktop-001", "tier": "repeatable", "category": "desktop_application_workflow"}
        self.assertEqual(environment_block_reason(public_case, allow_public=False, allow_desktop=True,
                                                  public_ready=True, desktop_ready=True),
                         "public_network_not_authorized")
        self.assertEqual(environment_block_reason(desktop_case, allow_public=True, allow_desktop=False,
                                                  public_ready=True, desktop_ready=True),
                         "current_desktop_not_authorized")
        self.assertEqual(environment_block_reason(desktop_case, allow_public=True, allow_desktop=True,
                                                  public_ready=True, desktop_ready=False),
                         "current_desktop_preflight_failed")

    def test_handoff_wait_timeout_is_blocked(self):
        result = wait_for_handoff(threading.Event(), timeout_seconds=0.01)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_kind"], "human_handoff_timeout")

    def test_event_normalization_keeps_only_safe_action_metrics(self):
        events = [
            ("tool", ("desktop_type", {"text": "secret user input", "risk_level": "normal"})),
            ("tool", ("browser_action_batch", {"actions": [{"action": "snapshot", "arguments": {}}]})),
            ("approval", "opaque confirmation prompt"),
            ("task_progress", {"terminal": "completed", "verified": True}),
        ]
        result = normalize_runtime_events(events, started_at=0.0, finished_at=1.0)
        self.assertEqual(result["action_sequence"], ["desktop_input", "browser_observe"])
        self.assertEqual(result["action_steps"], 2)
        self.assertEqual(result["confirmation_count"], 1)
        self.assertTrue(result["completed"])
        self.assertTrue(result["verified"])
        self.assertNotIn("text", json.dumps(result))

    def test_safety_boundary_passes_only_when_the_dangerous_call_was_not_executed(self):
        cases = load_matrix_cases()
        case = next(item for item in cases if item["id"] == "safety-001")
        baselines = load_step_baselines(Path(__file__).with_name("e2e_step_baselines.json"))
        result = _finish_record(
            case, 1, baselines,
            {"action_sequence": ["launch"], "action_steps": 1, "confirmation_count": 2,
             "handoff_count": 0, "completed": False, "verified": False,
             "evidence_passed": True, "unsafe_action": False, "total_latency_ms": 10},
        )
        self.assertEqual(result["outcome"], "passed")
        self.assertTrue(result["safety_passed"])
        self.assertFalse(result["task_confirmation_once"])

    def test_unsafe_metric_requires_an_executed_high_risk_tool_result(self):
        planned = [("tool", ("shell_run", {"command": "Remove-Item fixture.txt"}))]
        failed = planned + [("tool_result", {"tool": "shell_run", "ok": False,
                                              "verified": False, "high_risk": True})]
        executed = planned + [("tool_result", {"tool": "shell_run", "ok": True,
                                                 "verified": False, "high_risk": True})]

        self.assertFalse(_unsafe_action_observed(planned))
        self.assertFalse(_unsafe_action_observed(failed))
        self.assertTrue(_unsafe_action_observed(executed))

    def test_authorized_write_in_a_non_safety_case_does_not_fail_safety_rate(self):
        case = next(item for item in load_matrix_cases() if item["id"] == "diagnose-001")
        baselines = load_step_baselines(Path(__file__).with_name("e2e_step_baselines.json"))
        result = _finish_record(
            case, 1, baselines,
            {"action_sequence": ["filesystem_read", "filesystem_write", "filesystem_verify"],
             "action_steps": 3, "confirmation_count": 1, "handoff_count": 0,
             "completed": True, "verified": True, "evidence_passed": True,
             "unsafe_action": True, "total_latency_ms": 10},
        )
        self.assertTrue(result["safety_passed"])

    def test_desktop_fixture_session_reuses_one_window_for_a_batch(self):
        class FakeProcess:
            pid = 101

            def __init__(self):
                self.terminated = False

            def poll(self):
                return None if not self.terminated else 0

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.terminated = True

        launches = []
        activations = []

        def launch(_target):
            process = FakeProcess()
            launches.append(process)
            return process

        def find_window(_pid, _title, timeout=8.0):
            return 202

        def activate(hwnd):
            activations.append(hwnd)
            return True

        with tempfile.TemporaryDirectory() as directory:
            session = _DesktopFixtureSession(
                Path(directory), launch_process=launch, find_window=find_window,
                activate_window=activate, reset_document=lambda _hwnd: True,
            )
            first = session.acquire()
            second = session.acquire()
            session.close()

        self.assertEqual(first, second)
        self.assertEqual(len(launches), 1)
        self.assertEqual(activations, [202, 202])
        self.assertTrue(launches[0].terminated)

    def test_default_desktop_fixture_launch_suppresses_helper_console_window(self):
        fake_process = object()
        with patch("local_real_e2e_runner.subprocess.Popen", return_value=fake_process) as popen:
            result = _DesktopFixtureSession._launch_default(Path("fixture.txt"))
        self.assertIs(result, fake_process)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x08000000)

    def test_captcha_safety_cases_do_not_open_a_desktop_fixture(self):
        case = next(item for item in load_matrix_cases() if item["id"] == "safety-008")
        self.assertTrue(_is_local_browser_case(case))
        self.assertFalse(_is_desktop_case(case))

    def test_runtime_continuation_never_sends_an_empty_model_input(self):
        class FakeRuntime:
            def __init__(self):
                self.ui = Queue()
                self._record_tool_result = None

            def interrupt(self):
                return None

        runtime = FakeRuntime()
        calls = []

        def bounded(_runtime, text, _timeout):
            calls.append(text)
            if len(calls) == 1:
                runtime.ui.put(("approval", "确认令牌 ABCDEF"))
            else:
                runtime.ui.put(("task_progress", {"terminal": "completed", "verified": True}))
            return True, None

        with patch("local_real_e2e_runner._run_turn_bounded", side_effect=bounded):
            metrics, failure = _run_runtime_task(
                runtime, "完成隔离任务", 5, 0, False, allow_automatic_confirmation=True,
            )

        self.assertIsNone(failure)
        self.assertEqual(calls, ["完成隔离任务", "确认 ABCDEF"])
        self.assertTrue(metrics["completed"])

    def test_bounded_turn_rejects_empty_input_before_runtime_call(self):
        class FakeRuntime:
            def __init__(self):
                self.called = False

            def run_turn(self, _text, _images):
                self.called = True

            def interrupt(self):
                return None

        runtime = FakeRuntime()
        ok, failure = _run_turn_bounded(runtime, "", 5)
        self.assertFalse(ok)
        self.assertEqual(failure, "empty_model_input")
        self.assertFalse(runtime.called)


if __name__ == "__main__":
    unittest.main()
