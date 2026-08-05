"""Manual end-to-end probe for DeskOrb's browser-agent path on Taobao.

This only searches and reads public results.  It deliberately excludes login,
cart, checkout, messaging, and any other account-changing action.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL


TASK = (
    "使用本地 MCP 浏览器完成测试：打开淘宝网页，搜索‘T恤’，从搜索结果中找到任意一件"
    "价格在 100 到 150 元（含）之间的 T 恤。不要登录、私信商家、加入购物车、提交表单、"
    "购买或绕过验证码。若出现验证码、访问限制或无法获得真实搜索结果，立即停止并明确报告"
    "阻碍原因。最后只报告商品标题、页面显示价格和是否遇到阻碍。"
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
        mcp_calls = sum(
            1
            for kind, value in all_events
            if kind == "tool" and value and value[0] == "MCP browser tool"
        )
        # A live CAPTCHA may stop the model before it produces a final answer.
        # The structured handoff event is authoritative in that case.
        handoff = any(kind == "human_verification" for kind, _value in all_events)
        blocked = handoff or bool(re.search(r"验证码|访问限制|风控|无法获得|阻碍", answer))
        price_found = bool(re.search(r"(?:¥|￥|元)\s?1(?:0\d|[1-4]\d)(?:\.\d+)?", answer))
        compliant = bool(token and mcp_calls and (price_found or blocked))
        result = {
            "ok": compliant,
            "task_completed": bool(price_found and not blocked),
            "status": "completed" if price_found and not blocked else ("blocked_expected" if blocked else "failed"),
            "approval_used": bool(token),
            "mcp_tool_calls": mcp_calls,
            "blocked": blocked,
            "human_handoff": handoff,
            "answer": answer[-3000:],
            "events": [kind for kind, _value in all_events],
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if compliant else 2
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:1000]},
                ensure_ascii=False,
            )
        )
        return 3
    finally:
        if runtime.mcp:
            runtime.mcp.close()


if __name__ == "__main__":
    raise SystemExit(main())
