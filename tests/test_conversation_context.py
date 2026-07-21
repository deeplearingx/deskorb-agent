import unittest

from conversation_context import ConversationContext


class ConversationContextTests(unittest.TestCase):
    def test_force_compaction_preserves_two_recent_turns(self):
        context = ConversationContext(token_budget=4000, recent_turns=3)
        for index in range(5):
            context.add_turn(f"user-{index}", f"assistant-{index}")
        candidate = context.compaction_candidate(force=True)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.message_count, 6)
        self.assertTrue(context.apply_compaction(candidate, "Remembered first three turns."))
        self.assertEqual(context.summary, "Remembered first three turns.")
        self.assertEqual([item.text for item in context.messages], [
            "user-3", "assistant-3", "user-4", "assistant-4",
        ])

    def test_automatic_compaction_uses_token_threshold(self):
        context = ConversationContext(token_budget=4000, recent_turns=2)
        for index in range(4):
            context.add_turn(f"u{index}-" + "x" * 1800, f"a{index}-" + "y" * 1800)
        candidate = context.compaction_candidate()
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.message_count, 4)

    def test_build_input_keeps_complete_recent_turns_under_budget(self):
        context = ConversationContext(token_budget=4000, recent_turns=3)
        context.summary = "Stable facts"
        for index in range(4):
            context.add_turn(f"question-{index}", f"answer-{index}")
        prompt = context.build_input("current")
        self.assertIn("Stable facts", prompt)
        self.assertIn("question-3", prompt)
        self.assertIn("answer-3", prompt)
        self.assertTrue(prompt.endswith("Current user message:\ncurrent"))

    def test_clear_removes_summary_and_messages(self):
        context = ConversationContext()
        context.summary = "summary"
        context.add_turn("hello", "world")
        context.clear()
        self.assertEqual(context.summary, "")
        self.assertEqual(context.messages, [])


if __name__ == "__main__":
    unittest.main()
