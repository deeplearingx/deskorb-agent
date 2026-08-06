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
import time
import argparse
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


def run_case(case_id: str, base_url: str, runtime: AgentRuntime, events: Queue,
             *, include_answer: bool = True) -> dict[str, object]:
    started = time.perf_counter()
    verification_results: list[dict[str, object]] = []
    original_record = runtime._record_tool_result

    def record_with_verification(name, arguments, result):
        if isinstance(result, dict) and isinstance(result.get("verification"), dict):
            verification = result["verification"]
            verification_results.append({
                "source": "tool_result",
                "passed": bool(verification.get("passed")),
                "checks": [bool(value) for value in verification.get("checks") or []][:16],
                "observed_chars": verification.get("observed_chars"),
                "observed_numbers": verification.get("observed_numbers"),
            })
        if isinstance(result, dict):
            for observation in result.get("observations") or []:
                if not isinstance(observation, dict) or not isinstance(observation.get("verification"), dict):
                    continue
                verification = observation["verification"]
                verification_results.append({
                    "source": "observation",
                    "action": str(observation.get("action") or "")[:24],
                    "passed": bool(verification.get("passed")),
                    "checks": [bool(value) for value in verification.get("checks") or []][:16],
                    "observed_chars": verification.get("observed_chars"),
                    "observed_numbers": verification.get("observed_numbers"),
                })
        return original_record(name, arguments, result)

    runtime._record_tool_result = record_with_verification
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
        mcp_calls = sum(1 for value in tool_calls if isinstance(value, tuple)
                        and (value[0] == "MCP browser tool" or value[0] == "browser_action_batch"
                             or str(value[0]).startswith("MCP browser")))
        missing = [needle for needle in case["needles"] if needle.lower() not in answer.lower()]
        progress = [value for kind, value in all_events
                    if kind == "task_progress" and isinstance(value, dict)]
        final_progress = progress[-1] if progress else {}
        verified = (final_progress.get("terminal") == "completed"
                    and bool(final_progress.get("verified")))
        return {"id": case_id, "ok": bool(token and mcp_calls and not missing and verified),
                "approval_used": bool(token), "mcp_tool_calls": mcp_calls,
                "missing_evidence": missing, "terminal": final_progress.get("terminal"),
                "verified": bool(final_progress.get("verified")),
                "verification_results": verification_results,
                "answer": answer[-3000:] if include_answer else None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        return {"id": case_id, "ok": False, "error_type": type(exc).__name__, "error": str(exc)[:1000],
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    finally:
        runtime._record_tool_result = original_record
        # Keep the local browser process alive for the next isolated agent task.
        # Creating a new Playwright MCP process per case can race over its profile.
        runtime.reset()


def _p50(values: list[int]) -> int | None:
    if not values:
        return None
    return sorted(values)[len(values) // 2]


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run real local browser-agent E2E cases")
    parser.add_argument("--repetitions", type=int, default=1,
                        help="Fresh AgentRuntime per repetition (1-20; opt-in real matrix).")
    args = parser.parse_args(argv)
    repetitions = max(1, min(20, int(args.repetitions)))
    handler = functools.partial(QuietHandler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    reports: list[dict[str, object]] = []
    try:
        # Use a fresh runtime/MCP browser per repetition. This measures task
        # startup reliability and prevents a stale browser profile or model
        # transcript from making later attempts look better than a new task.
        for attempt in range(1, repetitions + 1):
            events: Queue = Queue()
            runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL,
                                   model_provider=MODEL_PROVIDER)
            try:
                for case_id in CASES:
                    result = run_case(case_id, base_url, runtime, events,
                                      include_answer=repetitions == 1)
                    result["attempt"] = attempt
                    reports.append(result)
            finally:
                if runtime.mcp:
                    runtime.mcp.close()
        grouped: dict[str, list[dict[str, object]]] = {case_id: [] for case_id in CASES}
        for item in reports:
            grouped.setdefault(str(item.get("id") or "unknown"), []).append(item)
        case_summary = []
        for case_id, items in grouped.items():
            elapsed = [int(item["elapsed_ms"]) for item in items if isinstance(item.get("elapsed_ms"), int)]
            passed = sum(1 for item in items if item.get("ok"))
            case_summary.append({"id": case_id, "runs": len(items), "passed": passed,
                                 "success_rate": round(passed / len(items), 4) if items else 0.0,
                                 "p50_elapsed_ms": _p50(elapsed), "p95_elapsed_ms": _p95(elapsed),
                                 "last": items[-1] if items else None})
        payload = {"ok": bool(reports) and all(bool(item.get("ok")) for item in reports),
                   "repetitions": repetitions, "base_url": base_url}
        # Keep the original single-run shape for existing dashboards; the
        # aggregate form is used only when an explicit matrix is requested.
        if repetitions == 1:
            payload["cases"] = reports
        else:
            payload["runs"] = reports
            payload["cases"] = case_summary
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload["ok"] else 2
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
