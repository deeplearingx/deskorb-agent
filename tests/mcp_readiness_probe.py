"""Print privacy-safe MCP tool discovery metadata for local diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_client import MCPToolBridge


def main() -> int:
    bridge = MCPToolBridge(None, enable_playwright=True, timeout_seconds=45)
    try:
        schemas = bridge.schemas_for_task(("playwright",))
        print(json.dumps({
            "servers": list(bridge.available_servers),
            "tool_names": [str(item.get("name") or "") for item in schemas],
            "tool_count": len(schemas),
            "diagnostics": list(bridge.diagnostics),
        }, ensure_ascii=False))
        return 0 if schemas else 2
    finally:
        bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
