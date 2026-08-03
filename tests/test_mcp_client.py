import json
import tempfile
import unittest
from pathlib import Path

from mcp_client import MCPToolBridge, load_mcp_servers


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


class MCPClientTests(unittest.TestCase):
    def test_standard_mcp_config_loads_enabled_servers_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {
                "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
                "disabled": {"command": "ignore", "enabled": False},
            }}), encoding="utf-8")
            specs = load_mcp_servers(path)
        self.assertEqual([spec.name for spec in specs], ["playwright"])
        self.assertIn("@playwright/mcp@latest", specs[0].args)

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
        playwright = next(spec for spec in specs if spec.name == "playwright")
        self.assertIn("--isolated", playwright.args)
        self.assertIn("powertoys", names)
        powertoys = next(spec for spec in specs if spec.name == "powertoys")
        self.assertTrue(powertoys.args[-1].endswith("powertoys_mcp.py"))

    def test_connected_playwright_mode_requires_extension_instead_of_isolated_profile(self):
        specs = load_mcp_servers(None, enable_playwright=True, playwright_mode="connected-playwright")
        playwright = next(spec for spec in specs if spec.name == "playwright")
        self.assertIn("--extension", playwright.args)
        self.assertNotIn("--isolated", playwright.args)

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

    def test_custom_server_requires_an_explicit_tool_allowlist(self):
        class CustomClient:
            def list_tools(self):
                return [
                    {"name": "knowledge_search", "inputSchema": {"type": "object"}},
                    {"name": "browser_run_code", "inputSchema": {"type": "object"}},
                ]

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {
                "knowledge": {"command": "knowledge-mcp", "deskorb": {
                    "allowed_tools": ["knowledge_search"]}},
            }}), encoding="utf-8")
            bridge = MCPToolBridge(path)
        bridge.clients["knowledge"] = CustomClient()
        schemas = bridge.schemas(["knowledge"])
        self.assertEqual([item["name"] for item in schemas], ["mcp_knowledge_knowledge_search"])

    def test_custom_server_blocks_urls_outside_allowlisted_domains(self):
        class CustomClient:
            def list_tools(self):
                return [{"name": "knowledge_search", "inputSchema": {"type": "object"}}]

            def call_tool(self, _name, _arguments):
                raise AssertionError("blocked URL must not reach MCP")

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {
                "knowledge": {"command": "knowledge-mcp", "deskorb": {
                    "allowed_tools": ["knowledge_search"], "allowed_domains": ["example.test"]}},
            }}), encoding="utf-8")
            bridge = MCPToolBridge(path)
        bridge.clients["knowledge"] = CustomClient()
        name = bridge.schemas(["knowledge"])[0]["name"]
        result = bridge.call(name, {"url": "https://other.test/private"})
        self.assertFalse(result["ok"])
        self.assertIn("allowed_domains", result["error"])


if __name__ == "__main__":
    unittest.main()
