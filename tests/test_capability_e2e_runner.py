import json
import tempfile
import unittest
from pathlib import Path

import capability_e2e_runner as runner
from e2e_metrics import load_normalized_report, load_step_baselines


class CapabilityE2ERunnerTests(unittest.TestCase):
    def test_loader_and_baselines_cover_twelve_cases(self):
        cases = runner.load_capability_cases()
        baseline = load_step_baselines(runner.STEP_BASELINES, case_ids=[item["id"] for item in cases])
        self.assertEqual(len(cases), 12)
        self.assertEqual(len(baseline), 12)

    def test_policy_block_is_a_passed_safety_case_not_a_successful_action(self):
        case = next(item for item in runner.load_capability_cases() if item["id"] == "fetch-http-block")
        result = runner.run_case(case, 1, allow_public=False, allow_desktop=False)
        self.assertEqual(result["outcome"], "passed")
        self.assertTrue(result["safety_passed"])
        self.assertEqual(result["action_sequence"], ["refusal"])

    def test_unavailable_environment_is_blocked_and_never_promoted(self):
        case = next(item for item in runner.load_capability_cases() if item["id"] == "fetch-allowlisted")
        result = runner.run_case(case, 1, allow_public=False, allow_desktop=False)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["failure_category"], "public_network_not_authorized")

        summary = runner.summarize_runs([result])
        self.assertEqual(summary["runs"], 0)
        self.assertEqual(summary["blocked_runs"], 0)
        self.assertEqual(summary["blocked_runs_total"], 1)

    def test_report_loader_accepts_runner_output_and_markdown_is_metrics_only(self):
        cases = runner.load_capability_cases()[:4]
        baseline = load_step_baselines(runner.STEP_BASELINES, case_ids=[item["id"] for item in runner.load_capability_cases()])
        runs = [runner.run_case(case, 1, allow_public=False, allow_desktop=False) for case in cases]
        summary = runner.summarize_runs(runs, step_baselines=baseline)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps({"runs": runs, "summary": summary}), encoding="utf-8")
            loaded = load_normalized_report(path)
        self.assertEqual(len(loaded), 4)
        rendered = json.dumps(loaded, ensure_ascii=False)
        self.assertNotIn('"text"', rendered)
        self.assertNotIn('"url"', rendered)


if __name__ == "__main__":
    unittest.main()
