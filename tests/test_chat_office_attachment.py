import unittest
from unittest.mock import Mock, patch

from deskorb_agent import Overlay
from office_sources import OfficeSnapshot, OfficeTarget
from office_edits import OfficeApplyResult, OfficeEditPlan, OfficeEditRecord, WordTextEdit


class ChatOfficeAttachmentTests(unittest.TestCase):
    def _overlay(self):
        overlay = Overlay.__new__(Overlay)
        overlay.chat_office_snapshot = None
        overlay._office_plan_active = False
        overlay._office_plan_raw = []
        overlay._pending_office_plan = None
        overlay._office_apply_active = False
        overlay.office_edit_history = []
        overlay._office_generation = 0
        overlay._office_operation_sequence = 0
        overlay._chat_word_read_sequence = 0
        overlay._chat_word_read_request = None
        overlay._chat_word_read_timeout_after = None
        overlay.add_user = Mock()
        overlay.add_err = Mock()
        overlay.add_delta = Mock()
        overlay.add_sys = Mock()
        overlay._set_busy = Mock()
        overlay._refresh_chat_word_attachment = Mock()
        overlay.worker = Mock()
        return overlay

    def test_office_attachment_uses_ephemeral_plan_without_chat_content_leak(self):
        overlay = self._overlay()
        overlay.chat_office_snapshot = OfficeSnapshot(
            kind="excel", expected_root=202, identity="excel:202:Plan.xlsx", name="Plan.xlsx",
            rendered_text="[Budget!C3] value=120 formula='=SUM(C1:C2)'", fingerprint="fingerprint",
            targets=(OfficeTarget("Budget!C3", "Budget!C3", 120, "=SUM(C1:C2)"),),
            has_unsaved_changes=False,
        )
        overlay.office_edit_history = [OfficeEditRecord(
            operation_id="op-1", kind="excel", identity="excel:202:Plan.xlsx",
            edits=(),
        )]

        overlay._send_chat_office_attachment("Update the total.")

        prompt = overlay.worker.ask_office_context.call_args.args[1]
        self.assertIn("Budget!C3", prompt)
        self.assertIn("Update the total.", prompt)
        self.assertIn('"plan": null', prompt)
        self.assertIn("operation op-1", prompt)
        self.assertIn("word_replace_text", prompt)
        self.assertIn("excel_set_cell", prompt)
        self.assertEqual(overlay.worker.ask_office_context.call_args.args[0], "Update the total.")
        self.assertTrue(overlay._office_plan_active)
        self.assertIsNotNone(overlay.chat_office_snapshot)
        display = overlay.add_user.call_args.args[0]
        self.assertIn("Plan.xlsx", display)
        self.assertNotIn("Budget!C3", display)

    def test_persistent_prompt_includes_word_edit_history_without_displaying_it(self):
        overlay = self._overlay()
        overlay.chat_office_snapshot = OfficeSnapshot(
            kind="word", expected_root=101, identity="word:101:Draft.docx", name="Draft.docx",
            rendered_text="[paragraph:1] value='New'", fingerprint="after",
            targets=(OfficeTarget("paragraph:1", "paragraph:1", "New"),),
            has_unsaved_changes=True,
        )
        overlay.office_edit_history = [OfficeEditRecord(
            operation_id="op-2", kind="word", identity="word:101:Draft.docx",
            edits=(WordTextEdit("paragraph:1", "Old", "New"),),
        )]

        overlay._send_chat_office_attachment("Summarize the latest change.")

        prompt = overlay.worker.ask_office_context.call_args.args[1]
        self.assertIn("paragraph:1", prompt)
        self.assertIn("'Old'", prompt)
        self.assertIn("'New'", prompt)
        display = overlay.add_user.call_args.args[0]
        self.assertNotIn("Old", display)
        self.assertNotIn("New", display)

    def test_word_prompt_explains_bounded_final_paragraph_insertion(self):
        overlay = self._overlay()
        overlay.chat_office_snapshot = OfficeSnapshot(
            kind="word", expected_root=101, identity="word:101:Draft.docx", name="Draft.docx",
            rendered_text="[paragraph:1] value='Last'", fingerprint="after",
            targets=(OfficeTarget("paragraph:1", "paragraph:1", "Last"),),
            has_unsaved_changes=True, paragraph_count=1,
        )

        overlay._send_chat_office_attachment("在文章末尾添加总结")

        prompt = overlay.worker.ask_office_context.call_args.args[1]
        self.assertIn("word_insert_paragraph_after", prompt)
        self.assertIn("Word paragraph count: 1", prompt)
        self.assertIn("Never invent a new paragraph locator", prompt)

    def test_office_plan_delta_is_buffered_not_rendered(self):
        overlay = self._overlay()
        overlay._office_plan_active = True

        overlay._handle("delta", '{"answer":"private"}')

        self.assertEqual(overlay._office_plan_raw, ['{"answer":"private"}'])
        overlay.add_delta.assert_not_called()

    def test_clear_persistent_office_removes_snapshot_history_plan_and_raw_buffer(self):
        overlay = self._overlay()
        overlay.chat_office_snapshot = Mock()
        overlay.office_edit_history = [Mock()]
        overlay._pending_office_plan = Mock()
        overlay._office_plan_raw = ["private document text"]
        overlay._office_plan_active = True

        overlay._clear_persistent_office()

        self.assertIsNone(overlay.chat_office_snapshot)
        self.assertEqual(overlay.office_edit_history, [])
        self.assertIsNone(overlay._pending_office_plan)
        self.assertEqual(overlay._office_plan_raw, [])
        self.assertFalse(overlay._office_plan_active)
        self.assertEqual(overlay._office_generation, 1)

    def test_late_office_delta_and_done_are_ignored_after_clear(self):
        overlay = self._overlay()
        overlay._office_generation = 4
        overlay._office_plan_active = True
        overlay._handle("office_delta", (4, "private response"))
        self.assertEqual(overlay._office_plan_raw, ["private response"])
        overlay._finish_office_plan = Mock()

        overlay._clear_persistent_office()
        overlay._handle("office_delta", (4, "late private response"))
        overlay._handle("office_plan_done", 4)

        self.assertEqual(overlay._office_plan_raw, [])
        overlay._finish_office_plan.assert_not_called()

    def test_malformed_office_response_keeps_attachment_for_retry(self):
        overlay = self._overlay()
        snapshot = Mock()
        overlay.chat_office_snapshot = snapshot
        overlay._office_plan_active = True
        overlay._office_plan_raw = ["not-json"]
        overlay._pending_office_plan = Mock()
        overlay._md_finalize = Mock()
        overlay._finish_turn_copy = Mock()

        overlay._finish_office_plan()

        self.assertIs(overlay.chat_office_snapshot, snapshot)
        self.assertIsNone(overlay._pending_office_plan)

    def test_clear_cancels_pending_read_and_invalidates_late_read(self):
        overlay = self._overlay()
        overlay.root = Mock()
        overlay._chat_word_read_request = 3
        overlay._chat_word_read_timeout_after = "timer"

        overlay._clear_chat_word_attachment()

        overlay.root.after_cancel.assert_called_once_with("timer")
        self.assertIsNone(overlay._chat_word_read_request)
        self.assertEqual(overlay._chat_word_read_sequence, 1)

    def test_successful_office_apply_keeps_attachment_for_follow_up(self):
        overlay = self._overlay()
        snapshot = OfficeSnapshot(
            kind="word", expected_root=101, identity="word:101:Draft.docx", name="Draft.docx",
            rendered_text="[paragraph:1] value='New'", fingerprint="after",
            targets=(OfficeTarget("paragraph:1", "paragraph:1", "New"),), has_unsaved_changes=True,
        )
        overlay.chat_office_snapshot = snapshot
        result = Mock(message="Changes were written to the open document and were not saved.")

        overlay._handle("office_apply", (result, None))

        self.assertIs(overlay.chat_office_snapshot, snapshot)
        self.assertIsNone(overlay._pending_office_plan)

    def test_successful_office_apply_refreshes_snapshot_and_records_history(self):
        before = OfficeSnapshot(
            kind="word", expected_root=101, identity="word:101:Draft.docx", name="Draft.docx",
            rendered_text="[paragraph:1] value='Old'", fingerprint="before",
            targets=(OfficeTarget("paragraph:1", "paragraph:1", "Old"),), has_unsaved_changes=True,
        )
        after = OfficeSnapshot(
            kind="word", expected_root=101, identity="word:101:Draft.docx", name="Draft.docx",
            rendered_text="[paragraph:1] value='New'", fingerprint="after",
            targets=(OfficeTarget("paragraph:1", "paragraph:1", "New"),), has_unsaved_changes=True,
        )
        overlay = self._overlay()
        overlay.chat_office_snapshot = before
        result = OfficeApplyResult(1, 1, "written")
        record = OfficeEditRecord(
            "office-1", "word", before.identity,
            (WordTextEdit("paragraph:1", "Old", "New"),),
        )

        overlay._handle("office_apply", (0, after, record, result, None))

        self.assertIs(overlay.chat_office_snapshot, after)
        self.assertEqual(overlay.office_edit_history, [record])
        overlay._refresh_chat_word_attachment.assert_called_once_with()

    def test_undo_request_builds_inverse_preview_from_latest_history(self):
        overlay = self._overlay()
        snapshot = OfficeSnapshot(
            kind="word", expected_root=101, identity="word:101:Draft.docx", name="Draft.docx",
            rendered_text="[paragraph:1] value='New'", fingerprint="after",
            targets=(OfficeTarget("paragraph:1", "paragraph:1", "New"),), has_unsaved_changes=True,
        )
        overlay.chat_office_snapshot = snapshot
        overlay.office_edit_history = [OfficeEditRecord(
            "office-1", "word", snapshot.identity,
            (WordTextEdit("paragraph:1", "Old", "New"),),
        )]
        overlay._add_office_plan_actions = Mock()

        overlay._send_chat_office_attachment("撤回刚才的修改")

        overlay.worker.ask_office_context.assert_not_called()
        self.assertEqual(overlay._pending_office_plan.edits,
                         (WordTextEdit("paragraph:1", "New", "Old"),))
        overlay._add_office_plan_actions.assert_called_once_with(overlay._pending_office_plan)

    def test_undo_without_agent_history_recommends_native_office_undo(self):
        overlay = self._overlay()
        overlay.chat_office_snapshot = OfficeSnapshot(
            kind="word", expected_root=101, identity="word:101:Draft.docx", name="Draft.docx",
            rendered_text="[paragraph:1] value='New'", fingerprint="after",
            targets=(OfficeTarget("paragraph:1", "paragraph:1", "New"),), has_unsaved_changes=True,
        )

        overlay._send_chat_office_attachment("undo the last change")

        overlay.worker.ask_office_context.assert_not_called()
        self.assertIn("Ctrl+Z", overlay.add_sys.call_args.args[0])

    def test_office_preview_places_actions_below_long_preview_text(self):
        from office_edits import OfficeEditPlan, WordTextEdit

        overlay = self._overlay()
        overlay.chat = Mock()
        overlay.px = lambda value: value
        overlay.f_small = "small"
        plan = OfficeEditPlan("word", "fingerprint", (
            WordTextEdit("paragraph:3", "A long original paragraph", "A long replacement paragraph"),
        ))

        with patch("deskorb_agent.tk.Frame", side_effect=[Mock(), Mock()]), \
                patch("deskorb_agent.tk.Label") as label, \
                patch("deskorb_agent.tk.Button"):
            overlay._add_office_plan_actions(plan)

        label.return_value.pack.assert_called_once_with(side="top", fill="x")


if __name__ == "__main__":
    unittest.main()
