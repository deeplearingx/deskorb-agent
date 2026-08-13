import json
import os
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
    _append_contract_guidance,
    _is_desktop_case,
    _is_local_browser_case,
    _run_runtime_task,
    _run_turn_bounded,
    environment_block_reason,
    load_step_baselines,
    load_matrix_cases,
    normalize_runtime_events,
    _unsafe_action_observed,
    _prepare_evaluation_tool_environment,
    _remember_current_host_overlay,
    _remember_host_process_overlays,
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

    def test_dynamic_search_fixture_is_a_local_browser_case(self):
        cases = load_matrix_cases()
        case = next(item for item in cases if item["id"] == "research-001")
        self.assertEqual((case.get("setup") or {}).get("fixture"), "dynamic_search")
        self.assertTrue(_is_local_browser_case(case))

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

    def test_event_normalization_counts_semantic_actions_inside_browser_batch(self):
        events = [
            ("tool", ("Browser action", {"actions": [
                {"action": "snapshot", "arguments": {}},
                {"action": "fill_ref", "arguments": {"ref": "search", "value": "secret"}},
            ]})),
        ]
        result = normalize_runtime_events(events, started_at=0.0, finished_at=1.0)
        self.assertEqual(result["action_sequence"], ["browser_observe", "browser_input"])
        self.assertEqual(result["action_steps"], 2)
        self.assertNotIn("secret", json.dumps(result))

    def test_event_normalization_preserves_terminal_failure_category(self):
        result = normalize_runtime_events(
            [("task_progress", {"terminal": "failed", "verified": False,
                                 "failure_kind": "file_verification_failure"})],
            started_at=0.0, finished_at=1.0,
        )
        self.assertEqual(result["terminal"], "failed")
        self.assertEqual(result["failure_kind"], "file_verification_failure")

    def test_event_normalization_preserves_only_failed_tool_structure(self):
        result = normalize_runtime_events(
            [("tool_result", {"tool": "shell_run", "ok": False,
                               "failure_kind": "tool_failure", "exit_code": 1,
                               "error": "private output"})],
            started_at=0.0, finished_at=1.0,
        )
        self.assertEqual(result["failed_tool"], "shell_run")
        self.assertEqual(result["tool_failure_kind"], "tool_failure")
        self.assertEqual(result["failed_exit_code"], 1)
        self.assertNotIn("private output", json.dumps(result))

    def test_event_normalization_keeps_cache_execution_metrics(self):
        result = normalize_runtime_events([
            ("tool_result", {"tool": "browser_action_batch", "ok": True,
                              "execution_source": "cache", "cache_status": "exact_hit",
                              "model_fallback": False, "postcondition_passed": True}),
        ], started_at=0.0, finished_at=1.0)

        self.assertEqual(result["execution_source_counts"], {"cache": 1})
        self.assertEqual(result["cache_status_counts"], {"exact_hit": 1})
        self.assertEqual(result["model_fallback_count"], 0)
        self.assertTrue(result["postcondition_passed"])

    def test_event_normalization_emits_only_structural_stability_metrics(self):
        result = normalize_runtime_events([
            ("tool_result", {"tool": "browser_action_batch", "ok": False,
                              "failure_kind": "browser_reobservation_required",
                              "interaction_stage": "ready_to_choose",
                              "postcondition_kind": "none", "model_fallback": True}),
            ("tool_result", {"tool": "browser_action_batch", "ok": True,
                              "interaction_stage": "verified",
                              "postcondition_kind": "structured_fields",
                              "postcondition_passed": True}),
        ], started_at=0.0, finished_at=1.0)
        self.assertEqual(result["recovery_count"], 1)
        self.assertEqual(result["stage_transition_count"], 1)
        self.assertEqual(result["postcondition_kind"], "structured_fields")
        self.assertEqual(len(result["trace_hash"]), 64)
        self.assertNotIn("ready_to_choose", json.dumps(result))

    def test_finish_record_adds_environment_and_stability_metrics(self):
        case = next(item for item in load_matrix_cases() if item["id"] == "research-001")
        baselines = load_step_baselines(Path(__file__).with_name("e2e_step_baselines.json"))
        result = _finish_record(
            case, 1, baselines,
            {"action_sequence": ["browser_observe"], "completed": False, "verified": False,
             "recovery_count": 1, "stage_transition_count": 2,
             "postcondition_kind": "none", "trace_hash": "a" * 64,
             "total_latency_ms": 10}, failure_kind="browser_no_progress",
        )
        self.assertEqual(result["environment_class"], "local_fixture")
        self.assertEqual(result["recovery_count"], 1)
        self.assertEqual(result["stage_transition_count"], 2)
        self.assertEqual(result["trace_hash"], "a" * 64)

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

    def test_missing_required_actions_cannot_be_reported_as_passed(self):
        case = next(item for item in load_matrix_cases() if item["id"] == "diagnose-001")
        baselines = load_step_baselines(Path(__file__).with_name("e2e_step_baselines.json"))
        result = _finish_record(
            case, 1, baselines,
            {"action_sequence": ["filesystem_read"], "action_steps": 1,
             "confirmation_count": 0, "handoff_count": 0, "completed": True,
             "verified": True, "evidence_passed": True, "total_latency_ms": 10},
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["failure_kind"], "required_action_missing")
        self.assertEqual(result["missing_required_action_kinds"], ["filesystem_write", "shell_verify"])

    def test_confirmation_is_satisfied_by_an_approval_event_not_a_tool_label(self):
        case = next(item for item in load_matrix_cases() if item["id"] == "desktop-003")
        baselines = load_step_baselines(Path(__file__).with_name("e2e_step_baselines.json"))
        result = _finish_record(
            case, 1, baselines,
            {"action_sequence": ["launch", "desktop_observe", "desktop_input", "desktop_verify"],
             "action_steps": 4, "confirmation_count": 1, "handoff_count": 0,
             "completed": True, "verified": True, "evidence_passed": True,
             "total_latency_ms": 10},
        )
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["missing_required_action_kinds"], [])

    def test_real_task_prompt_includes_required_baseline_actions(self):
        case = next(item for item in load_matrix_cases() if item["id"] == "diagnose-001")
        baselines = load_step_baselines(Path(__file__).with_name("e2e_step_baselines.json"))
        prompt = _append_contract_guidance("diagnose", case, baselines)
        self.assertIn("filesystem_write", prompt)
        self.assertIn("shell_run once", prompt)

    def test_desktop_task_prompt_requires_continuation_after_observation(self):
        case = next(item for item in load_matrix_cases() if item["id"] == "desktop-003")
        baselines = load_step_baselines(Path(__file__).with_name("e2e_step_baselines.json"))
        prompt = _append_contract_guidance("desktop task", case, baselines)
        self.assertIn("desktop_capture_state", prompt)
        self.assertIn("desktop_type", prompt)
        self.assertIn("desktop_uia_observe", prompt)
        self.assertIn("desktop_uia_set_value", prompt)
        self.assertIn("uia_observation_id", prompt)
        self.assertIn("Do not stop after observation", prompt)
        self.assertIn("runtime", prompt)
        self.assertIn("risk_level=normal", prompt)

    def test_evaluation_shell_environment_uses_runner_python_directory(self):
        runner_dir = str(Path(sys.executable).resolve().parent)
        with patch.dict(os.environ, {"PATH": "C:\\Windows\\System32"}, clear=False):
            _prepare_evaluation_tool_environment()
            self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], runner_dir)

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

    def test_desktop_fixture_session_retries_a_transient_focus_rejection(self):
        class FakeProcess:
            pid = 101

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

        activations = iter([False, True])
        with tempfile.TemporaryDirectory() as directory, \
             patch("local_real_e2e_runner.time.sleep"):
            session = _DesktopFixtureSession(
                Path(directory), launch_process=lambda _target: FakeProcess(),
                find_window=lambda _pid, _title, timeout=8.0: 202,
                activate_window=lambda _hwnd: next(activations),
                reset_document=lambda _hwnd: True,
            )
            self.assertEqual(session.acquire()[0], 202)
            session.close()

    def test_desktop_fixture_waits_for_new_window_input_queue_before_focus(self):
        class FakeProcess:
            pid = 101

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

        events = []

        def fake_sleep(_seconds):
            events.append("sleep")

        def activate(_hwnd):
            events.append("activate")
            return events.count("sleep") > 0

        with tempfile.TemporaryDirectory() as directory, \
             patch("local_real_e2e_runner.time.sleep", side_effect=fake_sleep):
            session = _DesktopFixtureSession(
                Path(directory), launch_process=lambda _target: FakeProcess(),
                find_window=lambda _pid, _title, timeout=8.0: 202,
                activate_window=activate,
                reset_document=lambda _hwnd: True,
            )
            self.assertEqual(session.acquire()[0], 202)
            session.close()

        self.assertEqual(events[:2], ["sleep", "activate"])

    def test_desktop_fixture_session_remembers_pre_fixture_foreground_as_overlay(self):
        class FakeProcess:
            pid = 101

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

        with tempfile.TemporaryDirectory() as directory, \
             patch("local_real_e2e_runner._foreground_window_handle", return_value=303):
            session = _DesktopFixtureSession(
                Path(directory), launch_process=lambda _target: FakeProcess(),
                find_window=lambda _pid, _title, timeout=8.0: 202,
                activate_window=lambda _hwnd: True,
                reset_document=lambda _hwnd: True,
            )
            session.acquire()
            overlay = session.overlay_window
            session.close()

        self.assertEqual(overlay, 303)

    def test_runner_remembers_only_the_current_host_window_as_overlay(self):
        with patch("local_real_e2e_runner._foreground_window_handle", return_value=404):
            overlays = _remember_current_host_overlay({303}, target_hwnd=202)
        self.assertEqual(overlays, {303, 404})

        with patch("local_real_e2e_runner._foreground_window_handle", return_value=202):
            overlays = _remember_current_host_overlay({303}, target_hwnd=202)
        self.assertEqual(overlays, {303})

    def test_runner_adds_only_visible_windows_from_runner_process_ancestors(self):
        with patch("local_real_e2e_runner._foreground_window_handle", return_value=404), \
             patch("local_real_e2e_runner._process_ancestry_ids", return_value={11, 22}), \
             patch("local_real_e2e_runner._visible_top_level_windows_for_processes",
                   return_value={505, 606}):
            overlays = _remember_host_process_overlays({303}, target_hwnd=202)

        self.assertEqual(overlays, {303, 404, 505, 606})

    def test_runner_does_not_add_target_or_unrelated_windows_to_host_overlays(self):
        with patch("local_real_e2e_runner._foreground_window_handle", return_value=202), \
             patch("local_real_e2e_runner._process_ancestry_ids", return_value={11}), \
             patch("local_real_e2e_runner._visible_top_level_windows_for_processes",
                   return_value={202, 707}):
            overlays = _remember_host_process_overlays({303}, target_hwnd=202)

        self.assertEqual(overlays, {303, 707})

    def test_default_desktop_fixture_launch_uses_gui_process_defaults(self):
        fake_process = object()
        with patch("local_real_e2e_runner.subprocess.Popen", return_value=fake_process) as popen:
            result = _DesktopFixtureSession._launch_default(Path("fixture.txt"))
        self.assertIs(result, fake_process)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0)

    def test_captcha_safety_cases_do_not_open_a_desktop_fixture(self):
        case = next(item for item in load_matrix_cases() if item["id"] == "safety-008")
        self.assertTrue(_is_local_browser_case(case))
        self.assertFalse(_is_desktop_case(case))

    def test_runtime_continuation_never_sends_an_empty_model_input(self):
        class FakeRuntime:
            def __init__(self):
                self.ui = Queue()

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

    def test_completed_terminal_event_wins_over_thread_timeout_during_cleanup(self):
        class FakeRuntime:
            def __init__(self):
                self.ui = Queue()

            def interrupt(self):
                return None

        runtime = FakeRuntime()

        def bounded(_runtime, _text, _timeout):
            runtime.ui.put(("task_progress", {"terminal": "completed", "verified": True}))
            return False, "provider_timeout"

        with patch("local_real_e2e_runner._run_turn_bounded", side_effect=bounded):
            metrics, failure = _run_runtime_task(
                runtime, "完成隔离任务", 5, 0, False, allow_automatic_confirmation=True,
            )

        self.assertIsNone(failure)
        self.assertTrue(metrics["completed"])
        self.assertTrue(metrics["verified"])

    def test_completed_verified_terminal_clears_recovered_focus_failure(self):
        class FakeRuntime:
            def __init__(self):
                self.ui = Queue()

            def interrupt(self):
                return None

        runtime = FakeRuntime()

        def bounded(_runtime, _text, _timeout):
            runtime.ui.put(("task_progress", {
                "terminal": "completed", "verified": True,
                "failure_kind": "desktop_focus_failure",
            }))
            return True, None

        with patch("local_real_e2e_runner._run_turn_bounded", side_effect=bounded):
            metrics, failure = _run_runtime_task(
                runtime, "完成隔离任务", 5, 0, False, allow_automatic_confirmation=True,
            )

        self.assertIsNone(failure)
        self.assertTrue(metrics["completed"])
        self.assertTrue(metrics["verified"])

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

    def test_turn_failure_preserves_transient_network_category(self):
        class FakeRuntime:
            def run_turn(self, _text, _images):
                raise RuntimeError("API network connection failed after 3 attempt(s)")

            def interrupt(self):
                return None

        ok, failure = _run_turn_bounded(FakeRuntime(), "continue the task", 1)
        self.assertFalse(ok)
        self.assertEqual(failure, "transient_network")


if __name__ == "__main__":
    unittest.main()
