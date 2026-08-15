import unittest
from queue import Queue
from pathlib import Path
from unittest.mock import patch

from agent_runtime import AgentRuntime
from browser_runtime import PlaywrightMCPBackend
from mcp_client import MCPError, MCPServerSpec, MCPToolBridge, StdioMCPClient
from task_runtime import classify_failure


class _FakeBrowserMCP:
    def __init__(self, *, snapshot=None, schema_error=None):
        self.snapshot = snapshot or {"ok": True, "content": {"role": "document"}}
        self.schema_error = schema_error
        self.schemas_calls = []
        self.calls = []
        self.close_calls = 0

    def is_browser_isolated(self):
        return True

    def schemas(self, server_names=None, timeout_seconds=None):
        self.schemas_calls.append((tuple(server_names or ()), timeout_seconds))
        if self.schema_error:
            raise self.schema_error
        return [{"name": "mcp_playwright_browser_snapshot"}]

    def call(self, name, arguments, timeout_seconds=None):
        self.calls.append((name, dict(arguments), timeout_seconds))
        return dict(self.snapshot)

    def browser_process_id(self, server_name="playwright"):
        return 1234

    def close(self):
        self.close_calls += 1


class BrowserStabilizationTests(unittest.TestCase):
    def test_browser_start_failure_codes_are_stable_task_categories(self):
        for value in (
            "browser_mcp_start_failed", "browser_window_not_visible",
            "browser_initial_snapshot_timeout", "browser_task_unverified",
        ):
            with self.subTest(value=value):
                self.assertEqual(classify_failure(value), value)

    def test_first_browser_batch_still_gates_high_risk_target(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=Path.cwd())
        self.assertTrue(runtime._high_risk_call("browser_action_batch", {
            "actions": [{
                "action": "click_ref",
                "arguments": {"ref": "submit_button", "observation_id": "obs-1"},
            }],
        }))

    def test_browser_backend_honors_remaining_tool_budget(self):
        class Bridge:
            def __init__(self):
                self.calls = []

            def call(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {"ok": True}

        bridge = Bridge()
        backend = PlaywrightMCPBackend(bridge, timeout_getter=lambda: 2.5)
        result = backend.call("snapshot", {})
        self.assertTrue(result["ok"])
        self.assertEqual(bridge.calls[0][1]["timeout_seconds"], 2.5)

        exhausted = PlaywrightMCPBackend(bridge, timeout_getter=lambda: 0)
        result = exhausted.call("snapshot", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "tool_execution_timeout")

    def test_stdio_stderr_is_bounded_and_redacted(self):
        client = StdioMCPClient(MCPServerSpec("playwright", "node", (), {}))
        for index in range(200):
            client._capture_stderr_line(
                f"ERROR browser launch {index} url=https://private.example/{index} "
                "authorization=super-secret-token page text must not escape"
            )

        diagnostics = client.diagnostics()
        rendered = "\n".join(diagnostics["stderr"])
        self.assertLessEqual(len(diagnostics["stderr"]), client.MAX_STDERR_LINES)
        self.assertLessEqual(sum(len(line) for line in diagnostics["stderr"]), client.MAX_STDERR_BYTES)
        self.assertNotIn("private.example", rendered)
        self.assertNotIn("super-secret-token", rendered)
        self.assertNotIn("page text must not escape", rendered)

    def test_prepare_visible_browser_reports_mcp_start_failure(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=Path.cwd())
        runtime.mcp = _FakeBrowserMCP(schema_error=MCPError("MCP process exited before initialize"))

        result = runtime.prepare_visible_browser()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_mcp_start_failed")
        self.assertEqual(runtime.mcp.close_calls, 1)
        self.assertEqual([item[1]["phase"] for item in _browser_statuses(events)], [
            "starting", "failed",
        ])

    def test_invalid_browser_batch_publishes_bounded_diagnostic(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=Path.cwd())
        runtime.mcp = _FakeBrowserMCP()

        class Session:
            def execute(self, _actions):
                return {
                    "ok": False,
                    "failure_kind": "invalid_browser_action_batch",
                    "error": "Browser batch contains an unsupported action.",
                }

        with patch.object(runtime, "_ensure_browser_session", return_value=Session()):
            result = runtime._run_browser_action_batch({"actions": [{"action": "navigate"}]})

        self.assertFalse(result["ok"])
        failed = _browser_statuses(events)[-1][1]
        self.assertEqual(failed["failure_kind"], "invalid_browser_action_batch")
        self.assertEqual(failed["detail"], "Browser batch contains an unsupported action.")

    def test_semantic_browser_transport_reconnects_once_without_replaying_actions(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=Path.cwd())
        runtime.mcp = _FakeBrowserMCP()

        class Session:
            def __init__(self):
                self.calls = 0

            def execute(self, _actions):
                self.calls += 1
                return {
                    "ok": False,
                    "failure_kind": "browser_mcp_connection_failed",
                    "error": "The local browser MCP connection closed.",
                    "requires_reobservation": True,
                }

        session = Session()
        with patch.object(runtime, "_ensure_browser_session", return_value=session), \
                patch.object(runtime, "_reconnect_browser_mcp") as reconnect:
            first = runtime._run_browser_action_batch({"actions": [{"action": "snapshot"}]})
            second = runtime._run_browser_action_batch({"actions": [{"action": "snapshot"}]})

        self.assertEqual(first["failure_kind"], "browser_mcp_connection_failed")
        self.assertEqual(first["recovery_attempts"], 1)
        self.assertTrue(first["requires_reobservation"])
        self.assertEqual(second["recovery_attempts"], 1)
        self.assertEqual(reconnect.call_count, 1)
        self.assertEqual(session.calls, 2)

    def test_raw_browser_transport_failure_is_classified_before_reconnect(self):
        class RawBrowserMCP:
            def __init__(self):
                self.calls = 0

            def owns(self, name):
                return name == "mcp_playwright_browser_click"

            def server_name(self, _name):
                return "playwright"

            def call(self, _name, _arguments):
                self.calls += 1
                raise RuntimeError("MCP server playwright disconnected")

            def close(self):
                return None

        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=Path.cwd())
        runtime.mcp = RawBrowserMCP()

        result = runtime._run_local_tool("mcp_playwright_browser_click", {})

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_mcp_connection_failed")
        self.assertEqual(result["recovery_attempts"], 1)
        self.assertTrue(result["requires_reobservation"])

    def test_semantic_browser_transport_stops_after_one_recovery(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=Path.cwd())
        runtime.mcp = _FakeBrowserMCP()
        runtime._browser_recovery_attempts = 1

        class Session:
            def execute(self, _actions):
                return {
                    "ok": False,
                    "failure_kind": "tool_execution_timeout",
                    "error": "The browser action exceeded its bounded budget.",
                    "requires_reobservation": True,
                }

        with patch.object(runtime, "_ensure_browser_session", return_value=Session()), \
                patch.object(runtime, "_reconnect_browser_mcp") as reconnect:
            result = runtime._run_browser_action_batch({"actions": [{"action": "snapshot"}]})

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_mcp_recovery_exhausted")
        self.assertEqual(result["recovery_attempts"], 1)
        self.assertFalse(result["requires_reobservation"])
        reconnect.assert_not_called()

    def test_browser_failure_telemetry_redacts_url_and_credentials(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=Path.cwd())
        runtime._publish_tool_result(
            "browser_action_batch",
            {"actions": [{"action": "navigate"}]},
            {
                "ok": False,
                "failure_kind": "invalid_browser_action_batch",
                "error": "bad https://private.example/path authorization=secret-token",
            },
        )
        _kind, payload = events.get_nowait()
        self.assertEqual(payload["failure_detail"], "bad <url> credential=<redacted>")

    def test_prepare_visible_browser_prewarms_and_focuses_only_once(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=Path.cwd())
        fake = _FakeBrowserMCP()
        runtime.mcp = fake
        with patch("agent_runtime.visible_browser_windows", return_value={9001}), \
             patch("agent_runtime.focus_browser_window_once", return_value=True) as focus:
            first = runtime.prepare_visible_browser()
            second = runtime.prepare_visible_browser()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(focus.call_count, 1)
        self.assertEqual([item[1]["phase"] for item in _browser_statuses(events)], [
            "starting", "visible", "ready",
        ])

    def test_prepare_visible_browser_classifies_initial_snapshot_timeout(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=Path.cwd())
        runtime.mcp = _FakeBrowserMCP(snapshot={
            "ok": False,
            "failure_kind": "browser_initial_snapshot_timeout",
            "error": "snapshot timed out",
        })
        with patch("agent_runtime.BROWSER_START_TIMEOUT_SECONDS", 1):
            result = runtime.prepare_visible_browser()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_initial_snapshot_timeout")
        self.assertEqual(runtime.mcp.close_calls, 1)
        self.assertEqual(_browser_statuses(events)[-1][1]["phase"], "failed")

    def test_browser_start_failure_cleans_owned_playwright_output_dir(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=Path.cwd())
        bridge = MCPToolBridge(None, enable_playwright=True, enable_officecli=False)
        spec = next(item for item in bridge.specs if item.name == "playwright")
        output_index = spec.args.index("--output-dir")
        output_dir = Path(spec.args[output_index + 1])
        output_dir.mkdir()
        runtime.mcp = bridge

        try:
            with patch.object(bridge, "schemas",
                              side_effect=MCPError("MCP process exited before initialize")):
                result = runtime.prepare_visible_browser()
        finally:
            bridge.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_mcp_start_failed")
        self.assertFalse(output_dir.exists())

    def test_prepare_visible_browser_classifies_missing_window(self):
        events = Queue()
        runtime = AgentRuntime(events, "test", "https://example.test/v1", working_dir=Path.cwd())
        runtime.mcp = _FakeBrowserMCP()
        with patch.object(runtime, "_wait_for_browser_window", return_value=set()):
            result = runtime.prepare_visible_browser()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_window_not_visible")
        self.assertEqual(runtime.mcp.close_calls, 1)
        self.assertEqual([item[1]["phase"] for item in _browser_statuses(events)], [
            "starting", "failed",
        ])

    def test_interrupt_closes_task_owned_browser_backend(self):
        runtime = AgentRuntime(Queue(), "test", "https://example.test/v1", working_dir=Path.cwd())
        fake = _FakeBrowserMCP()
        runtime.mcp = fake

        runtime.interrupt()

        self.assertEqual(fake.close_calls, 1)
        self.assertFalse(runtime._browser_prepared)


def _browser_statuses(events):
    values = []
    while not events.empty():
        item = events.get_nowait()
        if item[0] == "browser_status":
            values.append(item)
    return values


if __name__ == "__main__":
    unittest.main()
