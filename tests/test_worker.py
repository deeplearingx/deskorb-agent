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

    def test_api_backend_does_not_send_images_to_text_only_provider(self):
        self.worker._backend = "api"
        with patch.object(self.worker, "_run_api_turn") as run_api:
            self.worker._run_turn("请回答这个问题", ["C:/tmp/auto-shot.png"])
        run_api.assert_called_once_with(
            "请回答这个问题", [], ephemeral=False, office_plan=False, office_generation=None,
        )

    def test_api_backend_routes_officecli_tasks_to_mcp_agent_runtime(self):
        self.worker._backend = "api"
        with patch.object(self.worker._agent, "_mcp_servers_for_task", return_value=("officecli",)), \
             patch.object(self.worker, "_run_agent_turn") as run_agent, \
             patch.object(self.worker, "_run_api_turn") as run_api:
            self.worker._run_turn(
                "请使用 OfficeCLI 创建一个 docx 文件", ["C:/tmp/auto-shot.png"],
            )
        run_agent.assert_called_once_with(
            "请使用 OfficeCLI 创建一个 docx 文件", [], ephemeral=False,
            office_plan=False, office_context=False, office_generation=None,
        )
        run_api.assert_not_called()

    def test_api_backend_routes_pending_agent_confirmation_to_mcp_runtime(self):
        self.worker._backend = "api"
        self.worker._agent.approvals.pending = object()
        with patch.object(self.worker, "_run_agent_turn") as run_agent, \
             patch.object(self.worker, "_run_api_turn") as run_api:
            self.worker._run_turn("确认 ABC123", [])
        run_agent.assert_called_once()
        run_api.assert_not_called()

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

    def test_ask_office_plan_queues_a_dedicated_request_kind(self):
        self.worker.ask_office_plan("private Office snapshot")
        self.assertEqual(self.worker.req.get_nowait(), ("ask_office_plan", "private Office snapshot"))

    def test_ask_office_context_queues_question_and_isolated_prompt(self):
        self.worker.ask_office_context("撤回刚才的修改", "private Office snapshot and history")
        self.assertEqual(
            self.worker.req.get_nowait(),
            ("ask_office_context", ("撤回刚才的修改", "private Office snapshot and history", None)),
        )

    def test_office_plan_agent_turn_uses_no_tools_runtime_and_terminal_event(self):
        self.worker._backend = "agent"
        with patch.object(self.worker._agent, "run_office_plan_turn") as run_turn:
            self.worker._run_turn("private Office snapshot", [], ephemeral=True, office_plan=True)
        run_turn.assert_called_once_with("private Office snapshot")
        self.assertIn(("office_plan_done", None), self.drain())

    def test_office_context_agent_turn_uses_persistent_context_runtime(self):
        self.worker._backend = "agent"
        with patch.object(self.worker._agent, "run_office_context_turn") as run_turn:
            self.worker._run_turn(
                "question and private Office context", [],
                ephemeral=True, office_plan=True, office_context=True, office_generation=7,
            )
        run_turn.assert_called_once_with("question and private Office context", event_token=7)
        self.assertIn(("office_plan_done", 7), self.drain())

    def test_office_delta_is_tagged_with_generation(self):
        self.worker._emit_delta("private Office response", 7)
        self.assertEqual(self.drain(), [("office_delta", (7, "private Office response"))])

    def test_office_plan_codex_turn_uses_temporary_read_only_thread(self):
        self.worker._codex = "codex"
        self.worker._session_id = "normal-thread"
        sent = []

        def send(method, payload):
            sent.append((method, payload))
            return len(sent)

        responses = iter([
            {"result": {"thread": {"id": "temporary-thread"}}},
            {"result": {"turn": {"id": "temporary-turn"}}},
            {"method": "turn/completed", "params": {"threadId": "temporary-thread"}},
        ])
        with patch.object(self.worker, "_start_server"), \
                patch.object(self.worker, "_send", side_effect=send), \
                patch.object(self.worker, "_wait_for", side_effect=lambda predicate, timeout: next(responses)):
            self.worker._run_codex_turn("private Office snapshot", [], ephemeral=True, office_plan=True)

        self.assertEqual(self.worker._session_id, "normal-thread")
        self.assertEqual(sent[0][0], "thread/start")
        self.assertEqual(sent[0][1]["sandbox"], "read-only")

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

