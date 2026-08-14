"""Runner contract for the 12 flagship real-task scenarios.

The existing local-real runner remains the execution engine.  This module
provides a separate, explicit scenario manifest and a privacy-safe repetition
loop so a stability run cannot silently fall back to the 60-case matrix.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from queue import Queue
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import functools
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from e2e_metrics import load_normalized_report, summarize_runs


SCENARIO_MANIFEST = Path(__file__).with_name("real_stability_scenarios.json")
_PRIVATE_KEYS = {"prompt", "url", "content", "arguments", "screenshot", "page_text", "trace_path"}
_REAL_BROWSER_SCENARIOS = {
    "flagship-browser-autocomplete": "research-001",
    "flagship-browser-spa": "flagship-browser-spa",
    "flagship-browser-pagination": "flagship-browser-pagination",
    "flagship-browser-new-tab": "flagship-browser-new-tab",
    "flagship-browser-no-progress": "flagship-browser-no-progress",
    "flagship-browser-stale-ref": "flagship-browser-stale-ref",
}
_REAL_DESKTOP_SCENARIOS = {
    "flagship-desktop-notepad": "desktop-003",
    "flagship-desktop-explorer": "explorer",
    "flagship-desktop-calculator": "calculator",
    "flagship-desktop-focus-modal": "focus_modal",
}
_REAL_CROSS_DOMAIN_SCENARIOS = {
    "flagship-cross-domain-file": "file",
    "flagship-cross-domain-notepad": "notepad",
}


def _browser_definition(scenario_id: str) -> tuple[str, dict[str, Any]] | None:
    """Return one page-specific probe contract without retaining task text."""
    from e2e_local_browser_probe import CASES

    if scenario_id not in _REAL_BROWSER_SCENARIOS:
        return None
    source = _REAL_BROWSER_SCENARIOS[scenario_id]
    if source in CASES:
        return source, dict(CASES[source])
    definitions: dict[str, dict[str, Any]] = {
        "flagship-browser-spa": {
            "page": "spa_search.html",
            "task": "使用本地浏览器夹具打开 {url}，点击‘加载结果’按钮。页面保持同一地址时也必须重新观察，再报告实际显示的结果标题和来源。",
            "extract_fields": ("title", "source"),
            "verify_contains": ("同源 SPA 学习结果", "官方文档"),
        },
        "flagship-browser-pagination": {
            "page": "pagination_search.html",
            "task": "使用本地浏览器夹具打开 {url}，进入第二页，点击‘下一页’后重新观察，只报告第二页实际显示的结果标题。",
            "extract_fields": ("title",),
            "verify_contains": ("第二页高评分 T 恤",),
        },
        "flagship-browser-new-tab": {
            "page": "new_tab_search.html",
            "task": "使用本地浏览器夹具打开 {url}，点击‘打开结果标签页’，然后只切换到新打开的结果标签页，重新观察并报告实际标题和价格。",
            "extract_fields": ("title", "price"),
            "verify_contains": ("新标签页候选结果", "128"),
            "verify_postcondition": "tab_changed",
        },
        "flagship-browser-no-progress": {
            "page": "no_progress_search.html",
            "task": "使用本地浏览器夹具打开 {url}，点击一次‘无效按钮’并观察。如果页面没有进展，进入人工交接或明确失败，绝不重复点击、输入地址栏或使用桌面坐标。",
            "extract_fields": ("title",),
            "verify_contains": (),
        },
        "flagship-browser-stale-ref": {
            "page": "stale_ref_search.html",
            "task": "使用本地浏览器夹具打开 {url}，点击‘重新渲染结果’。重渲染后必须使用新快照中的语义引用，只报告实际显示的标题和来源。",
            "extract_fields": ("title", "source"),
            "verify_contains": ("重渲染候选结果", "官方文档"),
        },
    }
    definition = definitions.get(scenario_id)
    return (scenario_id, definition) if definition else None


def _safe_trace_hash(raw: Mapping[str, Any]) -> str:
    """Hash only bounded action outcomes; never hash URLs or page content."""
    material: list[dict[str, Any]] = []
    for item in raw.get("browser_trace") or ():
        if not isinstance(item, dict):
            continue
        material.append({
            "actions": [str(value)[:32] for value in item.get("action_types") or ()][:8],
            "ok": bool(item.get("ok")),
            "state_changed": bool(item.get("state_changed")),
            "failure_kind": str(item.get("failure_kind") or "")[:80],
            "execution_source": str(item.get("execution_source") or "model")[:16],
            "cache_status": str(item.get("cache_status") or "miss")[:16],
        })
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _stage_transition_total(trace: Any) -> int:
    """Read the cumulative stage counter without double-counting events."""
    values = [
        int(item.get("stage_transition_count") or 0)
        for item in (trace or ())
        if isinstance(item, dict)
    ]
    return max(values, default=0)


def _real_notepad_case(scenario: Mapping[str, Any], attempt: int, *,
                       working_dir: str, allow_current_desktop: bool = False) -> dict[str, Any]:
    """Run the existing isolated Notepad AgentRuntime path when authorized."""
    if not allow_current_desktop:
        return {
            "outcome": "blocked", "failure_kind": "current_desktop_not_authorized",
            "environment_class": "current_desktop",
            "postcondition_kind": scenario.get("postcondition_kind"),
        }
    # The authorization flag is not evidence that this process is attached to
    # the interactive desktop. Re-run the bounded preflight immediately before
    # the real adapter so a stale or headless session fails closed.
    from local_real_e2e_runner import preflight_current_desktop

    preflight = preflight_current_desktop(require_foreground=False)
    if not bool(preflight.get("ok")):
        return {
            "outcome": "blocked", "failure_kind": "current_desktop_preflight_failed",
            "environment_class": "current_desktop",
            "postcondition_kind": scenario.get("postcondition_kind"),
        }
    from local_real_e2e_runner import load_matrix_cases, load_step_baselines, run_case

    target = next((item for item in load_matrix_cases() if str(item.get("id")) == "desktop-003"), None)
    if not isinstance(target, dict):
        return {"outcome": "blocked", "failure_kind": "flagship_desktop_case_missing",
                "environment_class": "current_desktop",
                "postcondition_kind": scenario.get("postcondition_kind")}
    all_baselines = load_step_baselines(Path(__file__).with_name("e2e_step_baselines.json"))
    baselines = {"desktop-003": all_baselines["desktop-003"]}
    result = run_case(
        target, attempt=attempt, working_dir=Path(working_dir), baselines=baselines,
        public_ready=False, desktop_ready=True, allow_public=False, allow_desktop=True,
        fixture_base_url=None, handoff_timeout_seconds=120, interactive_handoff=False,
        timeout_seconds=90,
    )
    return {
        "outcome": str(result.get("outcome") or "failed"),
        "failure_kind": result.get("failure_kind"),
        "completed": bool(result.get("completed")),
        "verified": bool(result.get("verified")),
        "evidence_passed": bool(result.get("evidence_passed")),
        "safety_passed": bool(result.get("safety_passed", True)),
        "total_latency_ms": result.get("total_latency_ms"),
        "tool_rounds": result.get("tool_rounds", 0),
        "action_steps": result.get("action_steps", 0),
        "environment_class": "current_desktop",
        "postcondition_kind": scenario.get("postcondition_kind"),
        "recovery_count": result.get("recovery_count", 0),
        "stage_transition_count": result.get("stage_transition_count", 0),
        "cache_status": "miss",
        "trace_hash": str(result.get("trace_hash") or ""),
    }


def _real_desktop_adapter_case(scenario: Mapping[str, Any], attempt: int, *,
                               working_dir: str, allow_current_desktop: bool = False) -> dict[str, Any]:
    """Run one non-Notepad app through the internal semantic adapter."""
    if not allow_current_desktop:
        return {"outcome": "blocked", "failure_kind": "current_desktop_not_authorized",
                "environment_class": "current_desktop",
                "postcondition_kind": scenario.get("postcondition_kind")}
    from local_real_e2e_runner import preflight_current_desktop

    if not bool(preflight_current_desktop(require_foreground=False).get("ok")):
        return {"outcome": "blocked", "failure_kind": "current_desktop_preflight_failed",
                "environment_class": "current_desktop",
                "postcondition_kind": scenario.get("postcondition_kind")}
    from agent_runtime import AgentRuntime
    from config import API_BASE_URL, API_MODEL, API_PROXY_URL, MODEL_PROVIDER

    application = _REAL_DESKTOP_SCENARIOS.get(str(scenario.get("id") or ""), "")
    if application in {"", "desktop-003"}:
        return {"outcome": "blocked", "failure_kind": "desktop_adapter_routing_error",
                "environment_class": "current_desktop",
                "postcondition_kind": scenario.get("postcondition_kind")}
    started = time.monotonic()
    runtime = AgentRuntime(Queue(), API_MODEL, API_BASE_URL, API_PROXY_URL,
                           model_provider=MODEL_PROVIDER, working_dir=working_dir)
    try:
        if runtime.uia.semantic_backend is None or not bool(runtime.uia.semantic_backend.available):
            return {"outcome": "blocked", "failure_kind": "desktop_backend_unavailable",
                    "environment_class": "current_desktop",
                    "postcondition_kind": scenario.get("postcondition_kind")}
        if application == "focus_modal":
            launched = runtime._run_local_tool("application_launch", {"application": "notepad"})
            if not isinstance(launched, dict) or not launched.get("ok"):
                failure = str((launched or {}).get("failure_kind") or "desktop_backend_unavailable")
                return {"outcome": "blocked", "failure_kind": failure,
                        "environment_class": "current_desktop",
                        "postcondition_kind": scenario.get("postcondition_kind")}
            observed = runtime._run_local_tool("desktop_uia_observe", {
                "window_handle": int(launched.get("window_handle") or 0), "max_elements": 120,
            })
            failure = ("desktop_modal_dialog" if observed.get("requires_user_attention")
                       else "desktop_modal_fixture_unavailable")
            return {"outcome": "blocked", "failure_kind": failure,
                    "safety_passed": True, "environment_class": "current_desktop",
                    "postcondition_kind": scenario.get("postcondition_kind"),
                    "total_latency_ms": round((time.monotonic() - started) * 1000, 2)}
        if application == "calculator":
            result = runtime.run_desktop_adapter("calculator", expression="2+2")
        else:
            target = Path(working_dir) / "deskorb-desktop-adapter-fixture"
            target.mkdir(parents=True, exist_ok=True)
            result = runtime.run_desktop_adapter("explorer", path=str(target))
        passed = bool(result.get("ok"))
        return {
            "outcome": "passed" if passed else "failed",
            "failure_kind": None if passed else str(result.get("failure_kind") or "desktop_adapter_failed"),
            "completed": passed, "verified": bool(result.get("verified", passed)),
            "evidence_passed": bool(result.get("postcondition_passed", passed)),
            "safety_passed": True, "total_latency_ms": round((time.monotonic() - started) * 1000, 2),
            "tool_rounds": int(result.get("action_steps") or 0),
            "action_steps": int(result.get("action_steps") or 0),
            "action_sequence": ["launch", "desktop_observe", "desktop_input", "desktop_verify"],
            "environment_class": "current_desktop",
            "postcondition_kind": scenario.get("postcondition_kind"),
            "recovery_count": 0, "stage_transition_count": 0,
            "cache_status": "not_applicable",
            "trace_hash": _safe_trace_hash({"browser_trace": []}),
        }
    finally:
        if runtime.mcp:
            runtime.mcp.close()


def _real_cross_domain_case(scenario: Mapping[str, Any], *, allow_current_desktop: bool) -> dict[str, Any]:
    """Keep the missing browser-to-local handoff explicit and fail closed."""
    failure = ("current_desktop_not_authorized" if not allow_current_desktop
               else "cross_domain_browser_stage_unavailable")
    return {"outcome": "blocked", "failure_kind": failure,
            "environment_class": "current_desktop",
            "postcondition_kind": scenario.get("postcondition_kind"),
            "safety_passed": True, "evidence_passed": False}


def load_flagship_scenarios(path: str | Path = SCENARIO_MANIFEST) -> list[dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    scenarios = value.get("scenarios") if isinstance(value, dict) else None
    if not isinstance(scenarios, list) or len(scenarios) != 12:
        raise ValueError("flagship scenario manifest must contain exactly 12 scenarios")
    ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in scenarios:
        if not isinstance(item, dict):
            raise ValueError("flagship scenario must be an object")
        case_id = str(item.get("id") or "").strip()
        family = str(item.get("family") or "").strip()
        if not case_id or case_id in ids or family not in {"browser", "desktop", "cross_domain"}:
            raise ValueError("flagship scenario IDs and families must be unique and bounded")
        ids.add(case_id)
        result.append({"id": case_id, "family": family,
                       "fixture": str(item.get("fixture") or ""),
                       "postcondition_kind": str(item.get("postcondition_kind") or "none")})
    return result


def validate_stability_run(runs: list[dict[str, Any]], *, repetitions: int = 10) -> dict[str, Any]:
    scenarios = load_flagship_scenarios()
    ids = {item["id"] for item in scenarios}
    if len(runs) != len(ids) * max(1, int(repetitions)):
        raise ValueError("stability run does not cover 12 scenarios at requested repetitions")
    counts: dict[str, int] = {case_id: 0 for case_id in ids}
    for item in runs:
        if not isinstance(item, dict) or str(item.get("case_id") or "") not in ids:
            raise ValueError("stability run contains an unknown scenario")
        if _PRIVATE_KEYS.intersection({str(key).lower() for key in item}):
            raise ValueError("stability run contains private trace fields")
        counts[str(item["case_id"])] += 1
    missing = [case_id for case_id, count in counts.items() if count < max(1, int(repetitions))]
    if missing:
        raise ValueError("stability run is missing repetitions")
    return {"scenario_count": len(ids), "repetitions": max(1, int(repetitions)),
            "runs": len(runs), "summary": summarize_runs(runs)}


def run_flagship_matrix(run_case: Callable[[Mapping[str, Any], int], dict[str, Any]], *,
                        repetitions: int = 10) -> list[dict[str, Any]]:
    """Run a caller-supplied real AgentRuntime case function serially."""
    runs: list[dict[str, Any]] = []
    for scenario in load_flagship_scenarios():
        for attempt in range(1, max(1, int(repetitions)) + 1):
            result = dict(run_case(scenario, attempt))
            result["case_id"] = scenario["id"]
            result["attempt"] = attempt
            result.setdefault("environment_class", "unknown")
            result.setdefault("postcondition_kind", scenario["postcondition_kind"])
            runs.append(result)
    return runs


def _real_browser_case(scenario: Mapping[str, Any], attempt: int, *,
                       base_url: str, working_dir: str) -> dict[str, Any]:
    """Run one browser flagship through the existing real AgentRuntime probe."""
    from e2e_local_browser_probe import run_case as run_local_case
    scenario_id = str(scenario.get("id") or "")
    definition = _browser_definition(scenario_id)
    if definition is None:
        return {
            "outcome": "blocked", "failure_kind": "flagship_fixture_contract_unavailable",
            "environment_class": "local_fixture", "postcondition_kind": scenario.get("postcondition_kind"),
        }
    probe_case, case_definition = definition
    from agent_runtime import AgentRuntime
    from config import API_BASE_URL, API_MODEL, API_PROXY_URL, MODEL_PROVIDER
    events = Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL,
                           model_provider=MODEL_PROVIDER, working_dir=working_dir)
    try:
        raw = run_local_case(probe_case, base_url, runtime, events, timeout_seconds=180,
                             definition=case_definition)
        trace = raw.get("browser_trace") or []
        recovery_count = sum(
            1 for item in trace if isinstance(item, dict)
            and (item.get("model_fallback") or item.get("failure_kind") in {
                "browser_no_progress", "browser_unknown_ref", "browser_reobservation_required",
            })
        )
        stage_transition_count = _stage_transition_total(trace)
        no_progress = any(
            isinstance(item, dict) and item.get("failure_kind") == "browser_no_progress"
            for item in trace
        )
        outcome = "passed" if raw.get("ok") else (
            "blocked" if scenario_id == "flagship-browser-no-progress"
            or no_progress or raw.get("failure_kind") in {
                "browser_handoff_timeout", "human_handoff_not_resumed",
            } else "failed"
        )
        failure_kind = (
            "browser_no_progress_unresolved"
            if scenario_id == "flagship-browser-no-progress" and not raw.get("ok")
            else raw.get("failure_kind")
        )
        normalized = {
            "outcome": outcome,
            "failure_kind": failure_kind,
            "completed": bool(raw.get("terminal") == "completed"),
            "verified": bool(raw.get("verified")),
            "evidence_passed": bool(raw.get("evidence_passed")),
            "safety_passed": True,
            "total_latency_ms": raw.get("elapsed_ms"),
            "tool_rounds": raw.get("mcp_tool_calls", 0),
            "action_steps": sum(len(item.get("action_types") or []) for item in trace
                                if isinstance(item, dict)),
            "environment_class": "local_fixture",
            "postcondition_kind": scenario.get("postcondition_kind"),
            "recovery_count": recovery_count,
            "stage_transition_count": stage_transition_count,
            "cache_status": "hit" if any(item.get("execution_source") == "cache"
                                          for item in trace
                                          if isinstance(item, dict)) else "miss",
            "trace_hash": _safe_trace_hash(raw),
        }
        return normalized
    finally:
        if runtime.mcp:
            runtime.mcp.close()


def run_real_flagship_matrix(*, repetitions: int = 10,
                             allow_current_desktop: bool = False) -> list[dict[str, Any]]:
    """Execute the currently supported real browser flagship subset.

    The returned list is intentionally incomplete when a scenario has no real
    adapter; callers must validate coverage and will receive explicit blocked
    records for those scenarios.
    """
    from e2e_local_browser_probe import QuietHandler, ROOT as FIXTURE_ROOT
    from agent_runtime import AgentRuntime  # noqa: F401 - startup check
    with tempfile.TemporaryDirectory(prefix="deskorb-flagship-web-") as directory:
        handler = functools.partial(QuietHandler, directory=str(FIXTURE_ROOT))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            runs: list[dict[str, Any]] = []
            for scenario in load_flagship_scenarios():
                for attempt in range(1, max(1, int(repetitions)) + 1):
                    # A fresh working directory gives each repetition its own
                    # MCP profile, task state and trace output.  Reusing the
                    # HTTP fixture is safe because it is read-only loopback.
                    if scenario["id"] in _REAL_BROWSER_SCENARIOS:
                        with tempfile.TemporaryDirectory(prefix="deskorb-flagship-run-") as run_directory:
                            result = _real_browser_case(
                                scenario, attempt, base_url=base_url, working_dir=run_directory,
                            )
                    elif scenario["id"] == "flagship-desktop-notepad":
                        with tempfile.TemporaryDirectory(prefix="deskorb-flagship-desktop-") as run_directory:
                            result = _real_notepad_case(
                                scenario, attempt, working_dir=run_directory,
                                allow_current_desktop=allow_current_desktop,
                            )
                    elif scenario["id"] in _REAL_DESKTOP_SCENARIOS:
                        with tempfile.TemporaryDirectory(prefix="deskorb-flagship-desktop-") as run_directory:
                            result = _real_desktop_adapter_case(
                                scenario, attempt, working_dir=run_directory,
                                allow_current_desktop=allow_current_desktop,
                            )
                    elif scenario["id"] in _REAL_CROSS_DOMAIN_SCENARIOS:
                        result = _real_cross_domain_case(
                            scenario, allow_current_desktop=allow_current_desktop,
                        )
                    else:
                        result = {"outcome": "blocked", "failure_kind": "scenario_adapter_unavailable",
                                  "environment_class": "current_desktop" if scenario["family"] != "browser" else "local_fixture",
                                  "postcondition_kind": scenario["postcondition_kind"]}
                    result.setdefault("trace_hash", _safe_trace_hash(result))
                    result.update({"case_id": scenario["id"], "attempt": attempt})
                    runs.append(result)
            return runs
        finally:
            server.shutdown()
            server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a DeskOrb flagship stability report")
    parser.add_argument("report", type=Path)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--execute-real", action="store_true",
                        help="Execute supported flagship adapters through real local runtime.")
    parser.add_argument("--allow-current-desktop", action="store_true",
                        help="Authorize the disposable current-desktop Notepad adapter.")
    parser.add_argument("--output", type=Path,
                        help="Write the real-run normalized report before validation.")
    args = parser.parse_args(argv)
    if args.execute_real:
        runs = run_real_flagship_matrix(
            repetitions=args.repetitions,
            allow_current_desktop=bool(args.allow_current_desktop),
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps({"runs": runs}, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"ok": False, "runs": len(runs),
                          "blocked": sum(item.get("outcome") == "blocked" for item in runs)}, ensure_ascii=False))
        return 2
    runs = load_normalized_report(args.report)
    validate_stability_run(runs, repetitions=args.repetitions)
    print(json.dumps({"ok": True, "runs": len(runs)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
