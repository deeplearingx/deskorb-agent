import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import mcp_readiness_probe as probe


class FakeBridge:
    available_servers = ("playwright", "powertoys")
    diagnostics = []

    def __init__(self, *_args, **_kwargs):
        self.closed = False

    def schemas_for_task(self, _servers):
        return [
            {"name": "mcp_playwright_browser_navigate"},
            {"name": "mcp_playwright_browser_snapshot"},
            {"name": "mcp_playwright_browser_wait_for"},
            {"name": "mcp_playwright_browser_tabs"},
        ]

    def close(self):
        self.closed = True


class MCPReadinessProbeTests(unittest.TestCase):
    def _run(self, diagnostics=(), bridge=FakeBridge):
        output = io.StringIO()
        with patch.object(probe, "local_playwright_diagnostics", return_value=list(diagnostics)), \
             patch.object(probe, "MCPToolBridge", bridge), \
             redirect_stdout(output):
            code = probe.main()
        return code, json.loads(output.getvalue())

    def test_ready_output_contains_only_bounded_metadata(self):
        code, result = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(result["tool_count"], 4)
        self.assertNotIn("tool_names", result)
        self.assertNotIn("url", json.dumps(result))

    def test_missing_local_install_fails_before_server_discovery(self):
        class UnexpectedBridge(FakeBridge):
            def schemas_for_task(self, _servers):
                raise AssertionError("missing local install must not launch MCP")

        code, result = self._run(("playwright_cli_missing",), UnexpectedBridge)
        self.assertEqual(code, 2)
        self.assertEqual(result["diagnostics"], ["playwright_cli_missing", "required_playwright_tools_missing"])

    def test_missing_required_task_tool_fails_readiness(self):
        class IncompleteBridge(FakeBridge):
            def schemas_for_task(self, _servers):
                return [{"name": "mcp_playwright_browser_snapshot"}]

        code, result = self._run(bridge=IncompleteBridge)
        self.assertEqual(code, 2)
        self.assertEqual(result["diagnostics"], ["required_playwright_tools_missing"])


if __name__ == "__main__":
    unittest.main()
