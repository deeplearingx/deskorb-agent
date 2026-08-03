import tempfile
import unittest
import io
import urllib.error
from pathlib import Path
from queue import Queue
from unittest.mock import Mock, patch

from agent_policy import Risk
from agent_runtime import AgentRuntime, ControlledTools, ReadOnlyTools
from model_registry import FallbackTarget


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

    def test_current_context_diagnostic_is_blocked_without_runtime_consent(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)

        runtime.run_turn("分析当前桌面并读取 DeskOrb 日志", [])

        kind, payload = events.get_nowait()
        self.assertEqual(kind, "privacy_consent_required")
        self.assertEqual(payload["scope"], "active_window_and_local_diagnostics")

    def test_mcp_router_activates_only_relevant_default_server(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)

        self.assertEqual(runtime._mcp_servers_for_task("打开浏览器搜索淘宝 T 恤"), ("playwright",))
        self.assertEqual(runtime._mcp_servers_for_task("启用 PowerToys 保持唤醒"), ("powertoys",))
        self.assertEqual(runtime._mcp_servers_for_task("打开记事本并输入 hello"), ())

    def test_mcp_router_uses_custom_intent_keywords_and_discovery_tool(self):
        class FakeMcp:
            available_servers = ("knowledge",)

            def server_catalog(self):
                return [{"name": "knowledge", "description": "Search internal documentation",
                         "keywords": ["wiki", "知识库"]}]

            def schemas(self, servers):
                return [{"type": "function", "name": "mcp_knowledge_search", "parameters": {}}]

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        self.assertEqual(runtime._mcp_servers_for_task("查一下公司知识库"), ("knowledge",))
        chooser = next(item for item in runtime._available_schemas("帮我找内部文档")
                       if item["name"] == "mcp_enable_server")
        self.assertIn("knowledge", chooser["description"])
        result = runtime._run_local_tool("mcp_enable_server", {"server_name": "knowledge"})
        self.assertTrue(result["ok"])
        self.assertIn("mcp_knowledge_search", [item["name"] for item in runtime._available_schemas("帮我找内部文档")])

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
        self.assertTrue(runtime._high_risk_call("desktop_clipboard_read_text", {}))
        self.assertTrue(runtime._high_risk_call("window_control", {"action": "close"}))
        runtime._task_authorized_until = __import__("time").monotonic() + 10
        self.assertTrue(runtime._task_authorized())
        runtime.set_permission_mode("plan")
        self.assertFalse(runtime._task_authorized())

    def test_task_lease_limits_normal_actions_to_authorized_capabilities(self):
        class FakeMcp:
            available_servers = ("playwright", "powertoys")

            def owns(self, name):
                return name.startswith("mcp_")

            def is_action(self, _name):
                return True

            def server_for(self, name):
                return "playwright" if "playwright" in name else "powertoys"

            def server_catalog(self):
                return []

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        runtime._grant_task_lease("打开浏览器搜索淘宝 T 恤", "application_launch")

        self.assertTrue(runtime._task_authorized_for("desktop_hotkey"))
        self.assertTrue(runtime._task_authorized_for("mcp_playwright_browser_click"))
        self.assertFalse(runtime._task_authorized_for("mcp_powertoys_apply_settings"))

    def test_browser_task_space_blocks_actions_during_manual_handoff(self):
        class FakeMcp:
            available_servers = ("playwright",)

            def owns(self, name):
                return name in {"mcp_playwright_browser_click", "mcp_playwright_browser_snapshot"}

            def is_action(self, _name):
                return True

            def server_for(self, _name):
                return "playwright"

            def server_catalog(self):
                return []

            def call(self, _name, _arguments):
                return {"ok": True, "content": [{"type": "text", "text": "clicked"}]}

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        runtime._grant_task_lease("打开浏览器搜索淘宝 T 恤", "application_launch")
        space = runtime.browser_spaces.for_task(runtime._task_id)
        self.assertIsNotNone(space)
        runtime.browser_spaces.hand_off(runtime._task_id)
        blocked = runtime._run_local_tool("mcp_playwright_browser_click", {"ref": "e1"})
        self.assertFalse(blocked["ok"])
        self.assertIn("user control", blocked["error"])
        runtime.browser_spaces.take_over(runtime._task_id, user_confirmed=True)
        allowed = runtime._run_local_tool("mcp_playwright_browser_click", {"ref": "e1"})
        self.assertTrue(allowed["ok"])
        self.assertIsNotNone(runtime.browser_spaces.for_task(runtime._task_id).checkpoint)

    def test_browser_action_batch_executes_only_mapped_semantic_steps(self):
        class FakeMcp:
            available_servers = ("playwright",)

            def owns(self, name):
                return name in {"mcp_playwright_browser_click", "mcp_playwright_browser_snapshot"}

            def is_action(self, name):
                return name == "mcp_playwright_browser_click"

            def server_for(self, _name):
                return "playwright"

            def schemas(self, _servers):
                return []

            def find_tool(self, _server, suffixes):
                if "browser_click" in suffixes:
                    return "mcp_playwright_browser_click"
                if "browser_snapshot" in suffixes:
                    return "mcp_playwright_browser_snapshot"
                return None

            def call(self, name, arguments):
                if name == "mcp_playwright_browser_snapshot":
                    return {"ok": True, "verification": {"passed": True}, "arguments": arguments}
                return {"ok": name == "mcp_playwright_browser_click", "arguments": arguments}

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        runtime._grant_task_lease("打开浏览器并点击搜索框", "browser_action_batch")
        result = runtime._run_local_tool("browser_action_batch", {
            "actions": [{"action": "click_ref", "arguments": {"ref": "e1"}}],
            "risk_level": "normal", "risk_reason": "ordinary navigation",
        })
        self.assertTrue(result["ok"])
        self.assertFalse(result["verification"]["passed"])
        self.assertTrue(runtime.web_recipes.save_verified(
            "search-v1", origin="https://example.test", kind="search",
            locators=[{"ref": "e1"}], preconditions={}, verifier={"required_fields": ["title"]},
            validators_passed=True))
        reused = runtime._run_local_tool("browser_action_batch", {
            "actions": [{"action": "click_ref", "arguments": {"ref": "e1"}},
                        {"action": "verify", "arguments": {}}],
            "recipe_id": "search-v1", "current_origin": "https://example.test",
            "risk_level": "normal", "risk_reason": "ordinary navigation",
        })
        self.assertEqual(reused["recipe_version"], 1)
        wrong_origin = runtime._run_local_tool("browser_action_batch", {
            "actions": [{"action": "click_ref", "arguments": {"ref": "e1"}}],
            "recipe_id": "search-v1", "current_origin": "https://other.test",
            "risk_level": "normal", "risk_reason": "ordinary navigation",
        })
        self.assertFalse(wrong_origin["ok"])
        rejected = runtime._run_local_tool("browser_action_batch", {
            "actions": [{"action": "javascript", "arguments": {"code": "alert(1)"}}],
            "risk_level": "normal", "risk_reason": "ordinary navigation",
        })
        self.assertFalse(rejected["ok"])

    def test_resume_task_requires_confirmation_and_reobserves_after_restart(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        task_id = runtime.task_journal.start("打开 Edge 并检查窗口")
        runtime.task_journal.set_status(task_id, "active", capabilities={"desktop_control"})
        runtime.task_journal.checkpoint(task_id, {"last_tool": "application_launch", "last_node_id": 1})
        pending = runtime.resume_task(task_id)
        self.assertTrue(pending["requires_confirmation"])
        runtime._request = lambda _payload, _key: {"output_text": "已重新检查窗口状态", "output": []}
        with patch("agent_runtime.get_api_key", return_value="test-key"):
            resumed = runtime.resume_task(task_id, user_confirmed=True)
        self.assertTrue(resumed["ok"])
        self.assertIsNone(runtime._task_id)

    def test_health_check_records_only_latency_and_provider_metadata(self):
        runtime = AgentRuntime(Queue(), "fixture-model", "https://example.test/v1", working_dir=self.root)
        runtime._request = lambda _payload, _key: {"output_text": "OK", "output": []}
        with patch("agent_runtime.get_api_key", return_value="fixture-key"):
            result = runtime.health_check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["history"]["samples"], 1)
        self.assertNotIn("fixture-key", str(result))

    def test_model_fallback_requires_confirmation_and_context_consent(self):
        runtime = AgentRuntime(Queue(), "primary", "https://example.test/v1", working_dir=self.root,
                               fallback_targets=[FallbackTarget("fixture", "openai-compatible", "backup",
                                                                "http://127.0.0.1:8000/v1")])
        with patch("agent_runtime.get_provider_api_key", return_value="backup-key"):
            pending = runtime.authorize_model_fallback("fixture")
            self.assertTrue(pending["requires_confirmation"])
            runtime.context.add_turn("记住这个账号", "已记住")
            blocked = runtime.authorize_model_fallback("fixture", user_confirmed=True)
            self.assertTrue(blocked["requires_context_consent"])
            selected = runtime.authorize_model_fallback("fixture", user_confirmed=True, share_context=True)
        self.assertTrue(selected["ok"])
        self.assertEqual(runtime.adapter.provider, "openai-compatible")
        self.assertTrue(selected["context_shared"])

    def test_model_request_failure_announces_but_does_not_switch_fallback(self):
        events = Queue()
        runtime = AgentRuntime(events, "primary", "https://example.test/v1", working_dir=self.root,
                               fallback_targets=[FallbackTarget("fixture", "openai-compatible", "backup",
                                                                "http://127.0.0.1:8000/v1")])
        with patch("agent_runtime.get_api_key", return_value="primary-key"), \
             patch.object(runtime, "_run_turn", side_effect=RuntimeError("API HTTP 503: unavailable")):
            with self.assertRaises(RuntimeError):
                runtime.run_turn("hello", [])
        events_list = []
        while not events.empty():
            events_list.append(events.get_nowait())
        fallback = next(payload for kind, payload in events_list if kind == "model_fallback_available")
        self.assertTrue(fallback["requires_confirmation"])
        self.assertFalse(fallback["automatic_switch"])
        self.assertEqual(runtime.adapter.provider, "responses")

    def test_login_and_qr_states_use_the_same_manual_handoff_boundary(self):
        class FakeMcp:
            def owns(self, name):
                return name == "mcp_playwright_browser_snapshot"

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        self.assertEqual(runtime._captcha_marker("mcp_playwright_browser_snapshot", {
            "ok": True, "content": [{"type": "text", "text": "请登录后继续"}]
        }), "请登录后继续")
        self.assertEqual(runtime._captcha_marker("mcp_playwright_browser_snapshot", {
            "ok": True, "content": [{"type": "text", "text": "扫描二维码登录"}]
        }), "扫描二维码")

    def test_loading_browser_state_blocks_click_until_wait_or_snapshot(self):
        class FakeMcp:
            def owns(self, name):
                return name == "mcp_playwright_browser_click"

            def is_action(self, _name):
                return True

            def server_for(self, _name):
                return "playwright"

            def call(self, _name, _arguments):
                raise AssertionError("click must be blocked while loading")

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        runtime._browser_last_state = "loading"
        result = runtime._run_local_tool("mcp_playwright_browser_click", {"ref": "e1"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["browser_state"], "loading")

    def test_browser_batch_stops_after_first_failed_or_unsupported_step(self):
        class FakeMcp:
            available_servers = ("playwright",)

            def __init__(self):
                self.calls = []

            def owns(self, name):
                return name == "mcp_playwright_browser_click"

            def is_action(self, _name):
                return True

            def server_for(self, _name):
                return "playwright"

            def schemas(self, _servers):
                return []

            def find_tool(self, _server, suffixes):
                return "mcp_playwright_browser_click" if "browser_click" in suffixes else None

            def call(self, name, arguments):
                self.calls.append((name, arguments))
                return {"ok": False, "error": "simulated page state change"}

        fake = FakeMcp()
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = fake
        runtime._grant_task_lease("打开浏览器并点击搜索框", "browser_action_batch")
        result = runtime._run_local_tool("browser_action_batch", {
            "actions": [
                {"action": "click_ref", "arguments": {"ref": "e1"}},
                {"action": "click_ref", "arguments": {"ref": "e2"}},
            ],
            "risk_level": "normal", "risk_reason": "ordinary navigation",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(len(fake.calls), 1)

    def test_only_read_only_transient_mcp_failure_is_retried_once(self):
        class FakeMcp:
            available_servers = ("playwright",)

            def owns(self, name):
                return name == "mcp_playwright_browser_snapshot"

            def is_action(self, _name):
                return False

            def is_high_risk(self, _name):
                return False

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        with patch.object(runtime, "_run_local_tool", side_effect=[
            {"ok": False, "error": "MCP timed out"}, {"ok": True, "content": []}
        ]) as call:
            result = runtime._run_tool_with_recovery("mcp_playwright_browser_snapshot", {})
        self.assertTrue(result["ok"])
        self.assertEqual(call.call_count, 2)

    def test_action_failure_is_never_replayed(self):
        class FakeMcp:
            def owns(self, name):
                return name == "mcp_playwright_browser_click"

            def is_action(self, _name):
                return True

            def is_high_risk(self, _name):
                return False

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        with patch.object(runtime, "_run_local_tool", return_value={"ok": False, "error": "MCP timed out"}) as call:
            result = runtime._run_tool_with_recovery("mcp_playwright_browser_click", {"ref": "e1"})
        self.assertFalse(result["ok"])
        call.assert_called_once()

    def test_lost_playwright_target_marks_browser_space_broken(self):
        class FakeMcp:
            available_servers = ("playwright",)

            def owns(self, name):
                return name == "mcp_playwright_browser_snapshot"

            def is_action(self, _name):
                return False

            def is_high_risk(self, _name):
                return False

            def server_for(self, _name):
                return "playwright"

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        runtime._grant_task_lease("检查浏览器页面", "mcp_playwright_browser_snapshot")
        with patch.object(runtime, "_run_local_tool", return_value={"ok": False, "error": "Target closed"}):
            result = runtime._run_tool_with_recovery("mcp_playwright_browser_snapshot", {})
        self.assertTrue(result["requires_browser_reconnect"])
        with self.assertRaises(PermissionError):
            runtime.browser_spaces.require_agent_control(runtime._task_id)

    def test_window_control_schema_is_non_strict_for_optional_bounds(self):
        schema = next(item for item in ControlledTools.schemas() if item["name"] == "window_control")
        self.assertFalse(schema["strict"])
        self.assertEqual(schema["parameters"]["required"], ["window_id", "action"])

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

    def test_captcha_handoff_pauses_and_resumes_the_same_browser_task(self):
        class FakeMcp:
            def owns(self, name):
                return name == "mcp_playwright_browser_snapshot"

            def is_high_risk(self, name):
                return False

            def is_action(self, name):
                return False

            def schemas(self):
                return []

            def call(self, name, arguments):
                return {"ok": True, "content": [{"type": "text", "text": "快速验证身份：我是人类"}]}

        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        responses = iter([
            {"output": [{"type": "function_call", "call_id": "call-captcha",
                         "name": "mcp_playwright_browser_snapshot", "arguments": "{}"}]},
            {"output_text": "Found a T-shirt priced ¥129.", "output": []},
        ])
        runtime._request = lambda payload, key: next(responses)

        with patch("agent_runtime.get_api_key", return_value="test-key"):
            runtime.run_turn("打开淘宝并搜索 100 到 150 元的 T 恤", [])
            paused = []
            while not events.empty():
                paused.append(events.get_nowait())
            runtime.run_turn(runtime.HUMAN_VERIFICATION_CONTINUE, [])
        resumed = paused
        while not events.empty():
            resumed.append(events.get_nowait())

        handoff = next(payload for kind, payload in paused if kind == "human_verification")
        self.assertEqual(handoff["marker"], "快速验证身份")
        self.assertTrue(any(kind == "delta" and "¥129" in payload for kind, payload in resumed))
        self.assertIsNone(runtime._pending_human_verification)
        self.assertFalse(runtime._task_authorized())

    def test_request_retries_transient_http_502_then_succeeds(self):
        class Response:
            def read(self, _limit):
                return b'{"output_text":"RECOVERED"}'

            def close(self):
                return None

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        transient = urllib.error.HTTPError("https://example.test/v1/responses", 502, "Bad Gateway", {},
                                            io.BytesIO(b'{"error":"temporary"}'))
        with patch("agent_runtime.urllib.request.urlopen", side_effect=[transient, Response()]) as open_call, \
             patch("agent_runtime.time.sleep") as sleep:
            response = runtime._request({"model": "test"}, "key")
        self.assertEqual(response["output_text"], "RECOVERED")
        self.assertEqual(open_call.call_count, 2)
        sleep.assert_called_once()



if __name__ == "__main__":
    unittest.main()
