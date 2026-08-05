import json
import unittest
from pathlib import Path


class E2ETaskDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).with_name("e2e_task_dataset.json")
        cls.dataset = json.loads(path.read_text(encoding="utf-8"))
        expansion_path = Path(__file__).with_name("e2e_task_dataset_expansions.json")
        cls.expansions = json.loads(expansion_path.read_text(encoding="utf-8"))

    def test_each_case_has_a_unique_id_and_verifiable_assertion(self):
        tasks = self.dataset["tasks"]
        self.assertGreaterEqual(len(tasks), 10)
        ids = [task["id"] for task in tasks]
        self.assertEqual(len(ids), len(set(ids)))
        for task in tasks:
            self.assertTrue(task["prompt"])
            self.assertTrue(task["success_assertions"])
            self.assertIn(task["tier"], {"repeatable", "live_acceptance", "consented_live_acceptance"})

    def test_live_cases_are_not_allowed_to_purchase_or_bypass_captcha(self):
        rules = self.dataset["execution_rules"]
        self.assertTrue(rules["no_real_purchase_or_message_send"])
        self.assertTrue(rules["no_captcha_bypass"])
        live = next(task for task in self.dataset["tasks"] if task["id"] == "web-002")
        self.assertIn("human_verification_needed", live["expected"]["allowed_terminal_states"])

    def test_current_desktop_case_requires_explicit_opt_in(self):
        current = next(task for task in self.dataset["tasks"] if task["id"] == "diagnose-003")
        self.assertTrue(current["setup"]["requires_explicit_opt_in"])
        self.assertIn("privacy", current["risk"])

    def test_release_gates_keep_safety_and_repeatability_separate(self):
        gates = self.dataset["quality_gates"]
        self.assertEqual(gates["minimum_rates"]["safety_pass_rate"], 1.0)
        self.assertEqual(gates["minimum_passes_per_case"]["safety-001"], 3)

    def test_base_and_expansion_cases_form_a_60_case_matrix(self):
        base = self.dataset["tasks"]
        expanded = self.expansions["cases"]
        ids = [item["id"] for item in [*base, *expanded]]
        self.assertEqual(len(ids), 60)
        self.assertEqual(len(ids), len(set(ids)))
        for item in expanded:
            self.assertTrue(item["prompt"])
            self.assertTrue(item["success_assertions"])
            self.assertIn(item["tier"], {"repeatable", "live_acceptance", "consented_live_acceptance"})
            self.assertTrue((item.get("setup") or {}).get("fixture"))


if __name__ == "__main__":
    unittest.main()
