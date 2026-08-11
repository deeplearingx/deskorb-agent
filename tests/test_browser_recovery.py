import tempfile
import unittest
from pathlib import Path
from queue import Queue

from agent_runtime import AgentRuntime


class RecoveryMcp:
    available_servers = ("playwright",)

    def __init__(self):
        self.click_calls = 0
        self.snapshot_calls = 0
        self.close_calls = 0

    def owns(self, name):
        return name in {"mcp_playwright_browser_click", "mcp_playwright_browser_snapshot"}

    def is_action(self, name):
        return name == "mcp_playwright_browser_click"

    def is_read_only_call(self, _name, _arguments):
        return False

    def call(self, name, _arguments):
        if name == "mcp_playwright_browser_click":
            self.click_calls += 1
            if self.click_calls == 1:
                return {"ok": False, "error": "Target closed"}
            return {"ok": True, "content": [{"type": "text", "text": "opened"}]}
        self.snapshot_calls += 1
        return {"ok": True, "content": [{"type": "text", "text": "fresh page"}],
                "verification": {"passed": True}}

    def close(self):
        self.close_calls += 1


class BrowserRecoveryTests(unittest.TestCase):
    def test_disconnect_never_replays_action_before_fresh_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(Queue(), "fixture-model", "https://example.test/v1",
                                   working_dir=Path(directory))
            bridge = RecoveryMcp()
            runtime.mcp = bridge

            first = runtime._run_local_tool("mcp_playwright_browser_click", {"ref": "e1"})
            blocked = runtime._run_local_tool("mcp_playwright_browser_click", {"ref": "e1"})
            observed = runtime._run_local_tool("mcp_playwright_browser_snapshot", {})
            retried = runtime._run_local_tool("mcp_playwright_browser_click", {"ref": "e2"})

        self.assertFalse(first["ok"])
        self.assertTrue(first["requires_reobservation"])
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["requires_reobservation"])
        self.assertTrue(observed["ok"])
        self.assertTrue(retried["ok"])
        self.assertEqual(bridge.click_calls, 2)
        self.assertEqual(bridge.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
