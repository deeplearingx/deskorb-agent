import unittest

from desktop_adapters import DesktopApplicationRegistry


class DesktopApplicationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.registry = DesktopApplicationRegistry()

    def test_matches_known_applications_by_process_basename(self):
        notepad = self.registry.match(r"C:\Windows\System32\notepad.exe")
        browser = self.registry.match("msedge.exe")
        self.assertEqual(notepad.app_id, "notepad")
        self.assertEqual(notepad.preferred_backend, "uia")
        self.assertEqual(browser.app_id, "chromium")
        self.assertEqual(browser.preferred_backend, "playwright")

    def test_unknown_process_gets_conservative_generic_profile(self):
        profile = self.registry.match("unknown-app.exe")
        self.assertEqual(profile.app_id, "generic")
        self.assertIn("fresh observation", " ".join(profile.verification_hints).lower())

    def test_catalog_contains_no_process_paths_or_secrets(self):
        catalog = self.registry.catalog()
        self.assertTrue(catalog)
        text = str(catalog).lower()
        self.assertNotIn("password", text)
        self.assertNotIn("api_key", text)

    def test_recommends_only_advertised_semantic_actions_and_marks_qq_send_high_risk(self):
        controls = [
            {"control_id": "U1", "name": "消息输入", "control_type": "Edit",
             "enabled": True, "actions": ["set_value"]},
            {"control_id": "U2", "name": "发送", "control_type": "Button",
             "enabled": True, "actions": ["invoke"]},
            {"control_id": "U3", "name": "不可用", "control_type": "Button",
             "enabled": False, "actions": ["invoke"]},
        ]
        actions = self.registry.recommended_actions("QQNT.exe", controls)
        self.assertEqual([item["control_id"] for item in actions], ["U1", "U2"])
        self.assertEqual(actions[1]["risk_level"], "high")

    def test_browser_profile_does_not_suggest_ui_actions_for_page_content(self):
        controls = [{"control_id": "U1", "name": "地址", "control_type": "Edit",
                     "enabled": True, "actions": ["set_value"]}]
        self.assertEqual(self.registry.recommended_actions("msedge.exe", controls), [])


if __name__ == "__main__":
    unittest.main()
