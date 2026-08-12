import unittest

from browser_actions import validate_browser_action_batch


class BrowserActionTests(unittest.TestCase):
    def test_accepts_bounded_semantic_actions(self):
        actions, error = validate_browser_action_batch([
            {"action": "snapshot", "arguments": {}},
            {"action": "click_ref", "arguments": {"ref": "e17", "observation_id": "obs-1"}},
        ])
        self.assertIsNone(error)
        self.assertEqual([item.action for item in actions], ["snapshot", "click_ref"])

    def test_requires_current_observation_for_ref_actions(self):
        _actions, error = validate_browser_action_batch([{
            "action": "click_ref", "arguments": {"ref": "e17"},
        }])
        self.assertIn("observation_id", error)

    def test_allows_one_terminal_state_change_only(self):
        actions, error = validate_browser_action_batch([
            {"action": "snapshot", "arguments": {}},
            {"action": "select_ref", "arguments": {
                "ref": "e17", "values": ["Python"], "observation_id": "obs-1",
            }},
        ])
        self.assertIsNone(error)
        self.assertEqual(actions[-1].action, "select_ref")

        _actions, error = validate_browser_action_batch([
            {"action": "click_ref", "arguments": {"ref": "e1", "observation_id": "obs-1"}},
            {"action": "snapshot", "arguments": {}},
        ])
        self.assertIn("final action", error)

        _actions, error = validate_browser_action_batch([
            {"action": "click_ref", "arguments": {"ref": "e1", "observation_id": "obs-1"}},
            {"action": "fill_ref", "arguments": {
                "ref": "e2", "value": "x", "observation_id": "obs-1",
            }},
        ])
        self.assertIn("at most one", error)

    def test_rejects_raw_script_and_unbounded_actions(self):
        _actions, error = validate_browser_action_batch([{"action": "javascript", "arguments": {"code": "alert(1)"}}])
        self.assertIn("unsupported", error)
        _actions, error = validate_browser_action_batch([{"action": "snapshot", "arguments": {}}] * 9)
        self.assertIn("1-8", error)

    def test_validates_tab_switch_and_structured_extract_contract(self):
        actions, error = validate_browser_action_batch([
            {"action": "snapshot", "arguments": {}},
            {"action": "extract", "arguments": {
                "ref": "e17", "fields": ["title", "price"], "observation_id": "obs-1",
            }},
        ])
        self.assertIsNone(error)
        self.assertEqual([item.action for item in actions], ["snapshot", "extract"])
        _actions, error = validate_browser_action_batch([{"action": "switch_tab", "arguments": {"index": -1}}])
        self.assertIn("non-negative", error)
        _actions, error = validate_browser_action_batch([{"action": "extract", "arguments": {
            "ref": "e17", "fields": [""], "observation_id": "obs-1",
        }}])
        self.assertIn("semantic names", error)

    def test_extract_bounds_ref_and_semantic_field_names(self):
        _actions, error = validate_browser_action_batch([{
            "action": "extract", "arguments": {"ref": "e" * 81, "fields": ["title"]},
        }])
        self.assertIsNotNone(error)

    def test_accepts_model_alias_for_fill_and_wait_duration(self):
        actions, error = validate_browser_action_batch([
            {"action": "snapshot", "arguments": {}},
            {"action": "fill_ref", "arguments": {
                "ref": "e17", "text": "Python", "observation_id": "obs-1",
            }},
        ])
        self.assertIsNone(error)
        self.assertEqual(actions[-1].arguments["value"], "Python")
        actions, error = validate_browser_action_batch([
            {"action": "wait", "arguments": {"observation_id": "obs-1", "ms": 250}},
        ])
        self.assertIsNone(error)
        self.assertEqual(actions[0].action, "wait")
        actions, error = validate_browser_action_batch([{
            "action": "extract", "arguments": {
                "ref": "e17", "selectors": ["heading", "paragraph"],
                "observation_id": "obs-1",
            },
        }])
        self.assertIsNone(error)
        self.assertEqual(actions[0].arguments["fields"], ["title", "source"])
        actions, error = validate_browser_action_batch([{
            "action": "verify", "arguments": {
                "expected": {"title": "Python asyncio 入门"},
            },
        }])
        self.assertIsNone(error)
        self.assertEqual(actions[0].arguments["required_fields"], ["title"])
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
