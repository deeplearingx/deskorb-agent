import unittest

from public_browser_scenarios import SCENARIOS, selected_scenarios


class PublicBrowserScenarioTests(unittest.TestCase):
    def test_scenarios_are_explicitly_live_read_only_and_domain_scoped(self):
        self.assertEqual(set(SCENARIOS), {"taobao-search", "bing-fastapi"})
        for scenario in SCENARIOS.values():
            self.assertTrue(scenario.live_only)
            self.assertTrue(scenario.allowed_domains)
            self.assertTrue(scenario.prompt)
            self.assertFalse(set(scenario.forbidden_browser_actions) & {"navigate", "snapshot", "extract", "verify", "wait"})

    def test_taobao_contract_requires_price_and_product_evidence(self):
        scenario = SCENARIOS["taobao-search"]
        self.assertIn("taobao.com", scenario.allowed_domains)
        self.assertEqual(scenario.required_fields, ("title", "price", "url"))
        self.assertEqual(scenario.price_range, (100.0, 150.0))
        self.assertEqual(scenario.minimum_results, 3)
        self.assertIn("男士", scenario.required_contains)
        self.assertIn("T 恤", scenario.required_contains)
        self.assertEqual(scenario.evidence_source_domains, ("taobao.com", "tmall.com"))

    def test_bing_contract_requires_three_distinct_results_and_official_source(self):
        scenario = SCENARIOS["bing-fastapi"]
        self.assertIn("bing.com", scenario.allowed_domains)
        self.assertEqual(scenario.minimum_results, 3)
        self.assertTrue(scenario.click_requires_evidence)
        self.assertIn("fastapi.tiangolo.com", scenario.required_source_domains)
        # Search-result metadata may name a public third-party source.  It is
        # evidence read from Bing, not permission to navigate to that source;
        # navigation remains constrained by allowed_domains.
        self.assertEqual(scenario.evidence_source_domains, ())
        self.assertEqual(scenario.required_fields, ("title", "source", "url"))

    def test_bing_prompt_allows_bounded_public_result_clicks(self):
        prompt = SCENARIOS["bing-fastapi"].prompt
        self.assertIn("https://www.bing.com/search", prompt)
        self.assertIn("必须从最新快照中定位", prompt)
        self.assertIn("点击确认官方资料", prompt)
        self.assertIn("点击前先", prompt)
        self.assertIn("提取三条", prompt)
        self.assertIn("不要点击广告", prompt)
        self.assertNotIn("不要点击任何结果链接", prompt)

    def test_selecting_scenarios_rejects_unknown_and_deduplicates(self):
        self.assertEqual([item.case_id for item in selected_scenarios("taobao-search,taobao-search")], ["taobao-search"])
        self.assertEqual([item.case_id for item in selected_scenarios("all")], ["taobao-search", "bing-fastapi"])
        with self.assertRaises(ValueError):
            selected_scenarios("unknown")


if __name__ == "__main__":
    unittest.main()
