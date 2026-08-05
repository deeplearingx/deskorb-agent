import unittest

from browser_state import BrowserState, classify_browser_result


class BrowserStateTests(unittest.TestCase):
    def test_classifies_human_gate_and_loading_as_non_ready(self):
        self.assertEqual(classify_browser_result("snapshot", {"ok": True, "content": "请登录后继续"}), BrowserState.HUMAN_HANDOFF)
        self.assertEqual(classify_browser_result("snapshot", {"ok": True, "content": "页面加载中"}), BrowserState.LOADING)

    def test_empty_and_broken_results_are_not_evidence(self):
        self.assertEqual(classify_browser_result("snapshot", {"ok": True, "content": []}), BrowserState.EMPTY)
        self.assertEqual(classify_browser_result("snapshot", {"ok": False, "error": "Target closed"}), BrowserState.BROKEN)

    def test_structured_result_is_ready(self):
        self.assertEqual(classify_browser_result("snapshot", {"ok": True, "content": [{"type": "text", "text": "Product card"}]}), BrowserState.READY)


if __name__ == "__main__":
    unittest.main()
