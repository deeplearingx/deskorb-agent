"""Read-only live acceptance probe for public learning-resource search."""
from __future__ import annotations

import json
import re
import sys
from queue import Empty, Queue
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL, MODEL_PROVIDER


TASK = (
    "打开浏览器，搜索 FastAPI 中文学习资料，优先官方文档和高质量教程，返回最相关的三个结果及链接。"
    "只读浏览，不登录、不输入账号、不下载、不提交表单。若出现验证码、登录或访问限制，立即停下并等待人工处理。"
)


def _drain(events: Queue) -> list[tuple[str, object]]:
    values = []
    while True:
        try:
            values.append(events.get_nowait())
        except Empty:
            return values


def _token(events: list[tuple[str, object]]) -> str | None:
    for kind, value in events:
        if kind == "approval":
            match = re.search(r"确认\s+([A-F0-9]{6,})", str(value))
            if match:
                return match.group(1)
    return None


def main() -> int:
    events: Queue = Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL, model_provider=MODEL_PROVIDER)
    try:
        runtime.run_turn(TASK, [])
        first = _drain(events)
        token = _token(first)
        if token:
            runtime.run_turn("确认 " + token, [])
        observed = [*first, *_drain(events)]
        answer = "\n".join(str(value) for kind, value in observed if kind == "delta")
        mcp_calls = sum(1 for kind, value in observed
                        if kind == "tool" and isinstance(value, tuple) and value[0] == "MCP browser tool")
        handoff = any(kind == "human_verification" for kind, _ in observed)
        links = len(re.findall(r"https?://", answer))
        # A live block is authoritative only when the runtime emits its
        # structured handoff event.  Negated prose such as “未出现验证码”
        # must not turn a successful read-only search into a blocked result.
        blocked = handoff
        completed = links >= 1 and not blocked
        result = {"ok": bool(token and mcp_calls and (completed or blocked)),
                  "status": "completed" if completed else ("blocked_expected" if blocked else "failed"),
                  "approval_used": bool(token), "mcp_tool_calls": mcp_calls,
                  "links_found": links, "human_handoff": handoff,
                  "answer": answer[-3000:]}
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
