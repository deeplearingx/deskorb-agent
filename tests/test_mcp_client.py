import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_client import (MCPServerSpec, MCPToolBridge, StdioMCPClient,
                        configured_playwright_proxy, load_mcp_servers,
                        resolve_officecli_binary)


class FakeClient:
    def list_tools(self):
        return [
            {"name": "browser_snapshot", "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "browser_click", "inputSchema": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]}},
        ]

    def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": json.dumps({"name": name, "arguments": arguments})}]}

    def close(self):
        return None


class RecordingProcessClient(FakeClient):
    def __init__(self):
        self.start_count = 0

    def list_tools(self):
        self.start_count += 1
        return super().list_tools()


class MCPClientTests(unittest.TestCase):
    def test_standard_mcp_config_loads_enabled_servers_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {
                "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.79", "--isolated"]},
                "disabled": {"command": "ignore", "enabled": False},
            }}), encoding="utf-8")
            specs = load_mcp_servers(path)
        self.assertEqual([spec.name for spec in specs], ["playwright"])
        self.assertIn("@playwright/mcp@0.0.79", specs[0].args)

    def test_fla_ui_example_keeps_the_isolated_playwright_backend(self):
        root = Path(__file__).resolve().parents[1]
        example = root / "mcp.servers.fla-ui.example.json"
        specs = load_mcp_servers(example)
        self.assertEqual([spec.name for spec in specs], ["windows", "playwright"])
        self.assertEqual(specs[0].command, r"D:\tools\deskorb\fla-ui-mcp\v0.2.0\FlaUI.Mcp.exe")
        self.assertIn("@playwright/mcp@0.0.79", specs[1].args)

        payload = json.loads(example.read_text(encoding="utf-8"))
        payload["mcpServers"]["windows"]["enabled"] = False
        with tempfile.TemporaryDirectory() as directory:
            enabled = Path(directory) / "mcp.json"
            enabled.write_text(json.dumps(payload), encoding="utf-8")
            specs = load_mcp_servers(enabled)
        self.assertEqual([spec.name for spec in specs], ["playwright"])

    def test_custom_intent_metadata_is_available_without_starting_server(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {
                "knowledge": {"command": "knowledge-mcp", "deskorb": {
                    "description": "Search internal documentation", "keywords": ["wiki", "知识库"]}},
            }}), encoding="utf-8")
            bridge = MCPToolBridge(path)
        self.assertEqual(bridge.server_catalog(), [{"name": "knowledge", "description": "Search internal documentation", "keywords": ["wiki", "知识库"]}])

    def test_bridge_marks_browser_actions_and_removes_deskorb_metadata(self):
        bridge = MCPToolBridge(None, enable_playwright=True)
        bridge.clients["playwright"] = FakeClient()
        schemas = bridge.schemas()
        snapshot = next(item for item in schemas if item["name"].endswith("browser_snapshot"))
        click = next(item for item in schemas if item["name"].endswith("browser_click"))
        self.assertFalse(bridge.is_action(snapshot["name"]))
        self.assertTrue(bridge.is_action(click["name"]))
        self.assertIn("_deskorb_risk_level", click["parameters"]["properties"])
        result = bridge.call(click["name"], {"ref": "button-7", "_deskorb_risk_level": "normal",
                                               "_deskorb_risk_reason": "search"})
        self.assertTrue(result["ok"])
        rendered = json.dumps(result["content"], ensure_ascii=False)
        self.assertIn("button-7", rendered)
        self.assertNotIn("_deskorb_risk_level", rendered)

    def test_default_servers_include_local_powertoys_adapter(self):
        specs = load_mcp_servers(None, enable_playwright=True)
        names = [spec.name for spec in specs]
        self.assertIn("playwright", names)
        self.assertIn("powertoys", names)
        powertoys = next(spec for spec in specs if spec.name == "powertoys")
        self.assertTrue(powertoys.args[-1].endswith("powertoys_mcp.py"))

    def test_default_playwright_backend_requires_isolated_profile(self):
        bridge = MCPToolBridge(None, enable_playwright=True)
        self.assertTrue(bridge.is_browser_isolated())

    def test_browser_backend_recovery_does_not_recreate_semantic_session(self):
        # The semantic runtime owns session identity; reconnecting the MCP
        # transport must not force a fresh session object or tab lineage.
        from browser_runtime import BrowserExecutionSession
        from unittest.mock import Mock
        backend = Mock()
        session = BrowserExecutionSession(backend)
        original_session_id = session.browser_session_id
        original_tab_id = session.tab_id
        session._reobservation_required = True
        result = session.execute([{"action": "snapshot", "arguments": {}}])
        self.assertEqual(result["browser_session_id"], original_session_id)
        self.assertEqual(result["tab_id"], original_tab_id)

    def test_playwright_output_dir_is_private_and_cleaned_with_bridge(self):
        bridge = MCPToolBridge(None, enable_playwright=True, enable_officecli=False)
        spec = next(item for item in bridge.specs if item.name == "playwright")
        output_index = spec.args.index("--output-dir")
        output_dir = Path(spec.args[output_index + 1])

        # The path is reserved at bridge construction, but the directory is
        # created only when the Playwright process actually starts.
        self.assertFalse(output_dir.exists())
        self.assertNotEqual(output_dir.parent.resolve(), Path(__file__).resolve().parents[1])
        self.assertTrue(output_dir.name.startswith("deskorb-playwright-output-"))

        output_dir.mkdir()
        bridge.close()

        self.assertFalse(output_dir.exists())

    def test_playwright_registry_dir_is_private_and_cleaned_with_bridge(self):
        bridge = MCPToolBridge(None, enable_playwright=True, enable_officecli=False)
        spec = next(item for item in bridge.specs if item.name == "playwright")
        registry_dir = Path(spec.env["PWTEST_SERVER_REGISTRY"])

        self.assertFalse(registry_dir.exists())
        self.assertNotEqual(registry_dir.parent.resolve(), Path(__file__).resolve().parents[1])
        self.assertTrue(registry_dir.name.startswith("deskorb-playwright-registry-"))

        registry_dir.mkdir()
        bridge.close()

        self.assertFalse(registry_dir.exists())

    def test_load_default_playwright_accepts_a_private_registry_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_dir = Path(directory) / "registry"
            specs = load_mcp_servers(None, playwright_registry_dir=registry_dir)
        spec = next(item for item in specs if item.name == "playwright")
        self.assertEqual(Path(spec.env["PWTEST_SERVER_REGISTRY"]), registry_dir.resolve())

    def test_browser_proxy_prefers_explicit_playwright_setting(self):
        with patch.dict(os.environ, {
            "PLAYWRIGHT_MCP_PROXY_SERVER": "http://127.0.0.1:12000",
            "DESKORB_AGENT_BROWSER_PROXY": "http://127.0.0.1:12001",
            "ALL_PROXY": "http://127.0.0.1:12002",
        }, clear=False):
            self.assertEqual(configured_playwright_proxy(), "http://127.0.0.1:12000")

    def test_default_playwright_maps_all_proxy_to_mcp_proxy(self):
        with patch.dict(os.environ, {
            "PLAYWRIGHT_MCP_PROXY_SERVER": "",
            "DESKORB_AGENT_BROWSER_PROXY": "",
            "ALL_PROXY": "",
            "all_proxy": "http://127.0.0.1:12000",
        }, clear=False):
            specs = load_mcp_servers(None)
        spec = next(item for item in specs if item.name == "playwright")
        self.assertEqual(spec.env.get("PLAYWRIGHT_MCP_PROXY_SERVER"), "http://127.0.0.1:12000")

    def test_invalid_browser_proxy_is_ignored(self):
        with patch.dict(os.environ, {
            "PLAYWRIGHT_MCP_PROXY_SERVER": "file:///not-a-proxy",
            "DESKORB_AGENT_BROWSER_PROXY": "not a url",
            "ALL_PROXY": "",
            "all_proxy": "",
        }, clear=False):
            self.assertEqual(configured_playwright_proxy(), "")

    def test_stdio_close_terminates_the_owned_mcp_process_tree_on_windows(self):
        class FakeProcess:
            pid = 4321

            def __init__(self):
                self.terminated = False
                self.killed = False

            def poll(self):
                return None if not (self.terminated or self.killed) else 0

            def wait(self, timeout=None):
                if not (self.terminated or self.killed):
                    raise TimeoutError("process still running")
                return 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        client = StdioMCPClient(MCPServerSpec("playwright", "node", (), {}))
        process = FakeProcess()
        client._process = process
        with patch("mcp_client.os.name", "nt"), \
                patch("mcp_client.subprocess.run") as run:
            run.return_value.returncode = 0
            client.close()

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [
            "taskkill", "/PID", "4321", "/T", "/F",
        ])
        self.assertIsNone(client._process)

    def test_default_servers_include_officecli_when_binary_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "officecli.exe"
            binary.write_bytes(b"stub")
            specs = load_mcp_servers(None, officecli_binary=binary)
        officecli = next(spec for spec in specs if spec.name == "officecli")
        self.assertEqual(officecli.args, ("mcp",))
        self.assertEqual(officecli.env["OFFICECLI_SKIP_UPDATE"], "1")
        self.assertIn("docx", officecli.intent_keywords)

    def test_officecli_uses_a_longer_operation_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "officecli.exe"
            binary.write_bytes(b"stub")
            bridge = MCPToolBridge(None, enable_playwright=False, officecli_binary=binary,
                                   timeout_seconds=30, officecli_timeout_seconds=180)
        self.assertEqual(bridge.clients["officecli"].timeout_seconds, 180)
        bridge.close()

    def test_officecli_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "officecli.exe"
            binary.write_bytes(b"stub")
            specs = load_mcp_servers(None, enable_officecli=False, officecli_binary=binary)
        self.assertNotIn("officecli", [spec.name for spec in specs])

    def test_missing_officecli_binary_does_not_break_existing_mcp(self):
        with patch("mcp_client.resolve_officecli_binary", return_value=None):
            specs = load_mcp_servers(None, officecli_binary=Path("missing-officecli.exe"))
        self.assertEqual([spec.name for spec in specs[:2]], ["playwright", "powertoys"])
        self.assertNotIn("officecli", [spec.name for spec in specs])

    def test_custom_mcp_config_preserves_existing_replacement_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "officecli.exe"
            binary.write_bytes(b"stub")
            path = root / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {
                "knowledge": {"command": "knowledge-mcp"},
            }}), encoding="utf-8")
            specs = load_mcp_servers(path, officecli_binary=binary)
        self.assertEqual([spec.name for spec in specs], ["knowledge"])

    def test_officecli_binary_resolution_uses_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "officecli.exe"
            binary.write_bytes(b"stub")
            self.assertEqual(resolve_officecli_binary(binary), str(binary.resolve()))

    def test_officecli_command_verbs_are_classified_from_arrays_and_strings(self):
        class OfficeClient:
            def list_tools(self):
                return [{"name": "officecli", "inputSchema": {
                    "type": "object", "properties": {"command": {}}, "required": ["command"]}}]

            def close(self):
                return None

        bridge = MCPToolBridge(None, enable_playwright=False, enable_officecli=False)
        bridge.clients = {"officecli": OfficeClient()}
        office_schema = bridge.schemas(["officecli"])[0]
        name = office_schema["name"]
        self.assertEqual(office_schema["parameters"]["required"], ["command"])
        self.assertEqual(bridge.server_name(name), "officecli")
        self.assertTrue(bridge.is_read_only_call(name, {"command": ["view", "report.docx", "text"]}))
        self.assertFalse(bridge.is_high_risk(name, {"command": 'view "report.docx" text'}))
        self.assertFalse(bridge.is_read_only_call(name, {
            "command": ["set", "report.docx", "/body/p[1]", "--prop", "text=Updated"]}))
        self.assertTrue(bridge.is_high_risk(name, {
            "command": 'set "report.docx" "/body/p[1]" --prop text=Updated'}))
        self.assertTrue(bridge.is_high_risk(name, {"command": ["unknown", "report.docx"]}))
        summary = bridge.command_summary(name, {"command": ["set", "report.docx", "/body/p[1]"]})
        self.assertIn("OfficeCLI file operation:", summary)
        self.assertIn("report.docx", summary)

    def test_officecli_batch_string_is_forwarded_as_quote_safe_argv(self):
        captured = {}

        class OfficeClient:
            def list_tools(self):
                return [{"name": "officecli", "inputSchema": {
                    "type": "object", "properties": {"command": {}}, "required": ["command"]}}]

            def call_tool(self, name, arguments):
                captured.update(arguments)
                return {"content": [{"type": "text", "text": "ok"}]}

            def close(self):
                return None

        bridge = MCPToolBridge(None, enable_playwright=False, enable_officecli=False)
        bridge.clients = {"officecli": OfficeClient()}
        name = bridge.schemas(["officecli"])[0]["name"]
        command = (
            'batch "C:\\Reports\\Warhammer 40K.docx" --commands '
            '[{"command":"add","parent":"/body","type":"paragraph",'
            '"props":{"text":"A long paragraph"}}] --json'
        )

        result = bridge.call(name, {"command": command})

        self.assertTrue(result["ok"])
        self.assertIsInstance(captured["command"], list)
        self.assertEqual(captured["command"][0:3], [
            "batch", "C:\\Reports\\Warhammer 40K.docx", "--commands",
        ])
        self.assertEqual(json.loads(captured["command"][3])[0]["props"]["text"], "A long paragraph")
        self.assertEqual(captured["command"][-1], "--json")

    def test_officecli_lock_errors_explain_save_and_close(self):
        class LockedClient:
            def list_tools(self):
                return [{"name": "officecli", "inputSchema": {"type": "object"}}]

            def call_tool(self, name, arguments):
                return {"isError": True, "content": [{"type": "text",
                        "text": "sharing violation: file is locked by another process"}]}

            def close(self):
                return None

        bridge = MCPToolBridge(None, enable_playwright=False, enable_officecli=False)
        bridge.clients = {"officecli": LockedClient()}
        name = bridge.schemas(["officecli"])[0]["name"]
        result = bridge.call(name, {"command": ["set", "C:\\Reports\\locked report.docx", "/body"]})
        self.assertFalse(result["ok"])
        self.assertIn("Save and close", result["error"])
        self.assertIn("locked report.docx", result["error"])

    def test_powertoys_reads_are_observations_but_apply_is_forced_high_risk(self):
        class PowerToysClient:
            def list_tools(self):
                return [
                    {"name": "powertoys_get_settings", "inputSchema": {"type": "object", "properties": {}, "required": []}},
                    {"name": "powertoys_apply_settings", "inputSchema": {"type": "object", "properties": {}, "required": []}},
                ]

            def call_tool(self, name, arguments):
                return {"content": [{"type": "text", "text": "{}"}]}

            def close(self):
                return None

        bridge = MCPToolBridge(None, enable_playwright=True)
        bridge.clients = {"powertoys": PowerToysClient()}
        schemas = bridge.schemas()
        get_name = next(item["name"] for item in schemas if item["name"].endswith("powertoys_get_settings"))
        apply_name = next(item["name"] for item in schemas if item["name"].endswith("powertoys_apply_settings"))
        self.assertFalse(bridge.is_action(get_name))
        self.assertTrue(bridge.is_action(apply_name))
        self.assertTrue(bridge.is_high_risk(apply_name))

    def test_selected_schemas_start_only_the_requested_server(self):
        class TrackingClient(FakeClient):
            def __init__(self):
                self.list_calls = 0

            def list_tools(self):
                self.list_calls += 1
                return super().list_tools()

        bridge = MCPToolBridge(None, enable_playwright=True)
        playwright = TrackingClient()
        powertoys = TrackingClient()
        bridge.clients = {"playwright": playwright, "powertoys": powertoys}

        schemas = bridge.schemas(["playwright"])

        self.assertTrue(schemas)
        self.assertEqual(playwright.list_calls, 1)
        self.assertEqual(powertoys.list_calls, 0)
        self.assertTrue(all(item["name"].startswith("mcp_playwright_") for item in schemas))

    def test_desktop_uia_backend_tools_are_internal_and_not_model_exposed(self):
        class WindowsClient:
            def list_tools(self):
                return [
                    {"name": "windows_snapshot", "inputSchema": {"type": "object"}},
                    {"name": "windows_click", "inputSchema": {"type": "object"}},
                    {"name": "windows_batch", "inputSchema": {"type": "object"}},
                ]

            def close(self):
                return None

        bridge = MCPToolBridge(None, enable_playwright=False, enable_officecli=False)
        bridge.specs = [MCPServerSpec(
            name="windows", command="", args=(), env={}, capability="desktop_uia",
            allowed_tools=("windows_snapshot", "windows_click", "windows_batch"),
            read_only_tools=("windows_snapshot",), action_tools=("windows_click", "windows_batch"),
        )]
        bridge.clients = {"windows": WindowsClient()}
        schemas = bridge.schemas(["windows"])
        self.assertTrue(bridge.is_internal_backend_server("windows"))
        self.assertFalse(bridge.is_model_exposed_server("windows"))
        self.assertTrue(schemas)

    def test_stdio_tool_call_accepts_a_per_call_timeout_budget(self):
        client = StdioMCPClient(MCPServerSpec("windows", "fla-ui", (), {}), timeout_seconds=30)
        with patch.object(client, "start"), patch.object(client, "_request", return_value={"content": []}) as request:
            result = client.call_tool("windows_snapshot", {"handle": "w1"}, timeout_seconds=2)
        self.assertEqual(result, {"content": []})
        request.assert_called_once_with(
            "tools/call", {"name": "windows_snapshot", "arguments": {"handle": "w1"}},
            timeout_seconds=2,
        )

    def test_failed_server_discovery_is_not_repeated_in_one_runtime(self):
        class FailingClient:
            def __init__(self):
                self.list_calls = 0

            def list_tools(self):
                self.list_calls += 1
                from mcp_client import MCPError
                raise MCPError("offline")

            def close(self):
                return None

        bridge = MCPToolBridge(None, enable_playwright=True)
        client = FailingClient()
        bridge.clients = {"playwright": client}
        bridge.schemas(["playwright"])
        bridge.schemas(["playwright"])
        self.assertEqual(client.list_calls, 1)


if __name__ == "__main__":
    unittest.main()
