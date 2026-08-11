import json
import tempfile
import unittest
from pathlib import Path

from e2e_metrics import load_normalized_report
from tests.run_quality_gate import main


class QualityGateCliTests(unittest.TestCase):
    def test_sample_report_passes_without_printing_private_trace_content(self):
        report = Path(__file__).parent / "fixtures" / "e2e" / "sample_normalized_report.json"
        self.assertEqual(main(["--report", str(report)]), 0)

    def test_raw_trace_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.json"
            path.write_text(json.dumps({"runs": [{"case_id": "x", "outcome": "passed", "prompt": "secret"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_normalized_report(path)

    def test_full_matrix_gate_defaults_to_repeatable_cases(self):
        report = Path(__file__).parent / "fixtures" / "e2e" / "sample_normalized_report.json"
        self.assertEqual(main(["--report", str(report), "--require-full-matrix"]), 2)

    def test_validate_report_only_accepts_public_subset_without_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.json"
            missing_dataset = Path(directory) / "missing-dataset.json"
            path.write_text(json.dumps({"runs": [
                {"case_id": "taobao-search", "outcome": "passed"},
                {"case_id": "bing-fastapi", "outcome": "passed"},
            ]}), encoding="utf-8")
            self.assertEqual(main([
                "--report", str(path), "--dataset", str(missing_dataset), "--validate-report-only",
            ]), 0)

    def test_validate_report_only_still_rejects_private_trace_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.json"
            path.write_text(json.dumps({"runs": [{
                "case_id": "taobao-search", "outcome": "passed",
                "nested": {"url": "https://example.test/private"},
            }]}), encoding="utf-8")
            self.assertEqual(main(["--report", str(path), "--validate-report-only"]), 2)

    def test_default_gate_does_not_treat_public_subset_as_release_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.json"
            path.write_text(json.dumps({"runs": [{
                "case_id": "taobao-search", "outcome": "passed",
                "requires_evidence": True, "evidence_passed": True,
                "safety_case": True, "safety_passed": True,
                "needs_task_confirmation": True, "task_confirmation_once": True,
            }]}), encoding="utf-8")
            self.assertEqual(main(["--report", str(path)]), 2)

    def test_report_only_mode_rejects_matrix_coverage_flag(self):
        report = Path(__file__).parent / "fixtures" / "e2e" / "sample_normalized_report.json"
        self.assertEqual(main(["--report", str(report), "--validate-report-only", "--require-full-matrix"]), 2)


if __name__ == "__main__":
    unittest.main()
