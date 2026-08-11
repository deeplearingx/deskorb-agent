import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import e2e_current_diagnosis_probe
from task_runtime import InMemoryTaskJournal


class CurrentDiagnosisConsentTests(unittest.TestCase):
    def test_probe_refuses_without_explicit_current_desktop_consent(self):
        output = io.StringIO()
        with patch.dict(e2e_current_diagnosis_probe.os.environ, {}, clear=False), \
             patch.object(e2e_current_diagnosis_probe.os, "environ", {"PATH": "fixture"}), \
             contextlib.redirect_stdout(output):
            result = e2e_current_diagnosis_probe.main([])
        self.assertEqual(result, 4)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "consent_required")
        self.assertTrue(payload["requires_user_confirmation"])

    def test_diagnostic_prompt_discovers_logs_in_the_configured_working_directory(self):
        root = Path("C:/DeskOrb/runtime")
        with patch.object(e2e_current_diagnosis_probe, "WORKING_DIR", root), \
             patch.dict(e2e_current_diagnosis_probe.os.environ, {}, clear=True):
            prompt = e2e_current_diagnosis_probe._build_diagnostic_task(root)
        self.assertIn("filesystem_list", prompt)
        self.assertIn("configured working directory", prompt)
        self.assertNotIn("deskorb-agent-debug.log", prompt)

    def test_diagnostic_prompt_uses_a_safe_relative_configured_log_path(self):
        root = Path("C:/DeskOrb/runtime")
        with patch.dict(e2e_current_diagnosis_probe.os.environ,
                        {"DESKORB_AGENT_DEBUG_LOG": "logs/agent.log"}, clear=True):
            prompt = e2e_current_diagnosis_probe._build_diagnostic_task(root)
        self.assertIn("logs/agent.log", prompt)
        self.assertIn("不要读取工作目录之外", prompt)

    def test_diagnostic_prompt_accepts_an_explicit_scoped_log_path(self):
        root = Path("C:/DeskOrb/runtime")
        prompt = e2e_current_diagnosis_probe._build_diagnostic_task(root, Path("logs/agent.log"))
        self.assertIn("logs/agent.log", prompt)
        self.assertNotIn("DESKORB_AGENT_DEBUG_LOG", prompt)

    def test_diagnostic_rejects_an_explicit_log_path_outside_working_directory(self):
        root = Path("C:/DeskOrb/runtime")
        prompt = e2e_current_diagnosis_probe._build_diagnostic_task(
            root, Path("C:/Users/Administrator/agent.log"))
        self.assertIn("outside working directory", prompt)

    def test_diagnostic_cli_passes_explicit_working_and_log_paths_to_runtime(self):
        class CapturingRuntime:
            created = []

            def __init__(self, events, *_args, **kwargs):
                self.events = events
                self.mcp = None
                self.kwargs = kwargs
                self.task = ""
                self.created.append(self)

            def run_turn(self, task, _images):
                self.task = task
                self.events.put(("delta", "Evidence: active window and configured log were inspected."))
                self.events.put(("tool", ("Active window", {})))
                self.events.put(("tool", ("List files", {})))
                self.events.put(("tool", ("Read file", {})))

        with patch.object(e2e_current_diagnosis_probe, "AgentRuntime", CapturingRuntime), \
             patch.dict(e2e_current_diagnosis_probe.os.environ, {}, clear=True), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = e2e_current_diagnosis_probe.main([
                "--confirm-current-desktop", "--working-dir", "D:/DeskOrb/runtime",
                "--log-path", "logs/agent.log",
            ])
        self.assertEqual(result, 0)
        runtime = CapturingRuntime.created[-1]
        self.assertEqual(runtime.kwargs["working_dir"], Path("D:/DeskOrb/runtime"))
        self.assertIn("logs/agent.log", runtime.task)

    def test_diagnostic_timeout_is_reported_without_claiming_environment_ready(self):
        import threading

        class HangingRuntime:
            def __init__(self, events, *_args, **kwargs):
                self.events = events
                self.mcp = None
                self._stop = threading.Event()

            def run_turn(self, _task, _images):
                self._stop.wait(10)

            def interrupt(self):
                self._stop.set()

        with patch.object(e2e_current_diagnosis_probe, "AgentRuntime", HangingRuntime), \
             patch.dict(e2e_current_diagnosis_probe.os.environ, {}, clear=True), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = e2e_current_diagnosis_probe.main([
                "--confirm-current-desktop", "--working-dir", "D:/DeskOrb/runtime",
                "--timeout-seconds", "1",
            ])
        self.assertEqual(result, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "provider_timeout")
        self.assertFalse(payload["ok"])

    def test_timeout_with_complete_missing_environment_evidence_is_not_ready(self):
        import threading

        class SlowEvidenceRuntime:
            def __init__(self, events, *_args, **kwargs):
                self.events = events
                self.mcp = None
                self._stop = threading.Event()

            def run_turn(self, _task, _images):
                self.events.put(("delta", "No capturable foreground window. The log is unavailable or not configured."))
                self.events.put(("tool", ("Active window", {})))
                self.events.put(("tool", ("List files", {})))
                self.events.put(("tool", ("Read file", {})))
                self._stop.wait(10)

            def interrupt(self):
                self._stop.set()

        with patch.object(e2e_current_diagnosis_probe, "AgentRuntime", SlowEvidenceRuntime), \
             patch.dict(e2e_current_diagnosis_probe.os.environ, {}, clear=True), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = e2e_current_diagnosis_probe.main([
                "--confirm-current-desktop", "--working-dir", "D:/DeskOrb/runtime",
                "--timeout-seconds", "1",
            ])
        self.assertEqual(result, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "environment_not_ready")
        self.assertEqual(payload["provider_failure"], "provider_timeout")
        self.assertFalse(payload["ok"])

    def test_current_diagnosis_uses_an_in_memory_journal(self):
        class CapturingRuntime:
            created = []

            def __init__(self, events, *_args, **kwargs):
                self.events = events
                self.mcp = None
                self.kwargs = kwargs
                self.created.append(self)

            def run_turn(self, _task, _images):
                self.events.put(("delta", "Evidence: active window and configured log were inspected."))
                self.events.put(("tool", ("Active window", {})))
                self.events.put(("tool", ("List files", {})))
                self.events.put(("tool", ("Read file", {})))

        with patch.object(e2e_current_diagnosis_probe, "AgentRuntime", CapturingRuntime), \
             patch.object(e2e_current_diagnosis_probe, "WORKING_DIR", Path("C:/DeskOrb/runtime")), \
             patch.dict(e2e_current_diagnosis_probe.os.environ, {}, clear=True), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = e2e_current_diagnosis_probe.main(["--confirm-current-desktop"])
        self.assertEqual(result, 0)
        journal = CapturingRuntime.created[-1].kwargs["task_journal"]
        self.assertIsInstance(journal, InMemoryTaskJournal)
        self.assertIsNone(journal.path)
        self.assertIsInstance(CapturingRuntime.created[-1]._model_health_store,
                              e2e_current_diagnosis_probe._EphemeralHealthStore)
        self.assertEqual(json.loads(output.getvalue())["status"], "ready")

    def test_diagnostic_marks_denied_or_missing_environment_as_not_ready(self):
        ready, status = e2e_current_diagnosis_probe._diagnostic_environment_ready(
            "未获得可捕获的窗口信息；系统拒绝访问工作目录，无法读取工作目录内容。",
            ["Active window", "List files"],
        )
        self.assertFalse(ready)
        self.assertEqual(status, "environment_not_ready")

    def test_diagnostic_marks_english_missing_window_or_log_as_not_ready(self):
        ready, status = e2e_current_diagnosis_probe._diagnostic_environment_ready(
            "Active-window metadata returned no capturable foreground window. "
            "The log is unavailable or not configured.",
            ["Active window", "List files"],
        )
        self.assertFalse(ready)
        self.assertEqual(status, "environment_not_ready")


if __name__ == "__main__":
    unittest.main()
