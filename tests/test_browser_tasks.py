import unittest

from browser_tasks import BrowserOwner, BrowserSpaceState, BrowserTaskSpaces


class BrowserTaskSpaceTests(unittest.TestCase):
    def test_agent_and_user_never_control_the_same_space(self):
        spaces = BrowserTaskSpaces(clock=lambda: 100.0)
        created = spaces.create("T001")
        self.assertEqual(created.owner, BrowserOwner.AGENT)
        spaces.require_agent_control("T001")

        handed_off = spaces.hand_off("T001")
        self.assertEqual(handed_off.owner, BrowserOwner.USER)
        self.assertEqual(handed_off.state, BrowserSpaceState.WAITING_HUMAN)
        with self.assertRaises(PermissionError):
            spaces.require_agent_control("T001")
        with self.assertRaises(PermissionError):
            spaces.take_over("T001", user_confirmed=False)

        resumed = spaces.take_over("T001", user_confirmed=True)
        self.assertEqual(resumed.owner, BrowserOwner.AGENT)
        self.assertEqual(resumed.state, BrowserSpaceState.ACTIVE)

    def test_terminal_space_cannot_be_taken_over(self):
        spaces = BrowserTaskSpaces()
        spaces.create("T001")
        closed = spaces.close("T001")
        self.assertEqual(closed.state, BrowserSpaceState.COMPLETED)
        with self.assertRaises(RuntimeError):
            spaces.take_over("T001", user_confirmed=True)

    def test_active_space_is_reused_for_the_same_task(self):
        spaces = BrowserTaskSpaces()
        first = spaces.create("T001")
        second = spaces.create("T001")
        self.assertEqual(first.space_id, second.space_id)

    def test_checkpoint_is_digest_only_and_page_change_invalidates_it(self):
        spaces = BrowserTaskSpaces(clock=lambda: 100.0)
        spaces.create("T001")
        saved = spaces.save_checkpoint("T001", "page-state-1")
        self.assertIsNotNone(saved.checkpoint)
        self.assertNotEqual(saved.checkpoint, "page-state-1")
        self.assertTrue(spaces.checkpoint_matches("T001", "page-state-1"))
        self.assertFalse(spaces.checkpoint_matches("T001", "page-state-2"))

    def test_broken_space_cannot_run_agent_actions_until_recovered(self):
        spaces = BrowserTaskSpaces()
        spaces.create("T001")
        broken = spaces.mark_broken("T001")
        self.assertEqual(broken.state, BrowserSpaceState.BROKEN)
        with self.assertRaises(PermissionError):
            spaces.require_agent_control("T001")
        recovered = spaces.take_over("T001", user_confirmed=True)
        self.assertEqual(recovered.state, BrowserSpaceState.ACTIVE)

    def test_reconnect_replaces_broken_space_and_clears_checkpoint(self):
        spaces = BrowserTaskSpaces()
        original = spaces.create("T001", backend="isolated-playwright")
        spaces.save_checkpoint("T001", "old-page")
        spaces.mark_broken("T001")
        replacement = spaces.reconnect("T001")
        self.assertIsNotNone(replacement)
        self.assertNotEqual(original.space_id, replacement.space_id)
        self.assertEqual(replacement.state, BrowserSpaceState.ACTIVE)
        self.assertIsNone(replacement.checkpoint)
        self.assertFalse(spaces.checkpoint_matches("T001", "old-page"))


if __name__ == "__main__":
    unittest.main()
