"""Print privacy-safe MCP tool discovery metadata for local diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_client import MCPToolBridge, local_playwright_diagnostics

_REQUIRED_TOOLS = frozenset({
    "browser_navigate", "browser_snapshot", "browser_wait_for", "browser_tabs",
})


def main() -> int:
    local_diagnostics = local_playwright_diagnostics()
    bridge = MCPToolBridge(None, enable_playwright=True, timeout_seconds=45)
    try:
        schemas = bridge.schemas_for_task(("playwright",)) if not local_diagnostics else []
        tool_names = set()
        for item in schemas:
            name = str(item.get("name") or "")
            prefix = "mcp_playwright_"
            tool_names.add(name[len(prefix):] if name.startswith(prefix) else name)
        diagnostics = [*local_diagnostics, *bridge.diagnostics]
        missing_tools = sorted(_REQUIRED_TOOLS - tool_names)
        if missing_tools:
            diagnostics.append("required_playwright_tools_missing")
        print(json.dumps({
            "servers": list(bridge.available_servers),
            "tool_count": len(schemas),
            "diagnostics": diagnostics,
        }, ensure_ascii=False))
        return 0 if not diagnostics else 2
    finally:
        bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
