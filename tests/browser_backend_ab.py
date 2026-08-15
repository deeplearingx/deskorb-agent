"""A/B runner for the Playwright and Browser Use semantic browser backends.

Only the user-specified cases #3 and #6 are included here.  The runner uses
the same real-model ``AgentRuntime`` and the same public-site acceptance
validator for both backends; it does not compare a raw Browser Use agent with
DeskOrb's semantic runtime.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import tempfile
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import complex_browser_acceptance as base
from browser_task_spec import BrowserTaskSpec
import extended_browser_acceptance as extended


CASE_IDS = ("case-03-conditional-4399", "case-06-wikipedia-back")
BACKENDS = ("playwright", "browser_use")
_SEARCH_HOSTS = {
    "google.com", "www.google.com", "bing.com", "www.bing.com", "cn.bing.com",
    "baidu.com", "www.baidu.com", "duckduckgo.com", "www.duckduckgo.com",
    "search.brave.com",
}


def _records(run: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = run.get("evidence_ledger")
    raw = ledger.get("records") if isinstance(ledger, dict) else []
    return [item for item in raw if isinstance(item, dict)]


def _fields(record: dict[str, Any]) -> dict[str, str]:
    raw = record.get("fields")
    return {
        str(key).casefold().replace("-", "_").replace(" ", "_"): str(value or "")
        for key, value in raw.items()
    } if isinstance(raw, dict) else {}


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return str(urlsplit(str(url or "")).hostname or "").casefold().rstrip(".")


def evidence_accuracy(case_id: str, run: dict[str, Any]) -> dict[str, Any]:
    """Score only deterministic fields in the runtime-owned evidence ledger."""
    records = _records(run)
    if case_id == "case-03-conditional-4399":
        prompt = str(extended.EXTENDED_SCENARIOS[case_id].get("prompt") or "")
        terms = tuple(str(item).casefold() for item in BrowserTaskSpec.from_goal(prompt).target_terms)
        matched = 0
        for record in records:
            fields = _fields(record)
            rendered = " ".join(fields.values()).casefold()
            source_url = str(record.get("source_url") or fields.get("url") or "")
            if (_host(source_url) not in _SEARCH_HOSTS
                    and fields.get("title") and fields.get("url")
                    and terms and any(term in rendered for term in terms)):
                matched = 1
                break
        return {"matched": matched, "expected": 1, "accuracy": float(matched),
                "exact": matched == 1, "terms": list(terms)}

    expected_slugs = {
        "artificial_intelligence": "/wiki/artificial_intelligence",
        "machine_learning": "/wiki/machine_learning",
        "deep_learning": "/wiki/deep_learning",
        "transformer": "/wiki/transformer",
    }
    matched_keys: set[str] = set()
    for record in records:
        fields = _fields(record)
        source_url = str(record.get("source_url") or fields.get("url") or "").casefold()
        if _host(source_url) not in {"wikipedia.org", "www.wikipedia.org", "en.wikipedia.org"}:
            continue
        if not all(fields.get(field) for field in ("title", "url", "excerpt")):
            continue
        for key, slug in expected_slugs.items():
            if slug in source_url:
                matched_keys.add(key)
    matched = len(matched_keys)
    return {"matched": matched, "expected": len(expected_slugs),
            "accuracy": matched / len(expected_slugs), "exact": matched == len(expected_slugs),
            "matched_keys": sorted(matched_keys)}


def _is_timeout(run: dict[str, Any]) -> bool:
    failure = str(run.get("failure_kind") or "").casefold()
    return any(marker in failure for marker in (
        "timeout", "timed_out", "provider_timeout", "tool_execution_timeout",
    ))


def _valid_runs(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed_statuses = {"completed_verified", "failed_product", "blocked_external", "waiting_human"}
    unclassified = {"typeerror", "valueerror", "keyerror", "attributeerror", "indexerror", "nameerror"}
    return [
        run for run in runs
        if str(run.get("status") or "") in allowed_statuses
        and bool(run.get("real_site"))
        and str(run.get("failure_kind") or "").casefold() not in unclassified
    ]


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    valid = _valid_runs(runs)
    completed = [run for run in valid if run.get("status") == "completed_verified"]
    timeout_count = sum(_is_timeout(run) for run in valid)
    action_values = [int(run.get("action_steps") or 0) for run in valid]
    evidence_values = [float(run.get("evidence_accuracy", 0.0)) for run in valid]
    exact_values = [bool(run.get("evidence_exact")) for run in valid]
    return {
        "runs": len(runs),
        "valid_runs": len(valid),
        "invalid_environment": sum(run.get("status") == "invalid_environment" for run in runs),
        "comparison_excluded": len(runs) - len(valid),
        "completed_verified": len(completed),
        "completion_rate": len(completed) / len(valid) if valid else None,
        "action_count_average": statistics.mean(action_values) if action_values else None,
        "action_count_median": statistics.median(action_values) if action_values else None,
        "timeout_count": timeout_count,
        "timeout_rate": timeout_count / len(valid) if valid else None,
        "evidence_accuracy_average": statistics.mean(evidence_values) if evidence_values else None,
        "evidence_exact_rate": statistics.mean(exact_values) if exact_values else None,
        "safety_violations": sum(
            int(run.get("state_violations") or 0) + int(run.get("forbidden_actions") or 0)
            for run in valid
        ),
        "external_blocks": sum(run.get("status") == "blocked_external" for run in valid),
    }


def _not_worse(value: float | None, baseline: float | None, *, lower_is_better: bool = False) -> bool:
    if value is None or baseline is None:
        return False
    return value <= baseline if lower_is_better else value >= baseline


def evaluate_ab(runs: list[dict[str, Any]], *, repetitions: int) -> dict[str, Any]:
    by_backend_case: dict[str, dict[str, Any]] = {}
    for backend in BACKENDS:
        for case_id in CASE_IDS:
            key = f"{backend}:{case_id}"
            items = [run for run in runs
                     if run.get("browser_backend") == backend and run.get("case_id") == case_id]
            by_backend_case[key] = _aggregate(items)

    backend_summary = {
        backend: _aggregate([run for run in runs if run.get("browser_backend") == backend])
        for backend in BACKENDS
    }
    playwright = backend_summary["playwright"]
    browser_use = backend_summary["browser_use"]
    comparable = bool(playwright["valid_runs"] and browser_use["valid_runs"])
    no_safety_regression = comparable and browser_use["safety_violations"] == 0
    no_completion_regression = comparable and _not_worse(
        browser_use["completion_rate"], playwright["completion_rate"]
    )
    no_timeout_regression = comparable and _not_worse(
        browser_use["timeout_rate"], playwright["timeout_rate"], lower_is_better=True
    )
    no_evidence_regression = comparable and _not_worse(
        browser_use["evidence_accuracy_average"], playwright["evidence_accuracy_average"]
    )
    strict_improvement = comparable and any((
        browser_use["completion_rate"] is not None
        and playwright["completion_rate"] is not None
        and browser_use["completion_rate"] > playwright["completion_rate"],
        browser_use["timeout_rate"] is not None
        and playwright["timeout_rate"] is not None
        and browser_use["timeout_rate"] < playwright["timeout_rate"],
        browser_use["evidence_accuracy_average"] is not None
        and playwright["evidence_accuracy_average"] is not None
        and browser_use["evidence_accuracy_average"] > playwright["evidence_accuracy_average"],
        browser_use["action_count_average"] is not None
        and playwright["action_count_average"] is not None
        and browser_use["action_count_average"] < playwright["action_count_average"],
    ))
    adopt = bool(no_safety_regression and no_completion_regression and no_timeout_regression
                 and no_evidence_regression and strict_improvement)
    decision = "browser_use_candidate" if adopt else "keep_playwright"
    if not comparable:
        decision = "keep_playwright_insufficient_valid_runs"
    return {
        "schema_version": "browser-backend-ab.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": list(CASE_IDS),
        "backends": list(BACKENDS),
        "repetitions": int(repetitions),
        "requested_runs": len(CASE_IDS) * len(BACKENDS) * int(repetitions),
        "received_runs": len(runs),
        "backend_summary": backend_summary,
        "backend_case_summary": by_backend_case,
        "decision": decision,
        "decision_checks": {
            "comparable": comparable,
            "no_safety_regression": no_safety_regression,
            "no_completion_regression": no_completion_regression,
            "no_timeout_regression": no_timeout_regression,
            "no_evidence_regression": no_evidence_regression,
            "strict_improvement": strict_improvement,
        },
        "default_backend_changed": False,
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Browser Backend A/B (#3 and #6)", "",
        f"- Decision: `{report.get('decision')}`",
        f"- Runs: `{report.get('received_runs', 0)}/{report.get('requested_runs', 0)}`",
        "",
        "| Backend | Valid | Completed | Completion | Avg actions | Timeout | Evidence accuracy | Exact evidence |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for backend, item in (report.get("backend_summary") or {}).items():
        def pct(value: Any) -> str:
            return "n/a" if value is None else f"{float(value) * 100:.1f}%"
        lines.append(
            f"| {backend} | {item.get('valid_runs', 0)} | {item.get('completed_verified', 0)} | "
            f"{pct(item.get('completion_rate'))} | {item.get('action_count_average', 'n/a')} | "
            f"{pct(item.get('timeout_rate'))} | {pct(item.get('evidence_accuracy_average'))} | "
            f"{pct(item.get('evidence_exact_rate'))} |"
        )
    lines.extend(["", "## Per-case", "", "| Backend / case | Valid | Completed | Evidence accuracy |", "|---|---:|---:|---:|"])
    for key, item in (report.get("backend_case_summary") or {}).items():
        accuracy = item.get("evidence_accuracy_average")
        rendered = "n/a" if accuracy is None else f"{float(accuracy) * 100:.1f}%"
        lines.append(f"| {key} | {item.get('valid_runs', 0)} | {item.get('completed_verified', 0)} | {rendered} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B test Playwright and Browser Use on real cases #3 and #6.")
    parser.add_argument("--live", action="store_true", help="Acknowledge real model and public-network use.")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("artifacts/browser-backend-ab.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("artifacts/browser-backend-ab.md"))
    args = parser.parse_args(argv)
    if not args.live:
        print(json.dumps({"ok": False, "error_kind": "live_opt_in_required"}, ensure_ascii=False))
        return 2
    repetitions = max(1, min(3, int(args.repetitions)))
    extended.prepare_catalog()
    runs: list[dict[str, Any]] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the default runner serial: each run gets a fresh MCP/profile, but
    # the acceptance requires a visible browser and the two backends should
    # not compete for foreground focus on Windows.
    with tempfile.TemporaryDirectory(prefix="deskorb-browser-backend-ab-") as directory:
        root = Path(directory)
        for backend in BACKENDS:
            for case_id in CASE_IDS:
                for attempt in range(1, repetitions + 1):
                    run = base.run_case(
                        case_id, attempt,
                        working_dir=root / f"{backend}-{case_id}-{attempt}",
                        browser_backend=backend,
                    )
                    run = extended._apply_custom_validation(run)
                    accuracy = evidence_accuracy(case_id, run)
                    run["evidence_accuracy"] = accuracy["accuracy"]
                    run["evidence_exact"] = accuracy["exact"]
                    run["evidence_accuracy_detail"] = accuracy
                    runs.append(run)
                    report = evaluate_ab(runs, repetitions=repetitions)
                    args.output.write_text(
                        json.dumps({**report, "partial": True, "runs": runs},
                                   ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    args.markdown_output.write_text(_markdown(report), encoding="utf-8")
    report = evaluate_ab(runs, repetitions=repetitions)
    args.output.write_text(
        json.dumps({**report, "partial": False, "runs": runs},
                   ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    args.markdown_output.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["decision"] == "browser_use_candidate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
