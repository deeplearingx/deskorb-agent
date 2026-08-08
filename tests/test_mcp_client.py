import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_client import MCPToolBridge, load_mcp_servers, resolve_officecli_binary


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
        self.assertEqual(specs[0].args[-1], "@playwright/mcp@latest")

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
