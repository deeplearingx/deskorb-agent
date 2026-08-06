import tempfile
import unittest
import io
import json
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
        self.assertTrue(AgentRuntime._execution_requested("搜索学习资料并返回最相关结果"))
        self.assertTrue(AgentRuntime._execution_requested("Search products on the web"))
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime._ensure_active_task("搜索学习资料并返回最相关结果")
        self.assertTrue(runtime._workflow.contract.requires_verification)

    def test_desktop_target_preserves_external_window_when_overlay_is_foreground(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.set_desktop_target_window(101, 202)
        self.assertEqual(runtime._desktop_target_hwnd, 101)
        self.assertEqual(runtime.desktop.preferred_hwnd, 101)
        self.assertEqual(runtime.desktop.overlay_hwnd, 202)

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

    def test_uia_profile_can_upgrade_model_normal_risk_to_high(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        with patch.object(runtime.uia, "is_high_risk", return_value=True):
            self.assertTrue(runtime._high_risk_call("desktop_uia_invoke", {
                "control_id": "U-SEND", "risk_level": "normal",
            }))

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
        self.assertEqual(runtime._capability_for_call("browser_action_batch"), "mcp:playwright")
        self.assertTrue(runtime._task_authorized_for("browser_action_batch"))
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

    def test_browser_action_batch_maps_tab_switch_and_extract_to_safe_tools(self):
        class FakeMcp:
            available_servers = ("playwright",)

            def __init__(self):
                self.calls = []

            def owns(self, name):
                return name in {"mcp_playwright_browser_tabs", "mcp_playwright_browser_snapshot"}

            def is_action(self, name):
                return name == "mcp_playwright_browser_tabs"

            def is_high_risk(self, _name):
                return False

            def server_for(self, _name):
                return "playwright"

            def schemas(self, _servers):
                return []

            def find_tool(self, _server, suffixes):
                if "browser_tabs" in suffixes:
                    return "mcp_playwright_browser_tabs"
                if "browser_snapshot" in suffixes:
                    return "mcp_playwright_browser_snapshot"
                return None

            def call(self, name, arguments):
                self.calls.append((name, arguments))
                return {"ok": True, "content": [{"type": "text", "text": "tab or snapshot"}]}

        fake = FakeMcp()
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = fake
        runtime._grant_task_lease("切换浏览器标签并提取商品信息", "browser_action_batch")
        result = runtime._run_local_tool("browser_action_batch", {
            "actions": [
                {"action": "switch_tab", "arguments": {"index": 1}},
                {"action": "extract", "arguments": {"ref": "e17", "fields": ["title", "price"]}},
            ],
            "risk_level": "normal", "risk_reason": "read-only browser observation",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(fake.calls[0], ("mcp_playwright_browser_tabs", {"action": "select", "index": 1}))
        self.assertEqual(fake.calls[1][0], "mcp_playwright_browser_snapshot")
        self.assertEqual(result["observations"][1]["fields"], ["title", "price"])
        self.assertEqual(result["observations"][1]["extraction"]["matched_fields"], 0)

    def test_browser_verify_uses_explicit_snapshot_postconditions(self):
        verified = AgentRuntime._verify_browser_observation({
            "ok": True,
            "content": [{"type": "text", "text": "深灰纯棉圆领 T 恤 价格 ¥129 https://shop.test/p/tee"}],
        }, {"contains": ["T 恤"], "price_min": 100, "price_max": 150, "origin": "shop.test"})
        self.assertTrue(verified["passed"])
        rejected = AgentRuntime._verify_browser_observation({
            "ok": True, "content": [{"type": "text", "text": "价格 ¥80 https://shop.test/p/tee"}],
        }, {"price_min": 100, "price_max": 150})
        self.assertFalse(rejected["passed"])

    def test_browser_verify_does_not_trust_false_backend_result_over_explicit_contract(self):
        verified = AgentRuntime._verify_browser_observation({
            "ok": True,
            "verification": {"passed": False},
            "content": [{"type": "text", "text": "title: T 恤\nprice: ¥129\nurl: /tee"}],
        }, {"contains": ["T 恤"], "required_fields": ["title", "url"],
            "price_min": 100, "price_max": 150})
        self.assertTrue(verified["passed"])

    def test_browser_verify_normalizes_snapshot_labels_and_text_whitespace(self):
        snapshot = {"ok": True, "content": [{"type": "text",
                    "text": 'paragraph "title: T 恤"\nlink "url: /products/tee"'}]}
        extracted = AgentRuntime._extract_browser_fields(snapshot, {"fields": ["title", "url"]})
        self.assertEqual(extracted["matched_fields"], 2)
        verified = AgentRuntime._verify_browser_observation(
            snapshot, {"contains": ["T恤"], "required_fields": ["title", "url"]})
        self.assertTrue(verified["passed"])

    def test_contract_postconditions_cannot_be_broadened_by_model(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime._ensure_active_task("搜索淘宝，找到 100-150 元的 T 恤")
        merged = runtime._contract_browser_postconditions({"price_min": 0, "price_max": 999,
                                                            "contains": ["其他"], "required_fields": ["price"]})
        self.assertEqual(merged["price_min"], 100.0)
        self.assertEqual(merged["price_max"], 150.0)
        self.assertEqual(set(merged["contains"]), {"其他", "T 恤"})
        self.assertEqual(set(merged["required_fields"]), {"price", "title", "url"})
        self.assertEqual(merged["origin"], "taobao.com")

    def test_browser_extract_and_required_fields_need_nonempty_values(self):
        snapshot = {"ok": True, "content": [{"type": "text", "text": "title: 深灰纯棉圆领 T 恤\nprice: ¥129"}]}
        extracted = AgentRuntime._extract_browser_fields(snapshot, {"ref": "e1", "fields": ["title", "price"]})
        self.assertEqual(extracted["matched_fields"], 2)
        self.assertEqual(extracted["fields"]["price"], "¥129")
        verified = AgentRuntime._verify_browser_observation(snapshot, {"required_fields": ["title", "price"]})
        self.assertTrue(verified["passed"])
        self.assertFalse(AgentRuntime._verify_browser_observation(
            {"ok": True, "content": [{"type": "text", "text": "title:"}]},
            {"required_fields": ["title"]})["passed"])

    def test_browser_price_range_ignores_unlabeled_page_numbers(self):
        outside = {"ok": True, "content": [{"type": "text",
            "text": "title: T 恤\nprice: ¥999\n材质：100% 纯棉\n评分：4.8"}]}
        self.assertFalse(AgentRuntime._verify_browser_observation(
            outside, {"price_min": 100, "price_max": 150})["passed"])
        labeled = {"ok": True, "content": [{"type": "text",
            "text": "title: T 恤\n售价 129 元\n材质：100% 纯棉"}]}
        self.assertTrue(AgentRuntime._verify_browser_observation(
            labeled, {"price_min": 100, "price_max": 150})["passed"])

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
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["history"]["samples"], 1)
        self.assertEqual(result["history"]["status_codes"], {"200": 1})
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

    def test_fallback_catalog_does_not_claim_generic_key_for_other_provider(self):
        runtime = AgentRuntime(Queue(), "primary", "https://example.test/v1", working_dir=self.root,
                               fallback_targets=[FallbackTarget(
                                   "deepseek", "deepseek", "deepseek-chat",
                                   "https://api.deepseek.com")])
        with patch("agent_runtime.get_provider_api_key", return_value=""):
            candidates = runtime.fallback_candidates()
        self.assertFalse(candidates[0]["api_key_configured"])

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
        self.assertTrue(result["requires_reobservation"])
        self.assertEqual(runtime.browser_spaces.for_task(runtime._task_id).state.value, "active")

    def test_reconnected_browser_blocks_actions_until_fresh_observation(self):
        class FakeMcp:
            available_servers = ("playwright",)

            def owns(self, name):
                return name in {"mcp_playwright_browser_snapshot", "mcp_playwright_browser_click"}

            def is_action(self, name):
                return name.endswith("browser_click")

            def is_high_risk(self, _name):
                return False

            def server_for(self, _name):
                return "playwright"

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime.mcp = FakeMcp()
        runtime._grant_task_lease("检查浏览器页面并点击搜索框", "mcp_playwright_browser_click")
        runtime._browser_reobservation_required = True
        with patch.object(runtime, "_run_local_tool") as local:
            blocked = runtime._run_tool_with_recovery("mcp_playwright_browser_click", {"ref": "e1"})
        self.assertTrue(blocked["requires_reobservation"])
        local.assert_not_called()

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

    def test_browser_tasks_use_structured_observation_without_full_screen_image(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime._task_mcp_servers.add("playwright")
        with patch.object(runtime.desktop, "capture_state", return_value={"ok": True, "snapshot_id": "NEXT"}), \
             patch.object(runtime.desktop, "capture_image_data_url", return_value="data:image/jpeg;base64,AA") as capture:
            transcript = runtime._append_desktop_observation([], "application_launch")
        self.assertEqual(len(transcript[-1]["content"]), 1)
        capture.assert_not_called()

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

    def test_task_transcript_budget_keeps_goal_and_complete_recent_tool_groups(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root,
                               context_tokens=4000)
        transcript = [{"role": "user", "content": [{"type": "input_text", "text": "原始任务目标"}]}]
        for index in range(8):
            transcript.extend([
                {"type": "function_call", "call_id": f"call-{index}", "name": "snapshot",
                 "arguments": "{}"},
                {"type": "function_call_output", "call_id": f"call-{index}",
                 "output": f"step-{index}-" + ("x" * 9000)},
            ])
        bounded = runtime._bound_task_transcript(transcript)
        encoded = json.dumps(bounded, ensure_ascii=False)
        self.assertLess(len(encoded), 30000)
        self.assertIn("原始任务目标", encoded)
        self.assertIn("step-7", encoded)
        self.assertIn("compacted", encoded)
        # Every retained tool output still has its corresponding call ID.
        call_ids = {item.get("call_id") for item in bounded if item.get("type") == "function_call"}
        output_ids = {item.get("call_id") for item in bounded if item.get("type") == "function_call_output"}
        self.assertEqual(output_ids - call_ids, set())

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

    def test_pause_preserves_continuation_and_resume_requires_explicit_click(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)
        runtime._grant_task_lease("打开记事本并确认状态", "desktop_capture_state")
        request = Mock(return_value={"output_text": "恢复后的结果", "output": []})
        runtime._request = request
        runtime.pause_task()
        runtime._run_task_loop("key", [{"role": "user", "content": [
            {"type": "input_text", "text": "打开记事本并确认状态"},
        ]}], "打开记事本并确认状态", False)
        self.assertIsNotNone(runtime._paused_execution)
        self.assertEqual(runtime.task_journal.task(runtime._task_id)["status"], "paused")
        request.assert_not_called()
        with patch("agent_runtime.get_api_key", return_value="rotated-key"):
            resumed = runtime.resume_paused_task(user_confirmed=True)
        self.assertTrue(resumed["ok"])
        self.assertIsNone(runtime._paused_execution)
        self.assertGreaterEqual(request.call_count, 1)

    def test_pause_does_not_set_cancelled_and_closes_inflight_response(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime._grant_task_lease("执行一个长任务", "desktop_capture_state")
        response = Mock()
        runtime._active_response = response
        result = runtime.pause_task()
        self.assertTrue(result["ok"])
        self.assertFalse(runtime._cancelled.is_set())
        response.close.assert_called_once()

    def test_explicit_handoff_marks_browser_space_for_user(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime._grant_task_lease("打开浏览器并检查页面", "browser_action_batch")
        task_id = runtime._task_id
        runtime.pause_task(handoff=True)
        runtime._pause_execution([], "打开浏览器并检查页面", False)
        space = runtime.browser_spaces.for_task(task_id)
        self.assertIsNotNone(space)
        self.assertEqual(space.owner.value, "user")

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

    def test_502_retry_compacts_reasoning_from_tool_continuation(self):
        class Response:
            def read(self, _limit):
                return b'{"output_text":"RECOVERED"}'

            def close(self):
                return None

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        transient = urllib.error.HTTPError("https://example.test/v1/responses", 502, "Bad Gateway", {}, io.BytesIO())
        payload = {"model": "test", "input": [
            {"type": "reasoning", "encrypted_content": "secret-envelope"},
            {"type": "function_call", "call_id": "c1", "name": "desktop_get_active_window", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "tool result"},
        ]}
        with patch("agent_runtime.urllib.request.urlopen", side_effect=[transient, Response()]) as open_call, \
             patch("agent_runtime.time.sleep"):
            response = runtime._request(payload, "key")
        self.assertEqual(response["output_text"], "RECOVERED")
        retry_body = json.loads(open_call.call_args_list[1].args[0].data.decode("utf-8"))
        self.assertNotIn("reasoning", [item.get("type") for item in retry_body["input"]])
        self.assertTrue(any(item.get("type") == "function_call_output" for item in retry_body["input"]))

    def test_502_retry_reduces_browser_schema_context_to_batch_adapter(self):
        payload = {
            "model": "test",
            "input": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "partial"}]},
                      {"type": "function_call_output", "call_id": "c1", "output": "snapshot"}],
            "tools": [{"type": "function", "name": "mcp_playwright_browser_snapshot"},
                      {"type": "function", "name": "browser_action_batch"},
                      {"type": "function", "name": "filesystem_read_text"}],
        }
        compacted = AgentRuntime._compact_retry_payload(payload)
        names = [item["name"] for item in compacted["tools"]]
        self.assertNotIn("mcp_playwright_browser_snapshot", names)
        self.assertIn("browser_action_batch", names)
        self.assertIn("filesystem_read_text", names)
        self.assertNotIn("message", [item.get("type") for item in compacted["input"]])

    def test_request_follows_same_origin_308_once(self):
        class Response:
            def read(self, _limit):
                return b'{"output_text":"REDIRECTED"}'

            def close(self):
                return None

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        redirect = urllib.error.HTTPError(
            "https://example.test/v1/responses", 308, "Permanent Redirect",
            {"Location": "https://example.test/v1/responses/"}, io.BytesIO())
        with patch("agent_runtime.urllib.request.urlopen", side_effect=[redirect, Response()]) as open_call:
            response = runtime._request({"model": "test"}, "key")
        self.assertEqual(response["output_text"], "REDIRECTED")
        self.assertEqual(open_call.call_count, 2)
        self.assertTrue(open_call.call_args_list[1].args[0].full_url.endswith("/responses/"))

    def test_request_rejects_cross_origin_308(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        redirect = urllib.error.HTTPError(
            "https://example.test/v1/responses", 308, "Permanent Redirect",
            {"Location": "https://evil.example/v1/responses"}, io.BytesIO())
        with patch("agent_runtime.urllib.request.urlopen", side_effect=redirect) as open_call:
            with self.assertRaisesRegex(RuntimeError, "unsafe or incomplete redirect"):
                runtime._request({"model": "test"}, "key")
        open_call.assert_called_once()

    def test_transient_provider_failures_open_circuit_and_health_probe_can_bypass(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        transient = urllib.error.HTTPError("https://example.test/v1/responses", 502, "Bad Gateway", {}, io.BytesIO())
        with patch("agent_runtime.API_CIRCUIT_FAILURE_THRESHOLD", 1), \
             patch("agent_runtime.time.sleep"), \
             patch("agent_runtime.urllib.request.urlopen", side_effect=transient) as open_call:
            with self.assertRaisesRegex(RuntimeError, "API HTTP 502"):
                runtime._request({"model": "test"}, "key")
            self.assertEqual(open_call.call_count, 3)
            with self.assertRaisesRegex(RuntimeError, "API circuit open"):
                runtime._request({"model": "test"}, "key")
            self.assertEqual(open_call.call_count, 3)
        class Response:
            def read(self, _limit):
                return b'{"output_text":"OK"}'

            def close(self):
                return None

        runtime._api_circuit_bypass = True
        with patch("agent_runtime.urllib.request.urlopen", return_value=Response()):
            self.assertEqual(runtime._request({"model": "test"}, "key")["output_text"], "OK")
        self.assertEqual(runtime._api_failure_streak, 0)

    def test_unverified_action_is_persisted_as_waiting_verification(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime._grant_task_lease("打开应用并确认状态", "application_launch")
        task_id = runtime._task_id
        runtime._record_tool_result("application_launch", {"application": "notepad"}, {"ok": True})
        runtime._close_active_task("completed")
        task = runtime.task_journal.task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "waiting_verification")

    def test_resume_blocks_side_effect_until_observation_and_deduplicates_it(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime._grant_task_lease("打开记事本并确认状态", "application_launch")
        runtime._resuming_task = True
        runtime._resume_observed = False
        with patch.object(runtime, "_run_local_tool") as local:
            blocked = runtime._run_tool_with_recovery("application_launch", {"application": "notepad"})
        self.assertTrue(blocked["requires_reobservation"])
        local.assert_not_called()
        runtime._resume_observed = True
        runtime.task_journal.record_effect(runtime._task_id, "application_launch", {"application": "notepad"})
        duplicate = runtime._run_tool_with_recovery("application_launch", {"application": "notepad"})
        self.assertTrue(duplicate["duplicate_prevented"])

    def test_empty_stateless_tool_continuation_gets_one_explicit_retry(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)
        runtime._ensure_active_task("回答工具结果")
        responses = iter([
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": ""}]}]},
            {"output_text": "继续结果", "output": []},
        ])
        runtime._request = lambda _payload, _key: next(responses)
        runtime._run_task_loop("key", [
            {"role": "user", "content": [{"type": "input_text", "text": "执行一个只读工具"}]},
            {"type": "function_call_output", "call_id": "probe", "output": "TOOL_OK"},
        ], "回答工具结果", False)
        values = []
        while not events.empty():
            values.append(events.get_nowait())
        self.assertTrue(any(kind == "delta" and value == "继续结果" for kind, value in values))
        self.assertTrue(any(kind == "task_progress" and value.get("terminal") == "completed"
                            for kind, value in values if isinstance(value, dict)))

    def test_invalid_sibling_call_does_not_reuse_previous_arguments(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)
        runtime._ensure_active_task("执行两个只读步骤")
        responses = iter([
            {"output": [
                {"type": "function_call", "call_id": "ok-call",
                 "name": "filesystem_list", "arguments": '{"path":"."}'},
                {"type": "function_call", "call_id": "bad-call",
                 "name": "filesystem_read_text", "arguments": "not-json"},
            ]},
            {"output_text": "已完成", "output": []},
            {"output_text": "已复核", "output": []},
        ])
        runtime._request = Mock(side_effect=lambda payload, key: next(responses))
        recorded = []
        with patch.object(runtime, "_run_local_tool", return_value={"ok": True, "entries": []}), \
             patch.object(runtime, "_record_tool_result",
                          side_effect=lambda name, arguments, result: recorded.append((name, arguments))):
            runtime._run_task_loop("key", [
                {"role": "user", "content": [{"type": "input_text", "text": "执行两个只读步骤"}]},
            ], "执行两个只读步骤", False)
        self.assertEqual(recorded[0], ("filesystem_list", {"path": "."}))
        self.assertEqual(recorded[1], ("filesystem_read_text", {}))

    def test_action_answer_gets_one_explicit_verification_round_before_completion(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)
        runtime._grant_task_lease("打开记事本并确认窗口状态", "desktop_capture_state")
        responses = iter([
            {"output": [{"type": "function_call", "call_id": "observe",
                         "name": "desktop_capture_state", "arguments": "{}"}]},
            {"output_text": "已执行操作。", "output": []},
            {"output": [{"type": "function_call", "call_id": "verify",
                         "name": "desktop_verify_state", "arguments": '{"snapshot_id":"S1"}'}]},
            {"output_text": "已验证窗口状态。", "output": []},
        ])
        runtime._request = Mock(side_effect=lambda payload, key: next(responses))
        with patch.object(runtime, "_run_local_tool", side_effect=[
            {"ok": True, "snapshot_id": "S1"},
            {"ok": True, "screen_changed": True},
        ]):
            runtime._run_task_loop("key", [
                {"role": "user", "content": [{"type": "input_text", "text": "打开记事本并确认窗口状态"}]},
            ], "打开记事本并确认窗口状态", False)
        values = []
        while not events.empty():
            values.append(events.get_nowait())
        self.assertTrue(any(kind == "delta" and value == "已验证窗口状态。" for kind, value in values))
        self.assertTrue(any(kind == "task_progress" and value.get("terminal") == "completed"
                            and value.get("verified") for kind, value in values if isinstance(value, dict)))

    def test_task_turn_retries_transient_provider_failure_without_replaying_tools(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=self.root)
        runtime._ensure_active_task("回答工具结果")
        request = Mock(side_effect=[RuntimeError("API HTTP 502: temporary gateway"),
                                    {"output_text": "恢复成功", "output": []}])
        runtime._request = request
        with patch("agent_runtime.time.sleep") as sleep:
            runtime._run_task_loop("key", [
                {"role": "user", "content": [{"type": "input_text", "text": "执行一个只读工具"}]},
                {"type": "function_call_output", "call_id": "probe", "output": "TOOL_OK"},
            ], "回答工具结果", False)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args[0], request.call_args_list[0].args[0])
        sleep.assert_called_once()
        values = []
        while not events.empty():
            values.append(events.get_nowait())
        self.assertTrue(any(kind == "delta" and value == "恢复成功" for kind, value in values))

    def test_task_request_records_safe_model_turn_metrics(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime._request = Mock(return_value={"output_text": "OK", "output": []})
        result = runtime._request_for_task({"model": "test"}, "key")
        self.assertEqual(result["output_text"], "OK")
        summary = runtime.model_health_store.summary("responses", "test", operation="model_turn")
        self.assertEqual(summary["samples"], 1)
        self.assertEqual(summary["success_rate"], 1.0)
        self.assertEqual(summary["retry_rate"], 0.0)

    def test_failed_task_request_records_status_and_retry_metadata(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        runtime._request = Mock(side_effect=[RuntimeError("API HTTP 502: gateway"),
                                             RuntimeError("API HTTP 503: unavailable")])
        with patch("agent_runtime.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "503"):
                runtime._request_for_task({"model": "test"}, "key")
        summary = runtime.model_health_store.summary("responses", "test", operation="model_turn")
        self.assertEqual(summary["samples"], 1)
        self.assertEqual(summary["retry_rate"], 1.0)
        self.assertEqual(summary["status_codes"], {"503": 1})
        self.assertEqual(summary["failure_kinds"], {"transient_network": 1})

    def test_task_retry_can_probe_once_after_per_request_circuit_opens(self):
        class Response:
            def read(self, _limit):
                return b'{"output_text":"RECOVERED"}'

            def close(self):
                return None

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=self.root)
        transient = urllib.error.HTTPError("https://example.test/v1/responses", 502, "Bad Gateway", {}, io.BytesIO())
        with patch("agent_runtime.API_REQUEST_RETRIES", 0), patch("agent_runtime.API_CIRCUIT_FAILURE_THRESHOLD", 1), \
             patch("agent_runtime.urllib.request.urlopen", side_effect=[transient, Response()]) as open_call, \
             patch("agent_runtime.time.sleep"):
            result = runtime._request_for_task({"model": "test"}, "key")
        self.assertEqual(result["output_text"], "RECOVERED")
        self.assertEqual(open_call.call_count, 2)
        self.assertFalse(runtime._api_circuit_bypass)



if __name__ == "__main__":
    unittest.main()
