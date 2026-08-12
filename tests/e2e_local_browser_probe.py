"""Run repeatable browser tasks through DeskOrb's real Agent and Playwright MCP.

The only external request is to the user-configured model provider. Browser pages
are served from this process on loopback and contain no login, analytics, cart, or
form submission. Output is privacy-safe normalized metrics; it never emits the
loopback URL, page text, model answer, tool parameters, screenshots, or credentials.
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
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL, API_TIMEOUT, MODEL_PROVIDER


ROOT = Path(__file__).with_name("fixtures") / "web"
CASES = {
    "web-001": {
        "page": "mock_store.html",
        "task": "使用本地 MCP 浏览器完成测试：打开 {url}。在页面中找到价格在 100 到 150 元之间、深色、100% 纯棉的男士 T 恤。不要购买。最后报告商品名称、价格、评分和详情链接。",
        "needles": ("深灰纯棉圆领", "129", "4.8", "/products/deep-gray-cotton-tee"),
        "extract_fields": ("title", "price", "url"),
        "verify_contains": ("深灰纯棉圆领",),
    },
    "research-001": {
        "page": "dynamic_search.html",
        "task": "使用本地 MCP 浏览器完成动态自动补全测试：打开 {url}。在搜索框中输入 Python asyncio，等待下拉选项出现，只点击名为‘Python asyncio 入门’的选项，不要再次重复输入，也不要使用地址栏或桌面坐标。最后报告页面显示的已选择结果和来源。",
        "needles": ("Python asyncio 入门", "官方文档"),
        "extract_fields": ("title", "source"),
        "verify_contains": ("Python asyncio 入门", "官方文档"),
    },
    "dynamic-search-no-progress": {
        "page": "dynamic_search.html",
        "task": "使用本地 MCP 浏览器打开 {url}，输入 Python asyncio 后只允许点击一个实际出现的下拉选项。",
        "needles": ("Python asyncio 入门", "官方文档"),
        "extract_fields": ("title", "source"),
        "verify_contains": ("Python asyncio 入门", "官方文档"),
    },
}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except ConnectionResetError:
            # Playwright may close a keep-alive connection while the loopback
            # server is shutting down. That is expected cleanup, not a probe
            # failure, and should not produce a misleading traceback.
            return


def drain(events: Queue) -> list[tuple[str, object]]:
    items = []
    while True:
        try:
            items.append(events.get_nowait())
        except Empty:
            return items


def _verification_evidence_passed(results: list[dict[str, object]]) -> bool:
    """Use the last explicit postcondition check after bounded recovery.

    A recoverable browser verification may fail before the model takes a fresh
    snapshot and retries.  The terminal task state is only accepted when the
    final explicit ``verify`` observation passes; an earlier failed attempt is
    retained in the in-memory diagnostic list but does not create a false
    negative after a successful recovery.
    """
    final_verify = next(
        (item for item in reversed(results)
         if item.get("source") == "observation" and item.get("action") == "verify"),
        None,
    )
    return bool(final_verify and final_verify.get("passed"))


def approval_token(events: list[tuple[str, object]]) -> str | None:
    for kind, value in events:
        if kind == "approval":
            match = re.search(r"确认\s+([A-F0-9]{6,})", str(value))
            if match:
                return match.group(1)
    return None


def _run_turn_bounded(runtime: AgentRuntime, text: str, timeout_seconds: int | None = None) -> tuple[bool, str | None]:
    """Run one model turn without allowing a provider/MCP hang to stall the matrix.

    The runtime already bounds its HTTP request, but a provider stream, MCP
    subprocess, or tool callback can still leave the outer turn waiting.  The
    probe must report that as a contained failure and continue collecting
    privacy-safe metrics instead of hanging the whole local test process.
    """
    timeout = max(1, int(timeout_seconds if timeout_seconds is not None else API_TIMEOUT + 5))
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            runtime.run_turn(text, [])
        except BaseException as exc:  # surfaced as a bounded failure category below
            errors.append(exc)

    worker = threading.Thread(target=invoke, name="deskorb-browser-probe-turn", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        runtime.interrupt()
        worker.join(2)
        return False, "provider_or_tool_timeout"
    if errors:
        return False, type(errors[0]).__name__.lower()
    return True, None


def run_case(case_id: str, base_url: str, runtime: AgentRuntime, events: Queue,
             timeout_seconds: int | None = None) -> dict[str, object]:
    started = time.perf_counter()
    verification_results: list[dict[str, object]] = []
    original_dispatch = runtime._run_local_tool

    def record_with_verification(name, arguments):
        result = original_dispatch(name, arguments)
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
        return result

    runtime._run_local_tool = record_with_verification
    case = CASES[case_id]
    task = case["task"].format(url=base_url + "/" + case["page"])
    task += (
        "\n完成前必须通过 browser_action_batch 获取当前快照，并使用 extract 提取页面中"
        "实际显示的目标字段，再使用 verify 验证这些字段；不能只用最终文字回答代替结构化验证。"
    )
    fields = list(case.get("extract_fields") or ())
    contains = list(case.get("verify_contains") or ())
    if fields:
        task += (" extract 时字段必须使用：" + ", ".join(fields)
                 + "；verify 时 required_fields 必须使用同一组字段")
    if contains:
        task += "，并在 verify 的 contains 中检查：" + "、".join(contains)
    task += "。verify 必须是最后一个浏览器动作，且不要把多个状态动作放在同一批次。"
    failure_kind = None
    try:
        first_ok, first_failure = _run_turn_bounded(runtime, task, timeout_seconds)
        failure_kind = first_failure
        first = drain(events)
        token = approval_token(first)
        if first_ok and token:
            second_ok, second_failure = _run_turn_bounded(runtime, "确认 " + token, timeout_seconds)
            if not second_ok:
                failure_kind = second_failure
        all_events = [*first, *drain(events)]
        tool_calls = [value for kind, value in all_events if kind == "tool"]
        mcp_calls = sum(1 for value in tool_calls if isinstance(value, tuple)
                        and (value[0] in {"MCP browser tool", "Browser action", "browser_action_batch"}
                             or str(value[0]).startswith("MCP browser")))
        progress = [value for kind, value in all_events
                    if kind == "task_progress" and isinstance(value, dict)]
        final_progress = progress[-1] if progress else {}
        verified = (final_progress.get("terminal") == "completed"
                    and bool(final_progress.get("verified")))
        evidence_passed = _verification_evidence_passed(verification_results)
        return {"id": case_id, "ok": bool(token and mcp_calls and verified and evidence_passed),
                "approval_used": bool(token), "mcp_tool_calls": mcp_calls,
                "terminal": final_progress.get("terminal"),
                "verified": bool(final_progress.get("verified")),
                "evidence_passed": evidence_passed,
                "failure_kind": failure_kind,
                "verification_results": verification_results,
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        return {"id": case_id, "ok": False, "error_type": type(exc).__name__,
                "failure_kind": type(exc).__name__.lower(),
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    finally:
        runtime._run_local_tool = original_dispatch
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


def _normalized_run(item: dict[str, object]) -> dict[str, object]:
    """Convert loopback-run results to the shared content-free metric format."""
    completed = item.get("terminal") == "completed"
    verified = bool(item.get("verified"))
    evidence_passed = bool(item.get("evidence_passed"))
    passed = bool(item.get("ok")) and completed and verified and evidence_passed
    return {
        "case_id": str(item.get("id") or "unknown"),
        "attempt": int(item.get("attempt") or 1),
        "outcome": "passed" if passed else "failed",
        "failure_kind": None if passed else (str(item.get("failure_kind") or "unverified_terminal_state")),
        "total_latency_ms": item.get("elapsed_ms"),
        "tool_rounds": int(item.get("mcp_tool_calls") or 0),
        "approval_used": bool(item.get("approval_used")),
        "needs_task_confirmation": True,
        "task_confirmation_once": bool(item.get("approval_used")),
        "completed": completed,
        "verified": verified,
        "safety_passed": True,
        "requires_evidence": True,
        "evidence_passed": evidence_passed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run real local browser-agent E2E cases")
    parser.add_argument("--repetitions", type=int, default=1,
                        help="Fresh AgentRuntime per repetition (1-20; opt-in real matrix).")
    parser.add_argument("--case", choices=("all", *CASES), default="all",
                        help="Run one focused case or the complete local probe set.")
    parser.add_argument("--timeout-seconds", type=int, default=max(1, API_TIMEOUT + 5),
                        help="Bound each model turn so a provider/MCP hang becomes a failure.")
    args = parser.parse_args(argv)
    repetitions = max(1, min(20, int(args.repetitions)))
    case_ids = list(CASES) if args.case == "all" else [args.case]
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
            with tempfile.TemporaryDirectory(prefix="deskorb-browser-probe-") as working_dir:
                events: Queue = Queue()
                runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL,
                                       model_provider=MODEL_PROVIDER, working_dir=working_dir)
                try:
                    for case_id in case_ids:
                        result = run_case(case_id, base_url, runtime, events,
                                          timeout_seconds=max(1, int(args.timeout_seconds)))
                        result["attempt"] = attempt
                        reports.append(result)
                finally:
                    if runtime.mcp:
                        runtime.mcp.close()
        grouped: dict[str, list[dict[str, object]]] = {case_id: [] for case_id in case_ids}
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
        runs = [_normalized_run(item) for item in reports]
        payload = {"ok": bool(runs) and all(item["outcome"] == "passed" for item in runs),
                   "repetitions": repetitions, "runs": runs, "cases": case_summary}
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload["ok"] else 2
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
