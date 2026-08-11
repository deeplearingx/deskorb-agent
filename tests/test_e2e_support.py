import unittest

from tests.e2e_support.datasets import (
    E2E_DATASET_PATH,
    E2E_EXPANSIONS_PATH,
    E2E_STEP_BASELINES_PATH,
    load_e2e_matrix_cases,
)


class E2ESupportTests(unittest.TestCase):
    def test_canonical_paths_point_to_checked_in_matrix_assets(self):
        self.assertTrue(E2E_DATASET_PATH.is_file())
        self.assertTrue(E2E_EXPANSIONS_PATH.is_file())
        self.assertTrue(E2E_STEP_BASELINES_PATH.is_file())

    def test_canonical_loader_returns_sixty_unique_cases(self):
        cases = load_e2e_matrix_cases()
        self.assertEqual(len(cases), 60)
        self.assertEqual(len({str(item["id"]) for item in cases}), 60)

    def test_runner_loaders_delegate_to_the_same_matrix(self):
        from tests.local_real_e2e_runner import load_matrix_cases as load_real_cases
        from tests.matrix_e2e_runner import load_matrix_cases as load_control_cases

        expected = [str(item["id"]) for item in load_e2e_matrix_cases()]
        self.assertEqual([str(item["id"]) for item in load_real_cases()], expected)
        self.assertEqual([str(item["id"]) for item in load_control_cases()], expected)


if __name__ == "__main__":
    unittest.main()
