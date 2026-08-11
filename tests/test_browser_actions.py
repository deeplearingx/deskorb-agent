import unittest

from browser_actions import validate_browser_action_batch


class BrowserActionTests(unittest.TestCase):
    def test_accepts_bounded_semantic_actions(self):
        actions, error = validate_browser_action_batch([
            {"action": "navigate", "arguments": {"url": "https://example.test/search"}},
            {"action": "snapshot", "arguments": {}},
            {"action": "click_ref", "arguments": {"ref": "e17"}},
        ])
        self.assertIsNone(error)
        self.assertEqual([item.action for item in actions], ["navigate", "snapshot", "click_ref"])

    def test_rejects_raw_script_and_unbounded_actions(self):
        _actions, error = validate_browser_action_batch([{"action": "javascript", "arguments": {"code": "alert(1)"}}])
        self.assertIn("unsupported", error)
        _actions, error = validate_browser_action_batch([{"action": "snapshot", "arguments": {}}] * 9)
        self.assertIn("1-8", error)

    def test_validates_tab_switch_and_structured_extract_contract(self):
        actions, error = validate_browser_action_batch([
            {"action": "switch_tab", "arguments": {"index": 1}},
            {"action": "extract", "arguments": {"ref": "e17", "fields": ["title", "price"]}},
        ])
        self.assertIsNone(error)
        self.assertEqual([item.action for item in actions], ["switch_tab", "extract"])
        _actions, error = validate_browser_action_batch([{"action": "switch_tab", "arguments": {"index": -1}}])
        self.assertIn("non-negative", error)
        _actions, error = validate_browser_action_batch([{"action": "extract", "arguments": {"ref": "e17", "fields": [""]}}])
        self.assertIn("semantic names", error)

    def test_extract_bounds_ref_and_semantic_field_names(self):
        _actions, error = validate_browser_action_batch([{
            "action": "extract", "arguments": {"ref": "e" * 81, "fields": ["title"]},
        }])
        self.assertIsNotNone(error)
        _actions, error = validate_browser_action_batch([{
            "action": "extract", "arguments": {"ref": "e17", "fields": ["title" * 41]},
        }])
        self.assertIsNotNone(error)
        _actions, error = validate_browser_action_batch([{
            "action": "extract", "arguments": {"ref": "e17", "fields": ["title[0]"]},
        }])
        self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
