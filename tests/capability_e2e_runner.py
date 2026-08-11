"""Run DeskOrb's independent document, public-fetch and UIA capability suite.

The suite deliberately keeps the existing 60-case matrix untouched.  It runs
safe local adapter checks by default; live public fetch and UIA canary checks
require explicit flags and report environment blocks rather than passing.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document_mcp import DocumentAdapter, DocumentError
from e2e_metrics import (compare_with_baseline, evaluate_quality_gates,
                         load_normalized_report, load_step_baselines, summarize_runs)
from mcp_security import URLPolicyError
from public_fetch_mcp import PublicFetchAdapter, PublicFetchError


ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(__file__).with_name("capability_task_dataset.json")
STEP_BASELINES = Path(__file__).with_name("capability_step_baselines.json")
PUBLIC_DOMAINS = ("fastapi.tiangolo.com",)
PUBLIC_URL = "https://fastapi.tiangolo.com/"
CAPABILITY_MINIMUMS = {"documents": 0.90, "public_fetch": 0.85, "uia_canary": 0.90}


def load_capability_cases() -> list[dict[str, Any]]:
    value = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = value.get("cases") if isinstance(value, dict) else None
    if not isinstance(cases, list) or len(cases) != 12:
        raise ValueError("capability suite must contain 12 cases")
    ids = [str(item.get("id") or "") for item in cases]
    if len(set(ids)) != 12 or any(not item for item in ids):
        raise ValueError("capability suite must contain unique case IDs")
    return [dict(item) for item in cases]


def _base(case: Mapping[str, Any], attempt: int, started: float, *, outcome: str,
          failure: str | None, sequence: list[str], safety: bool, evidence: bool,
          completed: bool, verified: bool) -> dict[str, Any]:
    return {
        "case_id": str(case["id"]), "attempt": attempt, "capability": str(case["capability"]),
        "outcome": outcome, "failure_category": failure,
        "total_latency_ms": round((time.monotonic() - started) * 1000, 2),
        "first_response_ms": None, "tool_rounds": len(sequence),
        "action_sequence": sequence, "action_steps": len(sequence),
        "safety_case": bool(case.get("safety_case")), "safety_passed": safety,
        "requires_evidence": bool(case.get("requires_evidence")), "evidence_passed": evidence,
        "completed": completed, "verified": verified,
        "confirmation_count": 0, "handoff_count": 0,
    }


def _blocked(case: Mapping[str, Any], attempt: int, started: float, reason: str) -> dict[str, Any]:
    result = _base(case, attempt, started, outcome="blocked", failure=reason, sequence=[],
                   safety=False, evidence=False, completed=False, verified=False)
    # Environment authorization/readiness is reported and counted separately,
    # but it must not be mistaken for an unsafe action or a bad evidence check.
    result["scorable"] = False
    return result


def _run_document(case: Mapping[str, Any], attempt: int, started: float) -> dict[str, Any]:
    operation = str(case["operation"])
    with tempfile.TemporaryDirectory(prefix="deskorb-capability-doc-") as directory:
        root = Path(directory) / "root"
        root.mkdir()
        adapter = DocumentAdapter((root,), markitdown_factory=lambda: None)
        source = root / "fixture.txt"
        source.write_text("DeskOrb document evidence", encoding="utf-8")
        if operation == "text":
            result = adapter.convert_file(source)
            passed = result.get("text") == "DeskOrb document evidence"
            return _base(case, attempt, started, outcome="passed" if passed else "failed",
                         failure=None if passed else "verification", sequence=["filesystem_read", "filesystem_verify"],
                         safety=True, evidence=passed, completed=passed, verified=passed)
        if operation == "outside_root":
            outside = Path(directory) / "outside.txt"
            outside.write_text("out of scope", encoding="utf-8")
            try:
                adapter.convert_file(outside)
            except DocumentError:
                return _base(case, attempt, started, outcome="passed", failure=None,
                             sequence=["refusal"], safety=True, evidence=True, completed=True, verified=True)
            return _base(case, attempt, started, outcome="failed", failure="safety",
                         sequence=["filesystem_read"], safety=False, evidence=False,
                         completed=False, verified=False)
        if operation == "binary_dependency":
            pdf = root / "fixture.pdf"
            pdf.write_bytes(b"%PDF fixture")
            try:
                adapter.convert_file(pdf)
            except DocumentError as exc:
                expected = "dependency" in str(exc)
                return _base(case, attempt, started, outcome="passed" if expected else "failed",
                             failure=None if expected else "tool", sequence=["filesystem_read", "refusal"],
                             safety=expected, evidence=expected, completed=expected, verified=expected)
        if operation == "unsupported_format":
            source.with_suffix(".exe").write_bytes(b"not a document")
            try:
                adapter.convert_file(source.with_suffix(".exe"))
            except DocumentError:
                return _base(case, attempt, started, outcome="passed", failure=None,
                             sequence=["refusal"], safety=True, evidence=True, completed=True, verified=True)
        return _base(case, attempt, started, outcome="failed", failure="tool", sequence=[],
                     safety=False, evidence=False, completed=False, verified=False)


def _run_fetch(case: Mapping[str, Any], attempt: int, started: float,
               allow_public: bool) -> dict[str, Any]:
    operation = str(case["operation"])
    adapter = PublicFetchAdapter(PUBLIC_DOMAINS)
    if operation == "fetch_live":
        if not allow_public:
            return _blocked(case, attempt, started, "public_network_not_authorized")
        try:
            result = adapter.fetch(PUBLIC_URL, max_chars=8000)
            passed = bool(result.get("text")) and result.get("host") == "fastapi.tiangolo.com"
            return _base(case, attempt, started, outcome="passed" if passed else "failed",
                         failure=None if passed else "verification",
                         sequence=["browser_navigate", "browser_observe", "browser_verify"],
                         safety=True, evidence=passed, completed=passed, verified=passed)
        except PublicFetchError:
            return _base(case, attempt, started, outcome="failed", failure="network",
                         sequence=["browser_navigate"], safety=True, evidence=False,
                         completed=False, verified=False)
    urls = {
        "reject_http": "http://fastapi.tiangolo.com/",
        "reject_private": "https://127.0.0.1/",
        "reject_domain": "https://evil.example/",
    }
    try:
        adapter.validate_url(urls[operation], resolve_dns=False)
    except (PublicFetchError, URLPolicyError):
        return _base(case, attempt, started, outcome="passed", failure=None,
                     sequence=["refusal"], safety=True, evidence=True, completed=True, verified=True)
    return _base(case, attempt, started, outcome="failed", failure="safety", sequence=[],
                 safety=False, evidence=False, completed=False, verified=False)


def _run_uia(case: Mapping[str, Any], attempt: int, started: float,
             allow_desktop: bool) -> dict[str, Any]:
    if not allow_desktop:
        return _blocked(case, attempt, started, "current_desktop_not_authorized")
    # The selected third-party UIA canary is never treated as installed merely
    # because a flag was passed. A future pinned release probe must provide a
    # real executable/session check before these cases can pass.
    return _blocked(case, attempt, started, "uia_canary_dependency_unavailable")


def run_case(case: Mapping[str, Any], attempt: int, *, allow_public: bool,
             allow_desktop: bool) -> dict[str, Any]:
    started = time.monotonic()
    capability = str(case.get("capability") or "")
    if capability == "documents":
        return _run_document(case, attempt, started)
    if capability == "public_fetch":
        return _run_fetch(case, attempt, started, allow_public)
    if capability == "uia_canary":
        return _run_uia(case, attempt, started, allow_desktop)
    return _base(case, attempt, started, outcome="failed", failure="tool", sequence=[],
                 safety=False, evidence=False, completed=False, verified=False)


def _capability_summaries(runs: list[dict[str, Any]], baselines: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for capability in sorted({str(item.get("capability") or "unknown") for item in runs}):
        selected = [item for item in runs if str(item.get("capability") or "unknown") == capability]
        result[capability] = summarize_runs(selected, step_baselines=baselines)
    return result


def _evaluate(summary: dict[str, Any], capabilities: Mapping[str, Mapping[str, Any]],
              baseline: Mapping[str, Any] | None) -> dict[str, Any]:
    failures: list[str] = []
    base_gate = evaluate_quality_gates(summary, {
        "minimum_rates": {"task_completion_rate": 0.85, "safety_pass_rate": 1.0,
                           "evidence_accuracy": 1.0},
    }, baseline)
    failures.extend(base_gate["failures"])
    for capability, minimum in CAPABILITY_MINIMUMS.items():
        item = capabilities.get(capability) or {}
        actual = item.get("task_completion_rate")
        if actual is None or float(actual) < minimum:
            failures.append(f"{capability} completion below {minimum}")
    return {"passed": not failures, "failures": failures}


def _markdown(summary: Mapping[str, Any], capabilities: Mapping[str, Mapping[str, Any]],
              gate: Mapping[str, Any], comparison: Mapping[str, Any]) -> str:
    lines = ["# DeskOrb capability E2E report", "", f"- Gate: {'passed' if gate.get('passed') else 'failed'}",
             f"- Runs: {summary.get('runs', 0)}", f"- Completion: {summary.get('task_completion_rate')}",
             f"- Safety: {summary.get('safety_pass_rate')}", f"- Evidence: {summary.get('evidence_accuracy')}", "",
             "## Capability summary", "", "| Capability | Completion | Runs | p95 latency ms |", "|---|---:|---:|---:|"]
    for name, item in capabilities.items():
        lines.append(f"| {name} | {item.get('task_completion_rate')} | {item.get('runs')} | {item.get('p95_total_latency_ms')} |")
    lines.extend(["", "## Failure categories", ""])
    for name, count in sorted((summary.get("failure_categories") or {}).items(), key=lambda pair: (-pair[1], pair[0])):
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## Step efficiency", "", f"- Pareto frontier points: {len(summary.get('pareto_frontier') or [])}",
                  f"- Average action steps: {summary.get('average_action_steps')}",
                  f"- Baseline comparison: {'available' if comparison.get('available') else 'not available'}"])
    if gate.get("failures"):
        lines.extend(["", "## Gate failures", "", *[f"- {item}" for item in gate["failures"]]])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run DeskOrb capability E2E evaluation")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--allow-public-network", action="store_true")
    parser.add_argument("--allow-current-desktop", action="store_true")
    parser.add_argument("--output", default="artifacts/capability-e2e.json")
    parser.add_argument("--markdown-output", default="artifacts/capability-e2e.md")
    parser.add_argument("--baseline")
    args = parser.parse_args(argv)
    cases = load_capability_cases()
    baselines = load_step_baselines(STEP_BASELINES, case_ids=[str(item["id"]) for item in cases])
    runs = [run_case(case, attempt, allow_public=args.allow_public_network,
                     allow_desktop=args.allow_current_desktop)
            for case in cases for attempt in range(1, max(1, min(20, args.repetitions)) + 1)]
    summary = summarize_runs(runs, step_baselines=baselines)
    capabilities = _capability_summaries(runs, baselines)
    baseline_summary = None
    if args.baseline:
        baseline_summary = summarize_runs(load_normalized_report(args.baseline), step_baselines=baselines)
    comparison = compare_with_baseline(summary, baseline_summary)
    gate = _evaluate(summary, capabilities, comparison)
    report = {"suite": "capability", "runs": runs, "summary": summary,
              "capabilities": capabilities, "quality_gate": gate,
              "baseline_comparison": comparison}
    output = Path(args.output)
    markdown = Path(args.markdown_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown.write_text(_markdown(summary, capabilities, gate, comparison), encoding="utf-8")
    print(json.dumps({"passed": gate["passed"], "summary": summary, "quality_gate": gate},
                     ensure_ascii=False, sort_keys=True))
    return 0 if gate["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
