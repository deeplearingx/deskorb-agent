import unittest

from extended_browser_acceptance import (
    EXTENDED_PROMPT_CONTRACTS,
    EXTENDED_SCENARIOS,
)


class ExtendedBrowserAcceptanceTests(unittest.TestCase):
    def test_user_six_cases_are_independent_and_bounded(self):
        self.assertEqual(len(EXTENDED_SCENARIOS), 6)
        self.assertEqual(set(EXTENDED_SCENARIOS), set(EXTENDED_PROMPT_CONTRACTS))
        self.assertEqual(
            {item["max_tabs"] for item in EXTENDED_SCENARIOS.values()},
            {6},
        )
        self.assertEqual(EXTENDED_SCENARIOS["case-01-multi-tab-frameworks"]["min_records"], 5)
        self.assertEqual(EXTENDED_SCENARIOS["case-04-infinite-news"]["min_records"], 15)
        self.assertEqual(EXTENDED_SCENARIOS["case-05-ecommerce-filter"]["min_records"], 8)

    def test_high_impact_ecommerce_case_explicitly_forbids_side_effects(self):
        contract = EXTENDED_PROMPT_CONTRACTS["case-05-ecommerce-filter"]
        self.assertIn("加入购物车", contract)
        self.assertIn("立即购买", contract)
        self.assertIn("提交订单", contract)

    def test_wikipedia_case_requires_back_navigation_and_final_home_page(self):
        contract = EXTENDED_PROMPT_CONTRACTS["case-06-wikipedia-back"]
        self.assertIn("go_back", contract)
        self.assertIn("Artificial intelligence 主页面", contract)


if __name__ == "__main__":
    unittest.main()
