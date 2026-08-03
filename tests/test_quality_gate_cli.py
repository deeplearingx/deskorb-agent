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


if __name__ == "__main__":
    unittest.main()
