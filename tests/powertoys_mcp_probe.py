"""Read-only smoke probe for the local PowerToys MCP server."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_client import MCPServerSpec, StdioMCPClient


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    client = StdioMCPClient(MCPServerSpec("powertoys", sys.executable,
                                          (str(root / "powertoys_mcp.py"),), {}, str(root)))
    try:
        tools = client.list_tools()
        status = client.call_tool("powertoys_status", {})
        awake = client.call_tool("powertoys_get_settings", {"module": "Awake"})
        content = awake.get("content") or []
        parsed = json.loads(content[0]["text"]) if content else {}
        properties = parsed.get("settings", {}).get("settings", {}).get("properties", {})
        dry_run = client.call_tool("powertoys_test_settings", {
            "module": "Awake", "properties": {"keepDisplayOn": bool(properties.get("keepDisplayOn", False))},
        })
        backups = client.call_tool("powertoys_list_backups", {})
        result = {
            "ok": bool(status and awake and dry_run),
            "tool_count": len(tools),
            "status": status.get("content"),
            "awake_read": bool(parsed.get("ok")),
            "dry_run_error": bool(dry_run.get("isError")),
            "dry_run": dry_run.get("content"),
            "backups_error": bool(backups.get("isError")),
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] and not result["dry_run_error"] else 2
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
