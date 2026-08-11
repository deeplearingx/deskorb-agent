"""Deterministic end-to-end checks for the real AgentRuntime orchestration.

The model and MCP server are fakes, but the approval flow, task lease,
browser batch executor, workflow evidence, journal and UI events are real.
No external website, account, or provider is contacted.
"""
import tempfile
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import patch

from agent_runtime import AgentRuntime


class FixtureMcp:
    available_servers = ("playwright",)

    def owns(self, name):
        return name in {"mcp_playwright_browser_click", "mcp_playwright_browser_snapshot"}

    def is_action(self, name):
        return name == "mcp_playwright_browser_click"

    def is_high_risk(self, _name):
        return False

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
        if name == "mcp_playwright_browser_click":
            return {"ok": True, "content": [{"type": "text", "text": "search result card opened"}],
                    "arguments": arguments}
        return {"ok": True, "content": [{"type": "text", "text": "Product: 深灰纯棉圆领 T 恤, price 129, url https://fixture.test/p/tee"}],
                "verification": {"passed": True, "fields": {"title": True, "price": True, "url": True}}}


class RecoveryMcp(FixtureMcp):
    def __init__(self):
        self.snapshot_calls = 0
        self.click_calls = 0
        self.space_ids: list[str | None] = []

    def call(self, name, arguments):
        if name == "mcp_playwright_browser_click":
            self.click_calls += 1
            if self.click_calls == 1:
                return {"ok": False, "error": "Target closed"}
            return {"ok": True, "content": [{"type": "text", "text": "result card"}]}
        if name == "mcp_playwright_browser_snapshot":
            self.snapshot_calls += 1
            return {"ok": True, "content": [{"type": "text", "text": "Product: 深灰纯棉圆领 T 恤, price 129"}],
                    "verification": {"passed": True}}
        return {"ok": False, "error": "Unexpected tool"}


class RuntimeE2ETests(unittest.TestCase):
    def test_browser_task_reaches_verified_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Queue()
            runtime = AgentRuntime(events, "fixture-model", "https://example.test/v1", working_dir=Path(directory))
            runtime.mcp = FixtureMcp()
            responses = iter([
                {"output": [{"type": "function_call", "call_id": "launch", "name": "application_launch",
                             "arguments": '{"application":"chrome"}'}]},
                {"output": [{"type": "function_call", "call_id": "batch", "name": "browser_action_batch",
                             "arguments": '{"actions":[{"action":"click_ref","arguments":{"ref":"e1"}}],'
                                         '"risk_level":"normal","risk_reason":"ordinary search"}'}]},
                {"output": [{"type": "function_call", "call_id": "snapshot", "name": "mcp_playwright_browser_snapshot",
                             "arguments": "{}"}]},
                {"output_text": "已找到：深灰纯棉圆领 T 恤，129 元。", "output": []},
            ])
            runtime._request = lambda _payload, _key: next(responses)
            with patch("agent_runtime.get_api_key", return_value="fixture-key"), \
                 patch.object(runtime.tools, "launch_application", return_value={"ok": True, "application": "chrome", "pid": 123}):
                runtime.run_turn("打开浏览器，搜索测试商品并返回结果", [])
                first = []
                while not events.empty():
                    first.append(events.get_nowait())
                approval = next(value for kind, value in first if kind == "approval")
                token = str(approval).split("确认 ", 1)[1].splitlines()[0]
                runtime.run_turn("确认 " + token, [])
            remaining = []
            while not events.empty():
                remaining.append(events.get_nowait())
            progress = [value for kind, value in remaining if kind == "task_progress" and isinstance(value, dict) and value.get("terminal")]
            answers = [str(value) for kind, value in remaining if kind == "delta"]
            self.assertTrue(progress)
            self.assertTrue(progress[-1]["verified"])
            self.assertIn("129", "\n".join(answers))
            self.assertEqual(runtime.recoverable_tasks(), [])

    def test_browser_reconnect_requires_observation_before_retrying_action(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(Queue(), "fixture-model", "https://example.test/v1", working_dir=Path(directory))
            runtime.mcp = RecoveryMcp()
            runtime._grant_task_lease("打开浏览器检查测试商品", "browser_action_batch")
            first_space = runtime.browser_spaces.for_task(runtime._task_id)
            failed = runtime._run_tool_with_recovery("mcp_playwright_browser_click", {"ref": "e1"})
            second_space = runtime.browser_spaces.for_task(runtime._task_id)
            self.assertFalse(failed["ok"])
            self.assertTrue(failed["requires_reobservation"])
            self.assertIsNotNone(first_space)
            self.assertIsNotNone(second_space)
            self.assertNotEqual(first_space.space_id, second_space.space_id)
            blocked = runtime._run_tool_with_recovery("mcp_playwright_browser_click", {"ref": "e1"})
            self.assertFalse(blocked["ok"])
            self.assertTrue(blocked["requires_reobservation"])
            observed = runtime._run_tool_with_recovery("mcp_playwright_browser_snapshot", {})
            self.assertTrue(observed["ok"])
            self.assertFalse(runtime._browser_reobservation_required)
            retried = runtime._run_tool_with_recovery("mcp_playwright_browser_click", {"ref": "e2"})
            self.assertTrue(retried["ok"])
            self.assertEqual(runtime.mcp.click_calls, 2)

    def test_desktop_task_reaches_verified_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Queue()
            runtime = AgentRuntime(events, "fixture-model", "https://example.test/v1", working_dir=Path(directory))
            responses = iter([
                {"output": [{"type": "function_call", "call_id": "launch", "name": "application_launch",
                             "arguments": '{"application":"notepad"}'}]},
                {"output": [{"type": "function_call", "call_id": "capture", "name": "desktop_capture_state",
                             "arguments": "{}"}]},
                {"output": [{"type": "function_call", "call_id": "type", "name": "desktop_type",
                             "arguments": '{"snapshot_id":"S1","text":"DeskOrb E2E smoke test",'
                                         '"risk_level":"normal","risk_reason":"fill the requested test field"}'}]},
                {"output": [{"type": "function_call", "call_id": "verify", "name": "desktop_verify_state",
                             "arguments": '{"snapshot_id":"S1"}'}]},
                {"output_text": "已输入并确认桌面状态变化。", "output": []},
            ])
            runtime._request = lambda _payload, _key: next(responses)
            with patch("agent_runtime.get_api_key", return_value="fixture-key"), \
                 patch.object(runtime.tools, "launch_application", return_value={"ok": True, "application": "notepad", "pid": 456}), \
                 patch.object(runtime.desktop, "capture_state", return_value={"ok": True, "snapshot_id": "S1", "active_window": "Notepad", "screen_digest": "fixture"}), \
                 patch.object(runtime.desktop, "capture_image_data_url", return_value=None), \
                 patch.object(runtime.desktop, "type_text", return_value={"ok": True, "characters": 19}), \
                 patch.object(runtime.desktop, "verify_state", return_value={"ok": True, "screen_changed": True, "active_window_changed": False}):
                runtime.run_turn("打开记事本并输入测试文本，然后验证状态", [])
                first = []
                while not events.empty():
                    first.append(events.get_nowait())
                approval = next(value for kind, value in first if kind == "approval")
                token = str(approval).split("确认 ", 1)[1].splitlines()[0]
                runtime.run_turn("确认 " + token, [])
            remaining = []
            while not events.empty():
                remaining.append(events.get_nowait())
            terminal = [value for kind, value in remaining if kind == "task_progress" and isinstance(value, dict) and value.get("terminal")]
            self.assertTrue(terminal)
            self.assertTrue(terminal[-1]["verified"])
            self.assertTrue(any(kind == "delta" and "确认" in str(value) for kind, value in remaining))


if __name__ == "__main__":
    unittest.main()
