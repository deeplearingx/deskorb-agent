"""Aggregate DeskOrb E2E traces into reproducible quality-gate metrics."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any, Mapping


_PRIVATE_TRACE_KEYS = {
    "answer", "prompt", "input", "messages", "content", "tool_calls", "tool_payloads",
    "arguments", "observations", "screenshot", "screenshots", "page_text", "final_answer",
    "url", "urls", "base_url", "error", "traceback", "api_key", "token", "cookie", "cookies",
}

_SAFE_ACTION_KINDS = {
    "launch", "browser_navigate", "browser_observe", "browser_extract", "browser_click",
    "browser_input", "browser_wait", "browser_verify", "desktop_observe", "desktop_input",
    "desktop_click", "desktop_hotkey", "desktop_scroll", "desktop_verify", "window_observe",
    "window_focus", "window_control", "filesystem_read", "filesystem_write", "filesystem_verify",
    "shell_verify", "provider_check", "confirmation", "handoff", "refusal", "other",
}

_ACTION_KIND_ALIASES = {
    "application_launch": "launch",
    "launch_application": "launch",
    "browser_action_batch": "browser_observe",
    "navigate": "browser_navigate",
    "snapshot": "browser_observe",
    "extract": "browser_extract",
    "click": "browser_click",
    "click_ref": "browser_click",
    "switch_tab": "browser_observe",
    "browser_tabs": "browser_observe",
    "fill": "browser_input",
    "fill_ref": "browser_input",
    "type": "desktop_input",
    "wait": "browser_wait",
    "verify": "browser_verify",
    "desktop_capture_state": "desktop_observe",
    "desktop_get_active_window": "desktop_observe",
    "desktop_uia_observe": "desktop_observe",
    "desktop_type": "desktop_input",
    "desktop_click": "desktop_click",
    "desktop_hotkey": "desktop_hotkey",
    "desktop_scroll": "desktop_scroll",
    "desktop_verify_state": "desktop_verify",
    "desktop_uia_invoke": "desktop_click",
    "desktop_uia_set_value": "desktop_input",
    "desktop_list_windows": "window_observe",
    "window_focus": "window_focus",
    "window_control": "window_control",
    "mcp_playwright_browser_navigate": "browser_navigate",
    "mcp_playwright_browser_snapshot": "browser_observe",
    "mcp_playwright_browser_click": "browser_click",
    "mcp_playwright_browser_type": "browser_input",
    "mcp_playwright_browser_wait_for": "browser_wait",
    "mcp_playwright_browser_tabs": "browser_observe",
    "filesystem_search_text": "filesystem_read",
    "filesystem_read_text": "filesystem_read",
    "filesystem_list": "filesystem_read",
    "filesystem_write": "filesystem_write",
    "filesystem_verify": "filesystem_verify",
    "shell_run": "shell_verify",
    "model_provider_check": "provider_check",
    "provider_check": "provider_check",
    "approval": "confirmation",
    "confirmation": "confirmation",
    "human_verification": "handoff",
    "handoff": "handoff",
    "refusal": "refusal",
}


def _private_trace_keys(value: Any) -> set[str]:
    """Find disallowed trace fields recursively, including nested payloads."""
    if isinstance(value, dict):
        found = {str(key).lower() for key in value if str(key).lower() in _PRIVATE_TRACE_KEYS}
        for child in value.values():
            found.update(_private_trace_keys(child))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for child in value:
            found.update(_private_trace_keys(child))
        return found
    return set()


def load_normalized_report(path: str | Path) -> list[dict[str, Any]]:
    """Load a metrics-only report and reject accidental content capture."""
    source = Path(path)
    if source.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("E2E report is larger than the 10 MB safety limit")
    value = json.loads(source.read_text(encoding="utf-8"))
    runs = value.get("runs") if isinstance(value, dict) else value
    if not isinstance(runs, list):
        raise ValueError("E2E report must be a list or an object with a runs list")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(runs):
        if not isinstance(item, dict):
            raise ValueError(f"run {index} is not an object")
        leaked = sorted(_private_trace_keys(item))
        if leaked:
            raise ValueError(f"run {index} contains private trace fields: {', '.join(leaked)}")
        case_id = str(item.get("case_id") or "").strip()
        outcome = str(item.get("outcome") or "").strip().lower()
        if not case_id or outcome not in {"passed", "partial", "failed", "blocked", "skipped"}:
            raise ValueError(f"run {index} must contain case_id and a normalized outcome")
        normalized.append(dict(item, case_id=case_id, outcome=outcome))
    return normalized


def load_step_baselines(path: str | Path, *, case_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Load and validate the content-free semantic-step contract.

    The baseline file is deliberately separate from the task prompts.  It may
    describe action kinds and numeric lower bounds, but it must never contain
    URLs, page text, prompts, credentials, or tool arguments.
    """
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"step baseline file does not exist: {source}")
    if source.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("step baseline file is larger than the 2 MB safety limit")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid step baseline file: {exc}") from exc
    cases = value.get("cases") if isinstance(value, dict) else None
    if not isinstance(cases, dict) or not cases:
        raise ValueError("step baseline file must contain a non-empty cases object")
    expected = {str(item) for item in case_ids} if case_ids is not None else set(cases)
    actual = {str(item) for item in cases}
    missing = sorted(expected - actual)
    extra = sorted(actual - expected) if case_ids is not None else []
    if missing:
        raise ValueError("missing step baselines: " + ", ".join(missing))
    if extra:
        raise ValueError("unexpected step baselines: " + ", ".join(extra))

    normalized: dict[str, dict[str, Any]] = {}
    for case_id, raw in cases.items():
        if not isinstance(raw, dict):
            raise ValueError(f"step baseline for {case_id} must be an object")
        try:
            minimum = int(raw["minimum_required_steps"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"step baseline for {case_id} needs an integer minimum_required_steps") from exc
        if minimum < 1:
            raise ValueError(f"step baseline for {case_id} must be at least one step")
        kinds = raw.get("required_action_kinds")
        if not isinstance(kinds, list) or not kinds or any(not isinstance(item, str) for item in kinds):
            raise ValueError(f"step baseline for {case_id} needs required_action_kinds")
        canonical = [_canonical_action_kind(item) for item in kinds]
        if any(item == "other" for item in canonical):
            raise ValueError(f"step baseline for {case_id} contains an unknown action kind")
        if len(set(canonical)) != len(canonical):
            raise ValueError(f"step baseline for {case_id} contains duplicate action kinds")
        normalized[str(case_id)] = {
            "minimum_required_steps": minimum,
            "required_action_kinds": canonical,
            "allow_handoff": bool(raw.get("allow_handoff", False)),
            "requires_evidence": bool(raw.get("requires_evidence", False)),
            "safety_case": bool(raw.get("safety_case", False)),
        }
    return normalized


def summarize_runs(runs: list[dict[str, Any]], *,
                   step_baselines: Mapping[str, Mapping[str, Any]] | None = None,
                   historical_runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Summarize normalized run records; no task prompts or tool payloads are retained."""
    scored = [item for item in runs if item.get("scorable", True)]
    total = len(scored)
    full = sum(item.get("outcome") == "passed" for item in scored)
    partial = sum(item.get("outcome") == "partial" for item in scored)
    safety = [item for item in scored if item.get("safety_case")
              and item.get("safety_scorable", True)]
    verified = [item for item in scored if item.get("requires_evidence")
                and item.get("evidence_scorable", True)]
    latencies = sorted(float(item["total_latency_ms"]) for item in scored if isinstance(item.get("total_latency_ms"), (int, float)))
    first = sorted(float(item["first_response_ms"]) for item in scored if isinstance(item.get("first_response_ms"), (int, float)))
    action_steps = sorted(float(value) for item in scored for value in [_action_steps(item)]
                          if value is not None and not (value == 0 and not _nonnegative_int(item.get("tool_rounds"))))
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in scored:
        by_case[str(item.get("case_id") or "unknown")].append(item)
    failure_categories = Counter(
        str(item.get("failure_category") or item.get("failure_kind") or "unknown")
        for item in scored
        if item.get("outcome") in {"failed", "blocked"}
    )
    action_sequences = Counter(
        " → ".join(sequence)
        for item in scored
        for sequence in [_safe_action_sequence(item)]
        if sequence
    )
    confirmations = sum(_nonnegative_int(item.get("confirmation_count", item.get("approval_count", 0))) for item in scored)
    handoffs = sum(_handoff_count(item) for item in scored)
    case_step_metrics = _case_step_metrics(by_case, step_baselines, historical_runs or [])
    pareto_points = [
        {"case_id": case_id, "success_rate": metrics["success_rate"],
         "action_steps": metrics["p50_action_steps"]}
        for case_id, metrics in case_step_metrics.items()
        if metrics.get("p50_action_steps") is not None
    ]
    return {
        "runs": total,
        "task_completion_rate": _ratio(full, total),
        "partial_completion_rate": _ratio(full + 0.5 * partial, total),
        "safety_pass_rate": _ratio(sum(bool(item.get("safety_passed")) for item in safety), len(safety)),
        "evidence_accuracy": _ratio(sum(bool(item.get("evidence_passed")) for item in verified), len(verified)),
        "p50_total_latency_ms": _percentile(latencies, 0.50),
        "p95_total_latency_ms": _percentile(latencies, 0.95),
        "p50_first_response_ms": _percentile(first, 0.50),
        "average_tool_rounds": _ratio(sum(float(item.get("tool_rounds", 0)) for item in scored), total),
        "average_action_steps": _ratio(sum(action_steps), len(action_steps)),
        "p50_action_steps": _percentile(action_steps, 0.50),
        "p95_action_steps": _percentile(action_steps, 0.95),
        "confirmation_count": confirmations,
        "handoff_count": handoffs,
        "blocked_runs": sum(item.get("outcome") == "blocked" for item in scored),
        # Keep environment blocks visible without allowing them to pollute
        # completion, safety, or evidence denominators when scorable=False.
        "blocked_runs_total": sum(item.get("outcome") == "blocked" for item in runs),
        "failure_categories": dict(sorted(failure_categories.items())),
        "safe_action_sequence_counts": dict(sorted(action_sequences.items())),
        "single_task_confirmation_coverage": _ratio(
            sum(bool(item.get("task_confirmation_once")) for item in scored
                if item.get("needs_task_confirmation") and item.get("confirmation_scorable", True)),
            sum(bool(item.get("needs_task_confirmation")) for item in scored
                if item.get("confirmation_scorable", True))),
        "captcha_handoff_success_rate": _ratio(sum(bool(item.get("handoff_passed")) for item in scored if item.get("captcha_case")),
                                               sum(bool(item.get("captcha_case")) for item in scored)),
        "case_passes": {case_id: sum(item.get("outcome") == "passed" for item in case_runs)
                        for case_id, case_runs in by_case.items()},
        "case_runs": {case_id: len(case_runs) for case_id, case_runs in by_case.items()},
        "case_step_metrics": case_step_metrics,
        "pareto_frontier": compute_pareto_frontier(pareto_points),
    }


def compute_pareto_frontier(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return non-dominated success-rate/step-count points.

    Higher success rate and fewer semantic steps are preferred.  A point is
    dominated only when another point is at least as successful and no slower,
    with one of those dimensions strictly better.
    """
    valid: list[dict[str, Any]] = []
    for point in points:
        case_id = str(point.get("case_id") or "").strip()
        try:
            success_rate = float(point.get("success_rate"))
            steps = float(point.get("action_steps"))
        except (TypeError, ValueError):
            continue
        if not case_id or success_rate < 0 or success_rate > 1 or steps < 0:
            continue
        valid.append({"case_id": case_id, "success_rate": round(success_rate, 4),
                      "action_steps": _number_or_int(steps)})
    frontier = []
    for candidate in valid:
        dominated = any(
            other["success_rate"] >= candidate["success_rate"]
            and other["action_steps"] <= candidate["action_steps"]
            and (other["success_rate"] > candidate["success_rate"]
                 or other["action_steps"] < candidate["action_steps"])
            for other in valid if other is not candidate
        )
        if not dominated:
            frontier.append(candidate)
    return sorted(frontier, key=lambda item: (item["action_steps"], -item["success_rate"], item["case_id"]))


def compare_with_baseline(current: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compare quality metrics against a previous run using fixed thresholds."""
    if not previous:
        return {"available": False, "passed": True, "regressions": [], "deltas": {}}
    regressions: list[str] = []
    deltas: dict[str, float] = {}

    def delta(metric: str) -> tuple[float | None, float | None]:
        old = _number(previous.get(metric))
        new = _number(current.get(metric))
        if old is not None and new is not None:
            deltas[metric] = round(new - old, 4)
        return new, old

    new, old = delta("task_completion_rate")
    if new is not None and old is not None and new < old - 0.05:
        regressions.append("task_completion_rate_drop_gt_5pp")
    for metric, name in (("safety_pass_rate", "safety_pass_rate_declined"),
                         ("evidence_accuracy", "evidence_accuracy_declined")):
        new, old = delta(metric)
        if new is not None and old is not None and new < old:
            regressions.append(name)
    new, old = delta("p95_total_latency_ms")
    if new is not None and old is not None and old > 0 and new > old * 1.25:
        regressions.append("p95_total_latency_increased_gt_25pct")
    new, old = delta("p50_action_steps")
    if new is not None and old is not None and old > 0 and new > old * 1.20:
        regressions.append("p50_action_steps_increased_gt_20pct")
    return {"available": True, "passed": not regressions, "regressions": regressions, "deltas": deltas}


def evaluate_quality_gates(summary: dict[str, Any], gates: dict[str, Any],
                           baseline_comparison: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate release gates without hiding any failed dimension."""
    failures: list[str] = []
    for metric, minimum in (gates.get("minimum_rates") or {}).items():
        actual = summary.get(metric)
        if actual is None:
            failures.append(f"{metric} is missing")
        elif float(actual) < float(minimum):
            failures.append(f"{metric} below {minimum}")
    for case_id, needed in (gates.get("minimum_passes_per_case") or {}).items():
        if int(summary.get("case_passes", {}).get(case_id, 0)) < int(needed):
            failures.append(f"{case_id} has too few successful repetitions")
    for metric, maximum in (gates.get("maximum_values") or {}).items():
        actual = summary.get(metric)
        if actual is not None and float(actual) > float(maximum):
            failures.append(f"{metric} above {maximum}")
    if baseline_comparison and not bool(baseline_comparison.get("passed", True)):
        failures.extend("regression: " + str(item) for item in baseline_comparison.get("regressions") or ())
    return {"passed": not failures, "failures": failures}


def evaluate_matrix_coverage(summary: dict[str, Any], case_ids: list[str], repetitions: int = 3) -> dict[str, Any]:
    """Require every matrix case to have the requested number of repetitions."""
    needed = max(1, int(repetitions))
    observed = summary.get("case_runs") if isinstance(summary.get("case_runs"), dict) else {}
    missing = [case_id for case_id in case_ids if int(observed.get(case_id, 0)) < needed]
    return {"passed": not missing, "required_repetitions": needed,
            "cases": len(case_ids), "covered_cases": len(case_ids) - len(missing),
            "missing_cases": missing}


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    index = round((len(values) - 1) * quantile)
    return values[index]


def _canonical_action_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in _ACTION_KIND_ALIASES:
        return _ACTION_KIND_ALIASES[text]
    # AgentRuntime emits a short human-facing label for a few local tools.
    # Keep this mapping structural; never preserve the label itself in a
    # report because a future label could contain user-provided text.
    if "application" in text and any(marker in text for marker in ("launch", "open")):
        return "launch"
    if text in {"active window", "desktop observation", "observe desktop", "list windows"}:
        return "desktop_observe"
    if text in {"list files", "read file", "search files"}:
        return "filesystem_read"
    if text == "mcp browser tool":
        return "browser_observe"
    return text if text in _SAFE_ACTION_KINDS else "other"


def _safe_action_sequence(item: Mapping[str, Any]) -> list[str]:
    raw = item.get("action_sequence")
    if raw is None:
        raw = item.get("safe_action_sequence")
    if not isinstance(raw, list):
        return []
    return [_canonical_action_kind(value) for value in raw if _canonical_action_kind(value) != "other"]


def _action_steps(item: Mapping[str, Any]) -> float | None:
    raw = item.get("action_steps", item.get("semantic_action_steps"))
    if raw is None:
        sequence = _safe_action_sequence(item)
        return float(len(sequence)) if sequence else None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _case_step_metrics(by_case: Mapping[str, list[dict[str, Any]]],
                       baselines: Mapping[str, Mapping[str, Any]] | None,
                       historical_runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    history: dict[str, list[float]] = defaultdict(list)
    for item in historical_runs:
        if item.get("outcome") != "passed":
            continue
        steps = _action_steps(item)
        if steps is not None:
            history[str(item.get("case_id") or "unknown")].append(steps)
    result: dict[str, dict[str, Any]] = {}
    for case_id, case_runs in sorted(by_case.items()):
        values = sorted(value for item in case_runs for value in [_action_steps(item)]
                        if value is not None and not (value == 0 and not _nonnegative_int(item.get("tool_rounds"))))
        passed = sum(item.get("outcome") == "passed" for item in case_runs)
        minimum = _number((baselines or {}).get(case_id, {}).get("minimum_required_steps")) if baselines else None
        p50 = _percentile(values, 0.50)
        redundancy = None
        if minimum is not None and minimum > 0 and p50 is not None and p50 >= minimum:
            redundancy = round((p50 - minimum) / minimum, 4)
        result[case_id] = {
            "success_rate": _ratio(passed, len(case_runs)) or 0.0,
            "successful_runs": passed,
            "minimum_required_steps": _number_or_int(minimum) if minimum is not None else None,
            "historical_min_successful_steps": (_number_or_int(min(history[case_id]))
                                                 if history.get(case_id) else None),
            "p50_action_steps": _number_or_int(p50) if p50 is not None else None,
            "p95_action_steps": (_number_or_int(_percentile(values, 0.95)) if values else None),
            "step_redundancy_ratio": redundancy,
        }
    return result


def _handoff_count(item: Mapping[str, Any]) -> int:
    value = item.get("handoff_count")
    if value is None:
        value = int(bool(item.get("handoff_present"))) + int(bool(item.get("handoff_used")))
    return _nonnegative_int(value)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _number_or_int(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(float(value), 4)
