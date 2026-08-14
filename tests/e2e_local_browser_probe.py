"""Run repeatable browser tasks through DeskOrb's real Agent and Playwright MCP.

The only external request is to the user-configured model provider. Browser pages
are served from this process on loopback and contain no login, analytics, cart, or
form submission. Output is privacy-safe normalized metrics; it never emits the
loopback URL, page text, model answer, tool parameters, screenshots, or credentials.
"""
from __future__ import annotations

import contextlib
import functools
import inspect
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
from task_runtime import ExecutionDeadline, classify_failure


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
         if item.get("action") == "verify"
         and item.get("source") in {"observation", "cache_tool_result"}),
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
    deadline = ExecutionDeadline(timeout)

    def invoke() -> None:
        try:
            parameters = inspect.signature(runtime.run_turn).parameters
            supports_deadline = "deadline" in parameters or any(
                item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
            )
            if supports_deadline:
                runtime.run_turn(text, [], deadline=deadline)
            else:
                runtime.run_turn(text, [])
        except BaseException as exc:  # surfaced as a bounded failure category below
            errors.append(exc)

    worker = threading.Thread(target=invoke, name="deskorb-browser-probe-turn", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        runtime.interrupt()
        worker.join(2)
        try:
            runtime.last_deadline_snapshot = deadline.snapshot()
        except Exception:
            pass
        phase = str(getattr(runtime, "execution_phase", "") or "")
        if phase == "tool_execution":
            failure = "tool_execution_timeout"
        elif phase == "desktop_observation":
            failure = "desktop_observation_timeout"
        elif phase == "postcondition_verification":
            failure = "postcondition_verification_timeout"
        elif bool(getattr(runtime, "turn_action_dispatched", False)):
            failure = "provider_timeout_after_tools"
        else:
            failure = "provider_timeout_before_tools"
        return False, failure
    if errors:
        category = classify_failure(str(errors[0]))
        return False, category if category != "unknown" else type(errors[0]).__name__.lower()
    return True, None


def _classify_probe_timeout(failure_kind: str | None,
                            browser_trace: list[dict[str, object]]) -> str | None:
    """Separate model-planning stalls from a browser action stall.

    A successful browser tool result followed by no next model response is a
    provider/planner timeout, not evidence that the browser action failed.
    """
    if failure_kind != "provider_or_tool_timeout":
        return failure_kind
    if not browser_trace:
        return failure_kind
    latest = browser_trace[-1]
    if latest.get("ok") and latest.get("action_types"):
        return "model_planning_timeout"
    if latest.get("started") and not latest.get("ok"):
        return "browser_action_timeout"
    return failure_kind


def run_case(case_id: str, base_url: str, runtime: AgentRuntime, events: Queue,
             timeout_seconds: int | None = None, *,
             definition: dict[str, object] | None = None) -> dict[str, object]:
    started = time.perf_counter()
    verification_results: list[dict[str, object]] = []
    browser_trace: list[dict[str, object]] = []
    original_dispatch = runtime._run_local_tool

    def record_with_verification(name, arguments):
        if name == "browser_action_batch" and isinstance(arguments, dict):
            browser_trace.append({
                "started": True,
                "action_types": [
                    str(item.get("action") or "")[:32]
                    for item in arguments.get("actions") or []
                    if isinstance(item, dict)
                ][:8],
            })
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
        if name == "browser_action_batch" and isinstance(result, dict):
            completed_trace = {
                "ok": bool(result.get("ok")),
                "failure_kind": str(result.get("failure_kind") or "")[:80] or None,
                "state_changed": bool(result.get("state_changed")),
                "action_steps": int(result.get("action_steps") or 0),
                "interaction_stage": str(result.get("interaction_stage") or "")[:32] or None,
                "stage_transition_count": int(result.get("stage_transition_count") or 0),
                "execution_source": str(result.get("execution_source") or "model")[:16],
                "cache_status": str(result.get("cache_status") or "miss")[:16],
                "model_fallback": bool(result.get("model_fallback")),
                "postcondition_passed": bool(result.get("postcondition_passed")),
                "observations": [
                    {
                        "action": str(observation.get("action") or "")[:32],
                        "ok": bool(observation.get("ok")),
                        "state_changed": bool(observation.get("state_changed")),
                        "failure_kind": str(observation.get("failure_kind") or "")[:80] or None,
                    }
                    for observation in result.get("observations") or []
                    if isinstance(observation, dict)
                ][:8],
            }
            browser_trace[-1].update(completed_trace)
        return result

    runtime._run_local_tool = record_with_verification
    # Flagship stability probes may supply an isolated variation definition
    # without expanding the ordinary three-case probe CLI.  Keeping the
    # normal CASES table unchanged preserves the fast smoke contract while
    # allowing each variation to run against its own fixture and evidence
    # fields.
    case = definition if isinstance(definition, dict) else CASES[case_id]
    task = case["task"].format(
        url=base_url + "/" + case["page"],
        working_dir=str(getattr(runtime, "working_dir", "") or ""),
    )
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
    if case.get("verify_postcondition"):
        task += "；verify 的 postcondition 必须使用：" + str(case["verify_postcondition"])
    task += "。verify 必须是最后一个浏览器动作，且不要把多个状态动作放在同一批次。"
    failure_kind = None
    try:
        first_ok, first_failure = _run_turn_bounded(runtime, task, timeout_seconds)
        failure_kind = _classify_probe_timeout(first_failure, browser_trace)
        first = drain(events)
        token = approval_token(first)
        if first_ok and token:
            second_ok, second_failure = _run_turn_bounded(runtime, "确认 " + token, timeout_seconds)
            if not second_ok:
                failure_kind = _classify_probe_timeout(second_failure, browser_trace)
        all_events = [*first, *drain(events)]
        # A cache replay publishes its bounded tool_result directly from the
        # approval continuation, so it does not pass through the model-tool
        # dispatcher wrapper above. Add only cache-sourced results here; model
        # results are already represented by the wrapper trace.
        for kind, value in all_events:
            if (kind != "tool_result" or not isinstance(value, dict)
                    or value.get("tool") != "browser_action_batch"
                    or value.get("execution_source") != "cache"):
                continue
            browser_trace.append({
                "started": False,
                "action_types": [str(item)[:32] for item in value.get("action_types") or []][:8],
                "ok": bool(value.get("ok")),
                "failure_kind": str(value.get("failure_kind") or "")[:80] or None,
                "state_changed": bool(value.get("state_changed")),
                "action_steps": int(value.get("action_steps") or 0),
                "execution_source": "cache",
                "cache_status": str(value.get("cache_status") or "miss")[:16],
                "model_fallback": bool(value.get("model_fallback")),
                "postcondition_passed": bool(value.get("postcondition_passed")),
                "verification_passed": bool(value.get("verification_passed")),
                "stage_transition_count": int(value.get("stage_transition_count") or 0),
            })
            verification_results.append({
                "source": "cache_tool_result",
                "action": "verify",
                "passed": bool(value.get("verification_passed")
                                or value.get("postcondition_passed")),
                "checks": [],
                "observed_chars": None,
                "observed_numbers": [],
            })
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
        bounded_failure = failure_kind or ("unverified_terminal_state" if not verified else None)
        return {"id": case_id, "ok": bool(token and mcp_calls and verified and evidence_passed),
                "approval_used": bool(token), "mcp_tool_calls": mcp_calls,
                "terminal": final_progress.get("terminal"),
                "verified": bool(final_progress.get("verified")),
                "evidence_passed": evidence_passed,
                "failure_kind": bounded_failure,
                "verification_results": verification_results,
                "browser_trace": browser_trace,
                "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        return {"id": case_id, "ok": False, "error_type": type(exc).__name__,
                "failure_kind": type(exc).__name__.lower(),
                "browser_trace": browser_trace,
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
    traces = [trace for trace in item.get("browser_trace") or [] if isinstance(trace, dict)]
    action_sequence: list[str] = []
    execution_source_counts: dict[str, int] = {}
    cache_status_counts: dict[str, int] = {}
    model_fallback_count = 0
    postcondition_seen = False
    postcondition_passed = False
    action_steps = 0
    for trace in traces:
        action_sequence.extend(
            str(action) for action in trace.get("action_types") or [] if str(action)
        )
        source = str(trace.get("execution_source") or "model")
        status = str(trace.get("cache_status") or "miss")
        execution_source_counts[source] = execution_source_counts.get(source, 0) + 1
        cache_status_counts[status] = cache_status_counts.get(status, 0) + 1
        model_fallback_count += int(bool(trace.get("model_fallback")))
        if "postcondition_passed" in trace:
            postcondition_seen = True
            postcondition_passed = postcondition_passed or bool(trace.get("postcondition_passed"))
        try:
            action_steps = max(action_steps, int(trace.get("action_steps") or 0))
        except (TypeError, ValueError):
            pass
    return {
        "case_id": str(item.get("id") or "unknown"),
        "attempt": int(item.get("attempt") or 1),
        "outcome": "passed" if passed else "failed",
        "failure_kind": None if passed else (str(item.get("failure_kind") or "unverified_terminal_state")),
        "total_latency_ms": item.get("elapsed_ms"),
        "tool_rounds": int(item.get("mcp_tool_calls") or 0),
        "action_sequence": action_sequence[:64],
        "action_steps": action_steps,
        "approval_used": bool(item.get("approval_used")),
        "needs_task_confirmation": True,
        "task_confirmation_once": bool(item.get("approval_used")),
        "completed": completed,
        "verified": verified,
        "safety_passed": True,
        "requires_evidence": True,
        "evidence_passed": evidence_passed,
        "execution_source_counts": execution_source_counts,
        "cache_status_counts": cache_status_counts,
        "model_fallback_count": model_fallback_count,
        "postcondition_passed": postcondition_passed if postcondition_seen else None,
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
            normalized_items = [_normalized_run(item) for item in items]
            last = normalized_items[-1] if normalized_items else None
            case_summary.append({"id": case_id, "runs": len(items), "passed": passed,
                                 "success_rate": round(passed / len(items), 4) if items else 0.0,
                                 "p50_elapsed_ms": _p50(elapsed), "p95_elapsed_ms": _p95(elapsed),
                                 "last": last})
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
