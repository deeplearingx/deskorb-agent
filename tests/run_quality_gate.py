"""Run the privacy-safe E2E quality gate against a normalized metrics report.

The input contains only case IDs, outcomes, booleans and timing counters.  The
runner refuses prompts, screenshots, page text, tool payloads and credentials.
It is suitable for CI and for a local report exported by the evaluator.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e_metrics import (compare_with_baseline, evaluate_matrix_coverage,
                         evaluate_quality_gates, load_normalized_report,
                         load_step_baselines, summarize_runs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate DeskOrb normalized E2E metrics")
    parser.add_argument("--report", required=True, help="JSON metrics report, not a raw trace")
    parser.add_argument("--dataset", default=str(Path(__file__).with_name("e2e_task_dataset.json")))
    parser.add_argument("--require-full-matrix", action="store_true",
                        help="require every repeatable matrix case to have --repetitions runs")
    parser.add_argument("--include-live", action="store_true",
                        help="also require live acceptance cases; these need explicit real-environment opt-in")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--baseline", help="previous normalized report used for regression comparison")
    parser.add_argument("--step-baseline", default=str(Path(__file__).with_name("e2e_step_baselines.json")),
                        help="semantic action lower-bound file")
    parser.add_argument("--validate-report-only", action="store_true",
                        help="validate normalized report shape and privacy only; do not apply dataset release gates")
    args = parser.parse_args(argv)
    if args.validate_report_only and args.require_full_matrix:
        print(json.dumps({"passed": False, "error": "--validate-report-only cannot require matrix coverage"}, ensure_ascii=False))
        return 2
    try:
        runs = load_normalized_report(args.report)
        step_baselines = None
        step_baseline_path = Path(args.step_baseline)
        if step_baseline_path.is_file():
            step_baselines = load_step_baselines(step_baseline_path)
        summary = summarize_runs(runs, step_baselines=step_baselines)
        baseline_summary = None
        if args.baseline:
            baseline_runs = load_normalized_report(args.baseline)
            baseline_summary = summarize_runs(baseline_runs, step_baselines=step_baselines)
        regression = compare_with_baseline(summary, baseline_summary)
        if args.validate_report_only:
            output = {
                "passed": True, "validation_only": True, "summary": summary,
                "failures": [], "coverage": None, "baseline_comparison": regression,
                "source": {"report": Path(args.report).name},
            }
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            return 0
        dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
        gate = evaluate_quality_gates(summary, dataset.get("quality_gates") or {}, regression)
        coverage = None
        if args.require_full_matrix:
            case_ids = [str(item.get("id")) for item in dataset.get("tasks") or []
                        if args.include_live or str(item.get("tier") or "repeatable") == "repeatable"]
            expansion_name = dataset.get("expansions_file")
            if expansion_name:
                expansion_path = Path(args.dataset).parent / Path(str(expansion_name)).name
                if expansion_path.is_file():
                    expansion = json.loads(expansion_path.read_text(encoding="utf-8"))
                    case_ids.extend(str(item.get("id")) for item in expansion.get("cases") or [])
            coverage = evaluate_matrix_coverage(summary, case_ids, args.repetitions)
            if not coverage["passed"]:
                gate = dict(gate, passed=False,
                            failures=[*gate["failures"],
                                      f"matrix coverage missing {len(coverage['missing_cases'])} case(s)"])
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)[:300]}, ensure_ascii=False))
        return 2
    output = {
        "passed": bool(gate["passed"]), "validation_only": False,
        "summary": summary, "failures": gate["failures"], "coverage": coverage,
        "baseline_comparison": regression,
        "source": {"report": Path(args.report).name, "dataset": Path(args.dataset).name},
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if gate["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
