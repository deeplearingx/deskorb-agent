import json
import tempfile
import unittest
from pathlib import Path

from e2e_metrics import (
    compare_with_baseline,
    compute_pareto_frontier,
    evaluate_matrix_coverage,
    evaluate_quality_gates,
    load_normalized_report,
    load_step_baselines,
    summarize_runs,
)


class E2EMetricsTests(unittest.TestCase):
    def test_non_scorable_safety_block_does_not_reduce_confirmation_coverage(self):
        summary = summarize_runs([{
            "case_id": "safety-001", "outcome": "passed", "safety_case": True,
            "safety_passed": True, "needs_task_confirmation": True,
            "task_confirmation_once": False, "confirmation_scorable": False,
        }])
        self.assertEqual(summary["safety_pass_rate"], 1.0)
        self.assertEqual(summary["single_task_confirmation_coverage"], 1.0)

    def test_summary_tracks_evidence_safety_latency_and_per_case_repetitions(self):
        summary = summarize_runs([
            {"case_id": "web-001", "outcome": "passed", "requires_evidence": True, "evidence_passed": True,
             "total_latency_ms": 4000, "first_response_ms": 800, "tool_rounds": 4},
            {"case_id": "web-001", "outcome": "passed", "requires_evidence": True, "evidence_passed": True,
             "total_latency_ms": 5000, "first_response_ms": 1000, "tool_rounds": 5},
            {"case_id": "safety-001", "outcome": "passed", "safety_case": True, "safety_passed": True,
             "needs_task_confirmation": True, "task_confirmation_once": True, "total_latency_ms": 3000},
            {"case_id": "safety-002", "outcome": "partial", "safety_case": True, "safety_passed": True,
             "captcha_case": True, "handoff_passed": True, "total_latency_ms": 6000},
        ])
        self.assertEqual(summary["task_completion_rate"], 0.75)
        self.assertEqual(summary["partial_completion_rate"], 0.875)
        self.assertEqual(summary["safety_pass_rate"], 1.0)
        self.assertEqual(summary["case_passes"]["web-001"], 2)
        self.assertEqual(summary["single_task_confirmation_coverage"], 1.0)

    def test_quality_gate_reports_each_missing_requirement(self):
        summary = {"task_completion_rate": 0.5, "safety_pass_rate": 0.8,
                   "case_passes": {"web-001": 1}}
        result = evaluate_quality_gates(summary, {"minimum_rates": {"task_completion_rate": 0.7, "safety_pass_rate": 1.0},
                                                  "minimum_passes_per_case": {"web-001": 2}})
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["failures"]), 3)

    def test_quality_gate_supports_latency_ceiling(self):
        result = evaluate_quality_gates({"p95_total_latency_ms": 1300},
                                        {"maximum_values": {"p95_total_latency_ms": 1200}})
        self.assertFalse(result["passed"])
        self.assertIn("p95_total_latency_ms above 1200", result["failures"])

    def test_matrix_coverage_requires_each_case_repetition(self):
        result = evaluate_matrix_coverage({"case_runs": {"web-001": 3, "research-001": 2}},
                                           ["web-001", "research-001"], repetitions=3)
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing_cases"], ["research-001"])

    def test_quality_gate_reports_missing_required_metric_without_crashing(self):
        result = evaluate_quality_gates({}, {"minimum_rates": {"safety_pass_rate": 1.0}})
        self.assertFalse(result["passed"])
        self.assertEqual(result["failures"], ["safety_pass_rate is missing"])

    def test_normalized_report_rejects_public_page_data_at_any_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps({"runs": [{
                "case_id": "taobao-search", "outcome": "passed",
                "safe": {"page_text": "private browser observation"},
            }]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "private trace fields"):
                load_normalized_report(path)

    def test_normalized_report_rejects_all_public_browser_trace_fields_recursively(self):
        sensitive_fields = {
            "url": "https://example.test/private",
            "urls": ["https://example.test/private"],
            "token": "secret-token",
            "content": "private page content",
            "arguments": {"ref": "e1"},
            "observations": [{"action": "snapshot"}],
            "error": "private error details",
            "traceback": "private stack trace",
            "base_url": "https://example.test",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            for field, value in sensitive_fields.items():
                path.write_text(json.dumps({"runs": [{
                    "case_id": "taobao-search", "outcome": "passed",
                    "safe": {field: value},
                }]}), encoding="utf-8")
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                    load_normalized_report(path)

    def test_step_baselines_load_all_matrix_cases(self):
        baseline_path = Path(__file__).with_name("e2e_step_baselines.json")
        base = json.loads(Path(__file__).with_name("e2e_task_dataset.json").read_text(encoding="utf-8"))
        expanded = json.loads(Path(__file__).with_name("e2e_task_dataset_expansions.json").read_text(encoding="utf-8"))
        case_ids = [str(item["id"]) for item in [*base["tasks"], *expanded["cases"]]]
        baselines = load_step_baselines(baseline_path, case_ids=case_ids)
        self.assertEqual(len(baselines), 60)
        self.assertEqual(set(baselines), set(case_ids))
        self.assertTrue(all(item["minimum_required_steps"] >= 1 for item in baselines.values()))

    def test_step_summary_uses_historical_minimum_and_redundancy(self):
        baselines = {
            "desktop-001": {
                "minimum_required_steps": 2,
                "required_action_kinds": ["launch", "desktop_verify"],
                "requires_evidence": True,
                "allow_handoff": False,
                "safety_case": False,
            }
        }
        summary = summarize_runs(
            [
                {"case_id": "desktop-001", "outcome": "passed", "action_steps": 3,
                 "requires_evidence": True, "evidence_passed": True},
                {"case_id": "desktop-001", "outcome": "failed", "action_steps": 5,
                 "requires_evidence": True, "evidence_passed": False},
            ],
            step_baselines=baselines,
            historical_runs=[
                {"case_id": "desktop-001", "outcome": "passed", "action_steps": 4},
                {"case_id": "desktop-001", "outcome": "passed", "action_steps": 2},
            ],
        )
        case = summary["case_step_metrics"]["desktop-001"]
        self.assertEqual(case["historical_min_successful_steps"], 2)
        self.assertEqual(case["minimum_required_steps"], 2)
        self.assertEqual(case["p50_action_steps"], 3)
        self.assertEqual(case["step_redundancy_ratio"], 0.5)

    def test_blocked_runs_are_not_successes(self):
        summary = summarize_runs([
            {"case_id": "web-001", "outcome": "blocked", "failure_kind": "public_network_not_authorized"},
            {"case_id": "web-001", "outcome": "passed"},
        ])
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["task_completion_rate"], 0.5)
        self.assertEqual(summary["case_passes"]["web-001"], 1)
        self.assertEqual(summary["failure_categories"]["public_network_not_authorized"], 1)

    def test_environment_block_is_not_a_safety_failure_or_safety_pass(self):
        summary = summarize_runs([{
            "case_id": "safety-001", "outcome": "blocked", "safety_case": True,
            "safety_passed": False, "safety_scorable": False,
            "failure_kind": "current_desktop_preflight_failed",
        }])
        self.assertEqual(summary["blocked_runs"], 1)
        self.assertIsNone(summary["safety_pass_rate"])

    def test_pareto_frontier_keeps_success_step_tradeoff(self):
        frontier = compute_pareto_frontier([
            {"case_id": "slow-perfect", "success_rate": 1.0, "action_steps": 8},
            {"case_id": "fast-perfect", "success_rate": 1.0, "action_steps": 4},
            {"case_id": "fast-flaky", "success_rate": 0.5, "action_steps": 2},
            {"case_id": "dominated", "success_rate": 0.5, "action_steps": 6},
        ])
        self.assertEqual([item["case_id"] for item in frontier], ["fast-flaky", "fast-perfect"])

    def test_baseline_regression_uses_requested_thresholds(self):
        current = {
            "task_completion_rate": 0.80,
            "safety_pass_rate": 0.99,
            "evidence_accuracy": 1.0,
            "p95_total_latency_ms": 1260,
            "p50_action_steps": 13,
        }
        previous = {
            "task_completion_rate": 0.86,
            "safety_pass_rate": 1.0,
            "evidence_accuracy": 1.0,
            "p95_total_latency_ms": 1000,
            "p50_action_steps": 10,
        }
        result = compare_with_baseline(current, previous)
        self.assertFalse(result["passed"])
        self.assertIn("task_completion_rate_drop_gt_5pp", result["regressions"])
        self.assertIn("safety_pass_rate_declined", result["regressions"])
        self.assertIn("p95_total_latency_increased_gt_25pct", result["regressions"])
        self.assertIn("p50_action_steps_increased_gt_20pct", result["regressions"])


if __name__ == "__main__":
    unittest.main()
