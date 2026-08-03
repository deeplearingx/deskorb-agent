import unittest

from e2e_metrics import evaluate_matrix_coverage, evaluate_quality_gates, summarize_runs


class E2EMetricsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
