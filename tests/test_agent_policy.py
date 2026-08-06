import unittest

from agent_policy import ApprovalManager, DecisionKind, Risk, ToolPolicy


class ToolPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ToolPolicy()

    def test_observation_is_allowed_in_read_only_mode(self):
        decision = self.policy.decide("filesystem_read_text", execution_requested=False, full_access=False)
        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertEqual(decision.risk, Risk.OBSERVE)

    def test_window_listing_is_allowed_in_read_only_mode(self):
        decision = self.policy.decide("desktop_list_windows", execution_requested=False, full_access=False)
        self.assertEqual(decision.kind, DecisionKind.ALLOW)

    def test_mcp_read_only_is_allowed_in_read_only_mode(self):
        decision = self.policy.decide("mcp_read_only", execution_requested=False, full_access=False)
        self.assertEqual(decision.kind, DecisionKind.ALLOW)
        self.assertEqual(decision.risk, Risk.OBSERVE)

    def test_write_requires_explicit_execution_intent(self):
        decision = self.policy.decide("filesystem_write", execution_requested=False, full_access=True)
        self.assertEqual(decision.kind, DecisionKind.DENY)

    def test_shell_requires_confirmation_even_in_full_access(self):
        decision = self.policy.decide("shell_run", execution_requested=True, full_access=True)
        self.assertEqual(decision.kind, DecisionKind.CONFIRM)
        self.assertEqual(decision.risk, Risk.DESTRUCTIVE_LOCAL)

    def test_desktop_click_requires_confirmation(self):
        decision = self.policy.decide("desktop_click", execution_requested=True, full_access=True)
        self.assertEqual(decision.kind, DecisionKind.CONFIRM)
        self.assertEqual(decision.risk, Risk.EXTERNAL_OR_ELEVATED)

    def test_keyboard_actions_require_confirmation(self):
        for tool in ("desktop_type", "desktop_hotkey"):
            with self.subTest(tool=tool):
                decision = self.policy.decide(tool, execution_requested=True, full_access=True)
                self.assertEqual(decision.kind, DecisionKind.CONFIRM)

    def test_scroll_and_focus_require_confirmation(self):
        for tool in ("desktop_scroll", "window_focus", "window_control", "desktop_clipboard_read_text"):
            with self.subTest(tool=tool):
                decision = self.policy.decide(tool, execution_requested=True, full_access=True)
            self.assertEqual(decision.kind, DecisionKind.CONFIRM)

    def test_application_launch_requires_confirmation(self):
        decision = self.policy.decide("application_launch", execution_requested=True, full_access=True)
        self.assertEqual(decision.kind, DecisionKind.CONFIRM)

    def test_task_authorization_allows_normal_desktop_steps(self):
        for tool in ("application_launch", "desktop_click", "desktop_type", "desktop_hotkey", "window_control"):
            with self.subTest(tool=tool):
                decision = self.policy.decide(tool, execution_requested=True, full_access=True,
                                              task_authorized=True)
                self.assertEqual(decision.kind, DecisionKind.ALLOW)

    def test_high_risk_step_still_requires_confirmation_during_task(self):
        decision = self.policy.decide("desktop_click", execution_requested=True, full_access=True,
                                      task_authorized=True, high_risk=True)
        self.assertEqual(decision.kind, DecisionKind.CONFIRM)


class ApprovalManagerTests(unittest.TestCase):
    def test_exact_token_is_required_and_consumed_once(self):
        now = [100.0]
        manager = ApprovalManager(clock=lambda: now[0])
        request = manager.create("shell_run", {"command": "echo safe"}, Risk.DESTRUCTIVE_LOCAL, "Run a command")
        self.assertEqual(manager.resolve("确认 WRONG")[0], "pending")
        self.assertEqual(manager.resolve(f"确认 {request.token}")[0], "approved")
        self.assertEqual(manager.resolve(f"确认 {request.token}")[0], "none")

    def test_cancel_and_expiry_drop_pending_action(self):
        now = [100.0]
        manager = ApprovalManager(ttl_seconds=20, clock=lambda: now[0])
        manager.create("filesystem_delete", {"path": "x"}, Risk.DESTRUCTIVE_LOCAL, "Delete x")
        self.assertEqual(manager.resolve("取消")[0], "cancelled")
        manager.create("filesystem_delete", {"path": "x"}, Risk.DESTRUCTIVE_LOCAL, "Delete x")
        now[0] = 121.0
        self.assertEqual(manager.resolve("确认 ANY")[0], "expired")


if __name__ == "__main__":
    unittest.main()
