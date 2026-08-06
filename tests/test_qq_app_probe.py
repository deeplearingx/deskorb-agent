import unittest
from unittest.mock import patch

import e2e_qq_app_probe as probe


class QQAppProbeTests(unittest.TestCase):
    def test_missing_qq_is_skipped_unless_required(self):
        with patch.object(probe.os, "name", "nt"), \
             patch.object(probe, "_qq_windows", return_value=[]):
            optional = probe.run(require_running=False)
            required = probe.run(require_running=True)
        self.assertTrue(optional["ok"])
        self.assertTrue(optional["skipped"])
        self.assertFalse(required["ok"])

    def test_probe_returns_only_safe_profile_counts(self):
        class FakeObserver:
            def observe_active_window(self, hwnd, max_elements=120):
                return {
                    "ok": True,
                    "application": {"id": "qq", "preferred_backend": "uia"},
                    "controls": [{"control_id": "U1"}, {"control_id": "U2"}],
                    "recommended_actions": [{"control_id": "U2", "risk_level": "high"}],
                }

        with patch.object(probe.os, "name", "nt"), \
             patch.object(probe, "_qq_windows", return_value=[101]), \
             patch.object(probe, "DesktopUIA", return_value=FakeObserver()):
            result = probe.run(require_running=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["observations"], [{
            "application_id": "qq", "preferred_backend": "uia",
            "control_count": 2, "recommended_action_count": 1,
            "high_risk_recommendation_count": 1,
        }])
        self.assertNotIn("title", str(result))
        self.assertNotIn("content", str(result))


if __name__ == "__main__":
    unittest.main()
