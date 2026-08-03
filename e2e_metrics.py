"""Aggregate DeskOrb E2E traces into reproducible quality-gate metrics."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from typing import Any


_PRIVATE_TRACE_KEYS = {
    "prompt", "input", "messages", "tool_calls", "tool_payloads", "screenshot",
    "screenshots", "page_text", "final_answer", "api_key", "cookie", "cookies",
}


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
        leaked = sorted(key for key in item if str(key).lower() in _PRIVATE_TRACE_KEYS)
        if leaked:
            raise ValueError(f"run {index} contains private trace fields: {', '.join(leaked)}")
        case_id = str(item.get("case_id") or "").strip()
        outcome = str(item.get("outcome") or "").strip().lower()
        if not case_id or outcome not in {"passed", "partial", "failed", "blocked", "skipped"}:
            raise ValueError(f"run {index} must contain case_id and a normalized outcome")
        normalized.append(dict(item, case_id=case_id, outcome=outcome))
    return normalized


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize normalized run records; no task prompts or tool payloads are retained."""
    scored = [item for item in runs if item.get("scorable", True)]
    total = len(scored)
    full = sum(item.get("outcome") == "passed" for item in scored)
    partial = sum(item.get("outcome") == "partial" for item in scored)
    safety = [item for item in scored if item.get("safety_case")]
    verified = [item for item in scored if item.get("requires_evidence")]
    latencies = sorted(float(item["total_latency_ms"]) for item in scored if isinstance(item.get("total_latency_ms"), (int, float)))
    first = sorted(float(item["first_response_ms"]) for item in scored if isinstance(item.get("first_response_ms"), (int, float)))
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in scored:
        by_case[str(item.get("case_id") or "unknown")].append(item)
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
        "single_task_confirmation_coverage": _ratio(sum(bool(item.get("task_confirmation_once")) for item in scored if item.get("needs_task_confirmation")),
                                                   sum(bool(item.get("needs_task_confirmation")) for item in scored)),
        "captcha_handoff_success_rate": _ratio(sum(bool(item.get("handoff_passed")) for item in scored if item.get("captcha_case")),
                                               sum(bool(item.get("captcha_case")) for item in scored)),
        "case_passes": {case_id: sum(item.get("outcome") == "passed" for item in case_runs)
                        for case_id, case_runs in by_case.items()},
        "case_runs": {case_id: len(case_runs) for case_id, case_runs in by_case.items()},
    }


def evaluate_quality_gates(summary: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    """Evaluate release gates without hiding any failed dimension."""
    failures: list[str] = []
    for metric, minimum in (gates.get("minimum_rates") or {}).items():
        if float(summary.get(metric, 0)) < float(minimum):
            failures.append(f"{metric} below {minimum}")
    for case_id, needed in (gates.get("minimum_passes_per_case") or {}).items():
        if int(summary.get("case_passes", {}).get(case_id, 0)) < int(needed):
            failures.append(f"{case_id} has too few successful repetitions")
    for metric, maximum in (gates.get("maximum_values") or {}).items():
        actual = summary.get(metric)
        if actual is not None and float(actual) > float(maximum):
            failures.append(f"{metric} above {maximum}")
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
