import tempfile
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from agent_policy import Risk
from agent_runtime import AgentRuntime, ControlledTools, ReadOnlyTools


class ReadOnlyToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "notes.txt").write_text("ORCHID-472\nsecond line\n", encoding="utf-8")
        (self.root / "nested").mkdir()
        self.tools = ReadOnlyTools(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_and_reads_within_root(self):
        listed = self.tools.call("filesystem_list", {"path": ".", "max_entries": 10})
        self.assertTrue(listed["ok"])
        self.assertIn("notes.txt", [entry["name"] for entry in listed["entries"]])
        read = self.tools.call("filesystem_read_text", {"path": "notes.txt", "max_chars": 100})
        self.assertTrue(read["ok"])
        self.assertIn("ORCHID-472", read["text"])

    def test_rejects_escape_from_root(self):
        result = self.tools.call("filesystem_read_text", {"path": "..\\outside.txt", "max_chars": 100})
        self.assertFalse(result["ok"])
        self.assertIn("outside", result["error"].lower())

    def test_search_is_bounded_and_returns_evidence(self):
        result = self.tools.call("filesystem_search_text", {"path": ".", "query": "ORCHID", "max_results": 10})
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["line"], 1)

    def test_read_only_mode_clears_pending_approval(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)
        request = runtime.request_approval("shell_run", {"command": "echo test"}, Risk.DESTRUCTIVE_LOCAL, "Run test")
        self.assertIsNotNone(runtime.approvals.pending)
        self.assertIn(request.token, events.get_nowait()[1])
        runtime.set_permission_mode("plan")
        self.assertFalse(runtime.full_access)
        self.assertIsNone(runtime.approvals.pending)

    def test_controlled_write_verifies_and_respects_overwrite(self):
        tools = ControlledTools(self.root)
        first = tools.write_text({"path": "created.txt", "text": "verified", "overwrite": False})
        self.assertTrue(first["ok"])
        self.assertTrue(first["verified"])
        second = tools.write_text({"path": "created.txt", "text": "changed", "overwrite": False})
        self.assertFalse(second["ok"])

    def test_shell_runner_returns_bounded_evidence(self):
        tools = ControlledTools(self.root)
        result = tools.run_shell({"command": "Write-Output SHELL_OK; $PSVersionTable.PSEdition", "timeout_seconds": 5})
        self.assertTrue(result["ok"])
        self.assertIn("SHELL_OK", result["stdout"])
        self.assertIn("Core", result["stdout"])

    def test_open_and_launch_are_execution_requests(self):
        self.assertTrue(AgentRuntime._execution_requested("打开 Google Chrome"))
        self.assertTrue(AgentRuntime._execution_requested("launch Edge"))

    def test_attachment_note_is_removed_from_task_summary(self):
        text = "[Attached: a live screenshot of my screen — monitor 1 (primary).]\n\n打开QQ，发送消息"
        self.assertEqual(AgentRuntime._clean_task_text(text), "打开QQ，发送消息")

    def test_application_launcher_uses_resolved_executable(self):
        tools = ControlledTools(self.root)
        class FakeProcess:
            pid = 4321
        with patch.object(tools, "_resolve_application", return_value="chrome.exe"), \
             patch("agent_runtime.subprocess.Popen", return_value=FakeProcess()) as popen:
            result = tools.launch_application({"application": "chrome"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 4321)
        popen.assert_called_once()

    def test_qq_resolver_finds_start_menu_shortcut(self):
        start = self.root / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "腾讯软件" / "QQ"
        start.mkdir(parents=True)
        shortcut = start / "QQ.lnk"
        shortcut.write_bytes(b"shortcut")
        with patch("agent_runtime.shutil.which", return_value=None), \
             patch.dict("agent_runtime.os.environ", {"PROGRAMDATA": str(self.root),
                                                      "APPDATA": str(self.root / "missing"),
                                                      "PROGRAMFILES": "", "PROGRAMFILES(X86)": "",
                                                      "LOCALAPPDATA": ""}, clear=False):
            self.assertEqual(ControlledTools._resolve_application("qq"), str(shortcut))

    def test_pending_confirmation_is_not_rendered_as_a_duplicate_prompt(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)
        request = runtime.request_approval("application_launch", {"application": "edge"},
                                           Risk.EXTERNAL_OR_ELEVATED, "Launch application: edge")
        events.get_nowait()  # The original approval is displayed once when it is created.
        with patch("agent_runtime.get_api_key", return_value="test-key"):
            runtime.run_turn("打开 Edge", [])
        kind, message = events.get_nowait()
        self.assertEqual(kind, "system")
        self.assertIn(request.token, message)
        self.assertNotIn("Confirmation required", message)

    def test_high_risk_marker_and_task_authorization(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        self.assertFalse(runtime._high_risk_call("desktop_type", {"risk_level": "normal"}))
        self.assertTrue(runtime._high_risk_call("desktop_type", {"risk_level": "high"}))
        runtime._task_authorized_until = __import__("time").monotonic() + 10
        self.assertTrue(runtime._task_authorized())
        runtime.set_permission_mode("plan")
        self.assertFalse(runtime._task_authorized())

    def test_desktop_observation_is_appended_after_action(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        with patch.object(runtime.desktop, "capture_state", return_value={"ok": True, "snapshot_id": "NEXT"}), \
             patch.object(runtime.desktop, "capture_image_data_url", return_value="data:image/jpeg;base64,AA"), \
             patch("agent_runtime.time.sleep"):
            transcript = runtime._append_desktop_observation([], "application_launch")
        self.assertEqual(transcript[-1]["role"], "user")
        self.assertEqual(transcript[-1]["content"][-1]["type"], "input_image")
        self.assertIn("NEXT", transcript[-1]["content"][0]["text"])

    def test_one_confirmation_covers_multiple_normal_desktop_steps(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)
        responses = iter([
            {"output": [{"type": "function_call", "call_id": "call-1",
                         "name": "application_launch", "arguments": '{"application":"edge"}'}]},
            {"output": [{"type": "function_call", "call_id": "call-2",
                         "name": "desktop_hotkey",
                         "arguments": '{"snapshot_id":"NEXT","keys":["ctrl","l"],"risk_level":"normal","risk_reason":"ordinary navigation"}'}]},
            {"output_text": "TASK_DONE", "output": []},
        ])
        runtime._request = lambda payload, key: next(responses)
        with patch("agent_runtime.get_api_key", return_value="test-key"), \
             patch.object(runtime, "_run_local_tool", return_value={"ok": True}), \
             patch.object(runtime, "_append_desktop_observation", side_effect=lambda transcript, name: transcript):
            runtime.run_turn("打开 Edge 并聚焦地址栏", [])
            first_events = []
            while not events.empty():
                first_events.append(events.get_nowait())
            approval = next(payload for kind, payload in first_events if kind == "approval")
            token = approval.split("确认 ", 1)[1].splitlines()[0]
            runtime.run_turn("确认 " + token, [])
        all_events = first_events
        while not events.empty():
            all_events.append(events.get_nowait())
        self.assertEqual(sum(1 for kind, _ in all_events if kind == "approval"), 1)
        self.assertTrue(any(kind == "delta" and payload == "TASK_DONE" for kind, payload in all_events))
        self.assertFalse(runtime._task_authorized())

    def test_agent_context_uses_configured_budget_and_compacts(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root,
                               context_tokens=4000, recent_turns=2)
        for index in range(4):
            runtime.context.add_turn("u" + str(index) + "-" + "x" * 1800,
                                     "a" + str(index) + "-" + "y" * 1800)
        self.assertEqual(runtime.context.token_budget, 4000)
        runtime._request = lambda payload, key: {"output_text": "Remembered early turns."}
        meta = runtime._compact_context("test-key", force=False)
        self.assertIsNotNone(meta)
        self.assertEqual(runtime.context.summary, "Remembered early turns.")
        self.assertEqual(len(runtime.context.messages), 4)

    def test_ephemeral_turn_does_not_mutate_context(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.context.add_turn("remember ordinary turn", "Ordinary answer")
        before = runtime.context.build_input("next question")
        request = Mock(return_value={"output_text": "Private answer", "output": []})
        runtime._request = request

        with patch("agent_runtime.get_api_key", return_value="test-key"):
            runtime.run_ephemeral_turn("private Word contents", [])

        self.assertEqual(runtime.context.build_input("next question"), before)
        payload = request.call_args.args[0]
        self.assertEqual(payload["input"][0]["content"][0]["text"], "private Word contents")



if __name__ == "__main__":
    unittest.main()

