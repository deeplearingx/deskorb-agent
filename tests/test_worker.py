import queue
import unittest
from unittest.mock import patch

from config import API_BASE_URL, API_MODEL
from worker import CodexWorker


class CodexWorkerTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.worker = CodexWorker(self.events, permission_mode="workspace-write")

    def drain(self):
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return out

    def test_sandbox_mapping(self):
        self.assertEqual(self.worker._sandbox(), "workspace-write")
        self.worker._permission_mode = "plan"
        self.assertEqual(self.worker._sandbox(), "read-only")

    def test_agent_message_becomes_delta(self):
        self.worker._handle_item(
            {"id": "a", "type": "agent_message", "text": "hello"},
            "item.completed",
        )
        self.assertEqual(self.drain(), [("delta", "hello")])

    def test_tool_item_is_emitted_once(self):
        item = {"id": "tool-1", "type": "command_execution", "command": "git status"}
        self.worker._handle_item(item, "item.started")
        self.worker._handle_item(item, "item.completed")
        self.assertEqual(self.drain(), [("tool", ("Shell", {"command": "git status"}))])

    def test_embedded_json_error_is_unwrapped(self):
        message = CodexWorker._event_message(
            {"message": '{"detail":"model unavailable"}'}
        )
        self.assertEqual(message, "model unavailable")

    def test_resume_command_contains_session(self):
        self.worker._session_id = "00000000-0000-0000-0000-000000000000"
        command = self.worker._build_command([])
        self.assertIn("resume", command)
        self.assertIn(self.worker._session_id, command)
        self.assertEqual(command[-1], "-")

    def test_backend_auto_uses_agent_with_key(self):
        self.worker._backend = "auto"
        with patch("worker.get_api_key", return_value=""):
            self.assertEqual(self.worker._resolved_backend(), "codex")
        with patch("worker.get_api_key", return_value="secret"):
            self.assertEqual(self.worker._resolved_backend(), "agent")

    def test_agent_backend_routes_to_independent_runtime(self):
        self.worker._backend = "agent"
        with patch.object(self.worker._agent, "run_turn") as run_turn:
            self.worker._run_turn("inspect", [])
            run_turn.assert_called_once_with("inspect", [])

    def test_ephemeral_agent_turn_routes_to_isolated_runtime(self):
        self.worker._backend = "agent"
        with patch.object(self.worker._agent, "run_ephemeral_turn") as run_turn:
            self.worker._run_turn("private Word contents", [], ephemeral=True)
        run_turn.assert_called_once_with("private Word contents", [])

    def test_ask_ephemeral_queues_a_distinct_request_kind(self):
        self.worker.ask_ephemeral("private Word contents", [])
        self.assertEqual(
            self.worker.req.get_nowait(),
            ("ask_ephemeral", ("private Word contents", [])),
        )

    def test_current_context_consent_is_carried_with_a_turn_request(self):
        self.worker.ask("分析当前桌面", [], current_context_consent=True)
        self.assertEqual(
            self.worker.req.get_nowait(),
            ("ask", ("分析当前桌面", [], True)),
        )

    def test_worker_blocks_current_context_before_any_backend_transport(self):
        self.worker._backend = "api"
        with patch.object(self.worker, "_run_api_turn") as run_api:
            self.worker._run_turn("分析当前桌面并读取日志", [], current_context_consent=False)
        run_api.assert_not_called()
        kind, payload = self.events.get_nowait()
        self.assertEqual(kind, "privacy_consent_required")
        self.assertEqual(payload["scope"], "active_window_and_local_diagnostics")

    def test_model_fallback_choice_queues_explicit_consent(self):
        self.worker.authorize_model_fallback("deepseek", share_context=True)
        self.assertEqual(self.worker.req.get_nowait(), ("authorize_model_fallback", {
            "target_id": "deepseek", "share_context": True,
        }))

    def test_agent_compact_routes_to_independent_runtime(self):
        self.worker._backend = "agent"
        with patch.object(self.worker._agent, "compact", return_value=None) as compact:
            self.worker._compact()
        compact.assert_called_once_with(force=True)

    def test_agent_startup_diagnostics_report_key_and_powershell(self):
        self.worker._backend = "agent"
        with patch("worker.get_api_key", return_value="secret"), \
             patch.object(self.worker._agent.tools, "_powershell_7_executable", return_value="pwsh"):
            diagnostics = self.worker._startup_diagnostics()
        self.assertTrue(any("API key configured" in message for message in diagnostics))
        self.assertTrue(any("PowerShell 7 available" in message for message in diagnostics))

    def test_agent_startup_diagnostics_explain_missing_requirements(self):
        self.worker._backend = "agent"
        with patch("worker.get_api_key", return_value=""), \
             patch.object(self.worker._agent.tools, "_powershell_7_executable", return_value=None):
            diagnostics = self.worker._startup_diagnostics()
        self.assertTrue(any("not configured" in message for message in diagnostics))
        self.assertTrue(any("was not found" in message for message in diagnostics))

    def test_extract_api_text(self):
        response = {"output": [{"content": [
            {"type": "output_text", "text": "hello"},
            {"type": "output_text", "text": " world"},
        ]}]}
        self.assertEqual(self.worker._extract_api_text(response), "hello world")

    def test_api_base_normalization(self):
        self.assertEqual(
            self.worker._normalize_api_base("https://example.test/v1/"),
            "https://example.test/v1",
        )

    def test_legacy_none_configuration_falls_back_to_safe_defaults(self):
        self.assertEqual(self.worker._normalize_model("None"), API_MODEL)
        self.assertEqual(self.worker._normalize_api_base("null"), API_BASE_URL)
        self.assertEqual(self.worker._normalize_proxy("None"), "")

    def test_api_stream_becomes_delta_and_keeps_local_history(self):
        class FakeResponse:
            def __iter__(self):
                return iter([
                    b'data: {"type":"response.output_text.delta","delta":"fast"}\n',
                    b'data: {"type":"response.completed","response":{"id":"resp_1"}}\n',
                ])
            def close(self):
                pass

        self.worker._model = "test-model"
        self.worker._api_base_url = "https://example.test/v1"
        with patch("worker.get_api_key", return_value="secret"), \
             patch("worker.urllib.request.urlopen", return_value=FakeResponse()):
            self.worker._run_api_turn("hello", [])
        events = self.drain()
        self.assertIn(("delta", "fast"), events)
        self.assertTrue(any(kind == "ctx" and isinstance(value, float) for kind, value in events))
        self.assertEqual(
            [(item.role, item.text) for item in self.worker._api_context.messages],
            [("user", "hello"), ("assistant", "fast")],
        )

    def test_api_history_is_included_in_next_request(self):
        self.worker._api_context.add_turn("remember CEDAR-91", "Stored.")
        prompt = self.worker._api_context.build_input("What was the code?")
        self.assertIn('"content":"remember CEDAR-91"', prompt)
        self.assertTrue(prompt.endswith("Current user message:\nWhat was the code?"))

    def test_automatic_compaction_failure_does_not_block_user_turn(self):
        self.worker._api_context.token_budget = 4000
        self.worker._api_context.recent_turns = 2
        for index in range(4):
            self.worker._api_context.add_turn(
                f"user-{index}-" + "x" * 1800,
                f"assistant-{index}-" + "y" * 1800,
            )
        with patch.object(self.worker, "_compact_api_context", side_effect=RuntimeError("offline")):
            self.worker._maybe_compact_api_context("secret")
        self.assertEqual(len(self.worker._api_context.messages), 8)


if __name__ == "__main__":
    unittest.main()
