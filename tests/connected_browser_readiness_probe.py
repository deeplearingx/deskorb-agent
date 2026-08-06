"""Privacy-safe readiness check for an explicitly connected browser profile.

The probe never clicks, types, navigates, submits, or changes account state. It
only lists the connected tab set and reads one snapshot to determine whether a
manual login/CAPTCHA handoff is still required. Page text and URLs are not
printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_client import MCPToolBridge


AUTH_MARKERS = (
    "请登录", "登陆后", "登录后", "登录以继续", "需要登录", "log in to continue",
    "login required", "sign in to continue", "password", "密码", "验证码", "captcha",
    "我是人类", "快速验证身份", "qr code", "二维码",
)


def _payload_text(result: dict[str, Any]) -> str:
    try:
        return json.dumps(result.get("content", result), ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return str(result.get("content", "")).lower()


def run(*, require_connected: bool = False, require_authenticated: bool = False) -> dict[str, Any]:
    bridge = MCPToolBridge(None, enable_playwright=True,
                           timeout_seconds=30, playwright_mode="connected-playwright")
    try:
        if "playwright" not in set(bridge.available_servers):
            return {"ok": not require_connected, "status": "playwright_not_configured"}
        bridge.schemas_for_task(("playwright",))
        tabs = bridge.find_tool("playwright", ("browser_tabs",))
        snapshot = bridge.find_tool("playwright", ("browser_snapshot",))
        if not tabs or not snapshot:
            return {"ok": False, "status": "connected_tools_missing",
                    "tabs_tool": bool(tabs), "snapshot_tool": bool(snapshot)}
        tab_result = bridge.call(tabs, {"action": "list"})
        if not tab_result.get("ok"):
            return {"ok": False, "status": "connected_tabs_unavailable"}
        snapshot_result = bridge.call(snapshot, {})
        if not snapshot_result.get("ok"):
            return {"ok": False, "status": "connected_snapshot_unavailable"}
        text = _payload_text(snapshot_result)
        auth_required = any(marker in text for marker in AUTH_MARKERS)
        return {"ok": not (require_authenticated and auth_required),
                "status": "human_handoff_required" if auth_required else "connected_ready",
                "auth_or_captcha_detected": auth_required,
                "tabs_observed": True,
                "snapshot_observed": True}
    finally:
        bridge.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check an explicitly connected browser profile")
    parser.add_argument("--require-connected", action="store_true",
                        help="Fail if the Playwright extension/session is unavailable.")
    parser.add_argument("--require-authenticated", action="store_true",
                        help="Fail if the snapshot still contains login/CAPTCHA markers.")
    args = parser.parse_args(argv)
    result = run(require_connected=args.require_connected,
                 require_authenticated=args.require_authenticated)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
