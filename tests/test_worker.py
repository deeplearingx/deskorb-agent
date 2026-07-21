import os
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

    def test_cmd_launcher_handles_a_codex_path_with_spaces(self):
        self.worker._codex = r"C:\Users\Hu yubin\AppData\Roaming\npm\codex.cmd"
        with patch("worker.os.name", "nt"):
            self.assertEqual(
                self.worker._launch_prefix(),
                [
                    os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", "call",
                    r"C:\Users\Hu yubin\AppData\Roaming\npm\codex.cmd",
                ],
            )

    def test_backend_auto_uses_api_only_with_key(self):
        self.worker._backend = "auto"
        with patch("worker.get_api_key", return_value=""):
            self.assertEqual(self.worker._resolved_backend(), "codex")
        with patch("worker.get_api_key", return_value="secret"):
            self.assertEqual(self.worker._resolved_backend(), "api")

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
        self.assertIn(("delta", "fast"), self.drain())
        self.assertEqual(
            [(item.role, item.text) for item in self.worker._api_context.messages],
            [("user", "hello"), ("assistant", "fast")],
        )

    def test_api_history_is_included_in_next_request(self):
        self.worker._api_context.add_turn("remember CEDAR-91", "Stored.")
        prompt = self.worker._api_context.build_input("What was the code?")
        self.assertIn('"content":"remember CEDAR-91"', prompt)
        self.assertTrue(prompt.endswith("Current user message:\nWhat was the code?"))

    def test_ephemeral_api_turn_bypasses_and_preserves_local_conversation_context(self):
        class FakeResponse:
            def __iter__(self):
                return iter([
                    b'data: {"type":"response.output_text.delta","delta":"private answer"}\n',
                    b'data: {"type":"response.completed","response":{"id":"resp_1"}}\n',
                ])
            def close(self):
                pass

        self.worker._api_context.add_turn("remember CEDAR-91", "Stored.")
        self.worker._model = "test-model"
        self.worker._api_base_url = "https://example.test/v1"
        with patch("worker.get_api_key", return_value="secret"), \
                patch("worker.urllib.request.urlopen", return_value=FakeResponse()) as open_request:
            self.worker._run_api_turn("[TEMP WORD]\nsecret document", [], ephemeral=True)

        payload = __import__("json").loads(open_request.call_args.args[0].data.decode("utf-8"))
        request_text = payload["input"][0]["content"][0]["text"]
        self.assertIn("secret document", request_text)
        self.assertNotIn("CEDAR-91", request_text)
        self.assertEqual(
            [(item.role, item.text) for item in self.worker._api_context.messages],
            [("user", "remember CEDAR-91"), ("assistant", "Stored.")],
        )

    def test_ephemeral_codex_turn_uses_a_separate_thread_without_replacing_chat_session(self):
        self.worker._codex = "codex"
        self.worker._session_id = "normal-chat-thread"
        self.worker._start_server = unittest.mock.Mock()
        self.worker._send = unittest.mock.Mock(side_effect=[10, 11])
        self.worker._wait_for = unittest.mock.Mock(side_effect=[
            {"result": {"thread": {"id": "temporary-word-thread"}}},
            {"result": {"turn": {"id": "temporary-turn"}}},
            {},
        ])

        self.worker._run_codex_turn("[TEMP WORD]\nsecret document", [], ephemeral=True)

        self.assertEqual(self.worker._session_id, "normal-chat-thread")
        self.assertEqual(self.worker._send.call_args_list[0].args[0], "thread/start")
        self.assertEqual(self.worker._send.call_args_list[1].args[0], "turn/start")
        self.assertEqual(
            self.worker._send.call_args_list[1].args[1]["threadId"], "temporary-word-thread"
        )

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
