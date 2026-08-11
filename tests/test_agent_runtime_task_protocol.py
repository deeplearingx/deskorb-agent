import tempfile
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from agent_runtime import AgentRuntime
from task_runtime import InMemoryTaskJournal, TASK_STATUS_COMPLETED, TASK_STATUS_WAITING_VERIFICATION


class AgentRuntimeTaskProtocolTests(unittest.TestCase):
    def test_verified_tool_result_publishes_terminal_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Queue()
            journal = InMemoryTaskJournal()
            runtime = AgentRuntime(events, "fixture-model", "https://example.test/v1",
                                   working_dir=Path(directory), task_journal=journal)
            runtime._request = Mock(side_effect=[
                {"output": [{"type": "function_call", "call_id": "check",
                             "name": "shell_run", "arguments": "{\"command\":\"Write-Output OK\"}"}]},
                {"output_text": "检查完成", "output": []},
            ])
            with patch("agent_runtime.get_api_key", return_value="fixture-key"), \
                 patch.object(runtime, "_run_local_tool", return_value={"ok": True, "exit_code": 0}):
                runtime.run_turn("运行安全检查并报告结果", [])

            progress = [value for kind, value in list(events.queue)
                        if kind == "task_progress" and isinstance(value, dict) and value.get("terminal")]
            self.assertEqual(len(progress), 1)
            self.assertEqual(progress[0]["terminal"], "completed")
            self.assertTrue(progress[0]["verified"])
            task = journal.task(progress[0]["task_id"])
            self.assertEqual(task["status"], TASK_STATUS_COMPLETED)
            self.assertIsNone(runtime._task_state)
            tool_results = [value for kind, value in list(events.queue) if kind == "tool_result"]
            self.assertEqual(tool_results, [{"tool": "shell_run", "ok": True,
                                             "verified": False, "high_risk": False}])

    def test_unverified_action_publishes_waiting_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Queue()
            journal = InMemoryTaskJournal()
            runtime = AgentRuntime(events, "fixture-model", "https://example.test/v1",
                                   working_dir=Path(directory), task_journal=journal)
            runtime._request = Mock(side_effect=[
                {"output": [{"type": "function_call", "call_id": "write",
                             "name": "filesystem_write", "arguments": "{\"path\":\"tmp.txt\",\"text\":\"x\",\"overwrite\":true}"}]},
                {"output_text": "已写入", "output": []},
            ])
            with patch("agent_runtime.get_api_key", return_value="fixture-key"), \
                 patch.object(runtime, "_run_local_tool", return_value={"ok": True}):
                runtime.run_turn("创建临时文件", [])

            progress = [value for kind, value in list(events.queue)
                        if kind == "task_progress" and isinstance(value, dict) and value.get("terminal")]
            self.assertEqual(progress[-1]["terminal"], "waiting_verification")
            self.assertFalse(progress[-1]["verified"])
            self.assertEqual(journal.task(progress[-1]["task_id"])["status"], TASK_STATUS_WAITING_VERIFICATION)

    def test_empty_user_input_is_blocked_without_model_request(self):
        events = Queue()
        runtime = AgentRuntime(events, "fixture-model", "https://example.test/v1")
        request = Mock()
        runtime._request = request

        with patch("agent_runtime.get_api_key", return_value="fixture-key"):
            runtime.run_turn("   ", [])

        request.assert_not_called()
        progress = [value for kind, value in list(events.queue)
                    if kind == "task_progress" and isinstance(value, dict)]
        self.assertEqual(progress[-1]["terminal"], "blocked")
        self.assertEqual(progress[-1]["failure_kind"], "empty_model_input")
        self.assertFalse(progress[-1]["verified"])

    def test_empty_office_context_is_blocked_without_model_request(self):
        events = Queue()
        runtime = AgentRuntime(events, "fixture-model", "https://example.test/v1")
        request = Mock()
        runtime._request = request

        with patch("agent_runtime.get_api_key", return_value="fixture-key"):
            runtime.run_office_context_turn(" ", "")

        request.assert_not_called()
        progress = [value for kind, value in list(events.queue)
                    if kind == "task_progress" and isinstance(value, dict)]
        self.assertEqual(progress[-1]["terminal"], "blocked")
        self.assertEqual(progress[-1]["failure_kind"], "empty_model_input")

    def test_malformed_function_arguments_publish_bounded_tool_result(self):
        events = Queue()
        runtime = AgentRuntime(events, "fixture-model", "https://example.test/v1")
        runtime._request = Mock(side_effect=[
            {"output": [{"type": "function_call", "call_id": "bad",
                         "name": "shell_run", "arguments": "not-json"}]},
            {"output_text": "无法执行", "output": []},
        ])

        with patch("agent_runtime.get_api_key", return_value="fixture-key"):
            runtime.run_turn("运行安全检查并报告结果", [])

        tool_results = [value for kind, value in list(events.queue) if kind == "tool_result"]
        self.assertEqual(tool_results, [{"tool": "shell_run", "ok": False,
                                         "verified": False, "high_risk": False}])


if __name__ == "__main__":
    unittest.main()
