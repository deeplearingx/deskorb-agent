"""Run repeatable browser tasks through DeskOrb's real Agent and Playwright MCP.

The only external request is to the user-configured model provider. Browser pages
are served from this process on loopback and contain no login, analytics, cart, or
form submission.  Output is JSON so a benchmark runner can ingest it directly.
"""
from __future__ import annotations

import contextlib
import functools
import json
import re
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL, MODEL_PROVIDER


ROOT = Path(__file__).with_name("fixtures") / "web"
CASES = {
    "web-001": {
        "page": "mock_store.html",
        "task": "使用本地 MCP 浏览器完成测试：打开 {url}。在页面中找到价格在 100 到 150 元之间、深色、100% 纯棉的男士 T 恤。不要购买。最后报告商品名称、价格、评分和详情链接。",
        "needles": ("深灰纯棉圆领", "129", "4.8", "/products/deep-gray-cotton-tee"),
    },
    "research-001": {
        "page": "mock_search.html",
        "task": "使用本地 MCP 浏览器完成测试：打开 {url}。为 Python asyncio 初学者挑出最相关的三个学习资料。每个结果报告标题、来源、链接和一句理由。",
        "needles": ("Python 官方文档", "Real Python", "Bilibili"),
    },
}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return


def drain(events: Queue) -> list[tuple[str, object]]:
    items = []
    while True:
        try:
            items.append(events.get_nowait())
        except Empty:
            return items


def approval_token(events: list[tuple[str, object]]) -> str | None:
    for kind, value in events:
        if kind == "approval":
            match = re.search(r"确认\s+([A-F0-9]{6,})", str(value))
            if match:
                return match.group(1)
    return None


def run_case(case_id: str, base_url: str, runtime: AgentRuntime, events: Queue) -> dict[str, object]:
    case = CASES[case_id]
    try:
        runtime.run_turn(case["task"].format(url=base_url + "/" + case["page"]), [])
        first = drain(events)
        token = approval_token(first)
        if token:
            runtime.run_turn("确认 " + token, [])
        all_events = [*first, *drain(events)]
        answer = "\n".join(str(value) for kind, value in all_events if kind == "delta")
        tool_calls = [value for kind, value in all_events if kind == "tool"]
        mcp_calls = sum(1 for value in tool_calls if isinstance(value, tuple) and value[0] == "MCP browser tool")
        missing = [needle for needle in case["needles"] if needle.lower() not in answer.lower()]
        return {"id": case_id, "ok": bool(token and mcp_calls and not missing), "approval_used": bool(token),
                "mcp_tool_calls": mcp_calls, "missing_evidence": missing, "answer": answer[-3000:]}
    except Exception as exc:
        return {"id": case_id, "ok": False, "error_type": type(exc).__name__, "error": str(exc)[:1000]}
    finally:
        # Keep the local browser process alive for the next isolated agent task.
        # Creating a new Playwright MCP process per case can race over its profile.
        runtime.reset()


def main() -> int:
    handler = functools.partial(QuietHandler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    events: Queue = Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL, model_provider=MODEL_PROVIDER)
    try:
        report = [run_case(case_id, base_url, runtime, events) for case_id in CASES]
        print(json.dumps({"ok": all(item["ok"] for item in report), "base_url": base_url, "cases": report}, ensure_ascii=False))
        return 0 if all(item["ok"] for item in report) else 2
    finally:
        if runtime.mcp:
            runtime.mcp.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
