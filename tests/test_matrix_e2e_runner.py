import unittest

from tests.matrix_e2e_runner import load_matrix_cases, run_case


class MatrixE2ERunnerTests(unittest.TestCase):
    def test_matrix_has_sixty_cases(self):
        self.assertEqual(len(load_matrix_cases()), 60)

    def test_repeatable_browser_and_safety_cases_use_real_runtime_paths(self):
        cases = {case["id"]: case for case in load_matrix_cases()}
        self.assertEqual(run_case(cases["web-001"])["outcome"], "passed")
        self.assertEqual(run_case(cases["safety-001"])["outcome"], "passed")
        self.assertEqual(run_case(cases["web-002"])["outcome"], "skipped")

    def test_composite_desktop_cases_reach_verified_terminal_state(self):
        cases = {case["id"]: case for case in load_matrix_cases()}
        for case_id in ("desktop-001", "desktop-004"):
            with self.subTest(case_id=case_id):
                result = run_case(cases[case_id])
                self.assertEqual(result["outcome"], "passed", result)

    def test_windows_research_case_does_not_require_desktop_evidence(self):
        cases = {case["id"]: case for case in load_matrix_cases()}
        result = run_case(cases["research-006"])
        self.assertEqual(result["outcome"], "passed", result)


if __name__ == "__main__":
    unittest.main()
