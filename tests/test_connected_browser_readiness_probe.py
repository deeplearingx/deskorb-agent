import unittest
from unittest.mock import patch

import connected_browser_readiness_probe as probe


class FakeBridge:
    available_servers = ("playwright",)

    def __init__(self, *_args, **_kwargs):
        self.closed = False

    def schemas_for_task(self, _servers):
        return []

    def find_tool(self, _server, suffixes):
        suffixes = tuple(suffixes)
        if "browser_tabs" in suffixes:
            return "tabs"
        if "browser_snapshot" in suffixes:
            return "snapshot"
        return None

    def call(self, name, _arguments):
        if name == "tabs":
            return {"ok": True, "content": [{"title": "safe"}]}
        return {"ok": True, "content": "Dashboard — signed in"}

    def close(self):
        self.closed = True


class ConnectedBrowserReadinessTests(unittest.TestCase):
    def test_authenticated_snapshot_is_ready_without_printing_page_content(self):
        with patch.object(probe, "MCPToolBridge", FakeBridge):
            result = probe.run(require_connected=True, require_authenticated=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "connected_ready")
        self.assertNotIn("Dashboard", result)

    def test_login_marker_fails_authenticated_gate(self):
        class LoginBridge(FakeBridge):
            def call(self, name, arguments):
                if name == "tabs":
                    return super().call(name, arguments)
                return {"ok": True, "content": "请先登录 / CAPTCHA"}

        with patch.object(probe, "MCPToolBridge", LoginBridge):
            result = probe.run(require_connected=True, require_authenticated=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "human_handoff_required")


if __name__ == "__main__":
    unittest.main()
