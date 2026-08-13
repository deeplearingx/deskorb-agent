import unittest
from unittest.mock import patch

from real_stability_runner import (
    _browser_definition,
    _real_notepad_case,
    _safe_trace_hash,
    _stage_transition_total,
    load_flagship_scenarios,
    run_flagship_matrix,
    validate_stability_run,
)


class RealStabilityRunnerTests(unittest.TestCase):
    def test_manifest_has_twelve_bounded_scenarios(self):
        scenarios = load_flagship_scenarios()
        self.assertEqual(len(scenarios), 12)
        self.assertEqual(len({item["id"] for item in scenarios}), 12)
        self.assertEqual({item["family"] for item in scenarios}, {"browser", "desktop", "cross_domain"})

    def test_matrix_runs_each_scenario_ten_times_by_default_contract(self):
        scenarios = load_flagship_scenarios()
        runs = run_flagship_matrix(lambda scenario, attempt: {
            "outcome": "passed", "postcondition_kind": scenario["postcondition_kind"],
            "environment_class": "local_fixture", "action_steps": 3,
        }, repetitions=10)
        self.assertEqual(len(runs), 120)
        self.assertEqual({item["case_id"] for item in runs}, {item["id"] for item in scenarios})
        self.assertTrue(all(item["attempt"] in range(1, 11) for item in runs))

    def test_validation_rejects_incomplete_or_private_run(self):
        with self.assertRaises(ValueError):
            validate_stability_run([], repetitions=10)
        runs = [{"case_id": load_flagship_scenarios()[0]["id"], "outcome": "passed", "prompt": "secret"}]
        with self.assertRaises(ValueError):
            validate_stability_run(runs, repetitions=1)

    def test_each_browser_variation_has_its_own_fixture_contract(self):
        expected = {
            "flagship-browser-autocomplete": "dynamic_search.html",
            "flagship-browser-spa": "spa_search.html",
            "flagship-browser-pagination": "pagination_search.html",
            "flagship-browser-new-tab": "new_tab_search.html",
            "flagship-browser-no-progress": "no_progress_search.html",
            "flagship-browser-stale-ref": "stale_ref_search.html",
        }
        for scenario_id, page in expected.items():
            definition = _browser_definition(scenario_id)
            self.assertIsNotNone(definition)
            self.assertEqual(definition[1]["page"], page)

    def test_trace_hash_is_stable_and_does_not_include_page_data(self):
        first = _safe_trace_hash({"browser_trace": [{
            "action_types": ["snapshot", "click_ref"],
            "ok": True, "state_changed": True,
            "failure_kind": None, "execution_source": "model",
            "cache_status": "miss", "content": "must not be read",
        }]})
        second = _safe_trace_hash({"browser_trace": [{
            "action_types": ["snapshot", "click_ref"],
            "ok": True, "state_changed": True,
            "failure_kind": None, "execution_source": "model",
            "cache_status": "miss", "content": "different page data",
        }]})
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_stage_transition_metric_uses_cumulative_trace_value_once(self):
        trace = [
            {"stage_transition_count": 1},
            {"stage_transition_count": 3},
            {"stage_transition_count": 4},
        ]
        self.assertEqual(_stage_transition_total(trace), 4)

    def test_desktop_adapter_is_fail_closed_without_explicit_authorization(self):
        scenario = next(item for item in load_flagship_scenarios()
                        if item["id"] == "flagship-desktop-notepad")
        result = _real_notepad_case(scenario, 1, working_dir=".", allow_current_desktop=False)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["failure_kind"], "current_desktop_not_authorized")

    def test_desktop_adapter_rechecks_interactive_preflight(self):
        scenario = next(item for item in load_flagship_scenarios()
                        if item["id"] == "flagship-desktop-notepad")
        with patch("local_real_e2e_runner.preflight_current_desktop",
                   return_value={"ok": False}):
            result = _real_notepad_case(
                scenario, 1, working_dir=".", allow_current_desktop=True,
            )
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["failure_kind"], "current_desktop_preflight_failed")

    def test_no_progress_scenario_is_not_scored_as_a_normal_failure(self):
        # The real runner classifies the no-progress fixture as a safety
        # boundary. Its terminal result must be blocked or handoff, never a
        # successful completion and never an unlabeled failure.
        from real_stability_runner import _REAL_BROWSER_SCENARIOS
        self.assertIn("flagship-browser-no-progress", _REAL_BROWSER_SCENARIOS)


if __name__ == "__main__":
    unittest.main()
