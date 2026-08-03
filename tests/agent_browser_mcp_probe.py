"""End-to-end probe for DeskOrb's own Agent + local Playwright MCP bridge.

The target is intentionally a public demo catalogue: no login, cart operation,
form submission, or user-browser profile is involved.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows consoles may still default to GBK. The public fixture intentionally
# contains £ prices, so make the probe's JSON evidence portable.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL


TASK = (
    "使用本地 MCP 浏览器完成测试：打开 https://books.toscrape.com/，从商品列表找到任意一本"
    "价格在 £20 到 £30（含）之间的书。不要登录、加入购物车、提交表单或购买。"
    "最后只报告书名和页面显示的价格。"
)


def _drain(events: Queue) -> list[tuple[str, object]]:
    result = []
    while True:
        try:
            result.append(events.get_nowait())
        except Empty:
            return result


def _approval_token(events: list[tuple[str, object]]) -> str | None:
    for kind, value in events:
        if kind == "approval":
            match = re.search(r"确认\s+([A-F0-9]{6,})", str(value))
            if match:
                return match.group(1)
    return None


def main() -> int:
    events: Queue = Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL)
    try:
        runtime.run_turn(TASK, [])
        first = _drain(events)
        token = _approval_token(first)
        if token:
            runtime.run_turn(f"确认 {token}", [])
        all_events = [*first, *_drain(events)]
        answer = "\n".join(str(value) for kind, value in all_events if kind == "delta")
        mcp_calls = sum(1 for kind, value in all_events if kind == "tool" and value and value[0] == "MCP browser tool")
        result = {
            "ok": bool(token and mcp_calls and re.search(r"£\s?\d+(?:\.\d+)?", answer)),
            "approval_used": bool(token),
            "mcp_tool_calls": mcp_calls,
            "answer": answer[-2000:],
            "events": [kind for kind, _value in all_events],
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:1000]}, ensure_ascii=False))
        return 3
    finally:
        if runtime.mcp:
            runtime.mcp.close()


if __name__ == "__main__":
    raise SystemExit(main())
