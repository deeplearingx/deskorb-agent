import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from office_sources import OfficeSnapshot, OfficeTarget


def word_snapshot():
    return OfficeSnapshot(
        kind="word", expected_root=101, identity="word:101:C:/docs/Project.docx",
        name="Project.docx", rendered_text="[paragraph:1] value='Old'", fingerprint="word-fingerprint",
        targets=(OfficeTarget("paragraph:1", "paragraph:1", "Old"),), has_unsaved_changes=False,
    )


def word_multi_paragraph_snapshot():
    return OfficeSnapshot(
        kind="word", expected_root=101, identity="word:101:C:/docs/Project.docx",
        name="Project.docx", rendered_text="[paragraph:1] value='Old'\n[paragraph:2] value='Last'",
        fingerprint="word-multi-fingerprint",
        targets=(
            OfficeTarget("paragraph:1", "paragraph:1", "Old"),
            OfficeTarget("paragraph:2", "paragraph:2", "Last"),
        ), has_unsaved_changes=False, paragraph_count=2,
    )


def word_trailing_empty_snapshot():
    snapshot = word_multi_paragraph_snapshot()
    return replace(snapshot, paragraph_count=3, targets=(
        OfficeTarget("paragraph:1", "paragraph:1", "Old"),
        OfficeTarget("paragraph:2", "paragraph:2", "Last"),
        OfficeTarget("table:1/1/1", "table:1/1/1", "Table tail"),
    ))


def excel_snapshot():
    return OfficeSnapshot(
        kind="excel", expected_root=202, identity="excel:202:C:/docs/Plan.xlsx",
        name="Plan.xlsx", rendered_text="", fingerprint="excel-fingerprint",
        targets=(
            OfficeTarget("Sheet1!A1", "Sheet1!A1", "Month"),
            OfficeTarget("Budget!C3", "Budget!C3", 120, "=SUM(C1:C2)"),
        ), has_unsaved_changes=False,
    )


class OfficePlanParsingTests(unittest.TestCase):
    def test_parses_valid_word_plan(self):
        from office_edits import WordTextEdit, parse_office_plan

        raw = json.dumps({
            "answer": "I prepared one replacement.",
            "plan": {"kind": "word", "snapshot_fingerprint": "word-fingerprint", "edits": [
                {"type": "word_replace_text", "locator": "paragraph:1",
                 "expected_value": "Old", "value": "New"},
            ]},
        })

        answer, plan = parse_office_plan(word_snapshot(), raw)

        self.assertEqual(answer, "I prepared one replacement.")
        self.assertIsInstance(plan.edits[0], WordTextEdit)
        self.assertEqual(plan.edits[0].value, "New")

    def test_parses_cross_sheet_excel_value_and_formula_plans(self):
        from office_edits import ExcelCellEdit, parse_office_plan

        raw = json.dumps({
            "answer": "I prepared two changes.",
            "plan": {"kind": "excel", "snapshot_fingerprint": "excel-fingerprint", "edits": [
                {"type": "excel_set_cell", "sheet": "Sheet1", "address": "A1",
                 "expected_value": "Month", "expected_formula": None, "value": "Month name", "formula": None},
                {"type": "excel_set_cell", "sheet": "Budget", "address": "C3",
                 "expected_value": 120, "expected_formula": "=SUM(C1:C2)", "value": None,
                 "formula": "=SUM(C1:C3)"},
            ]},
        })

        _, plan = parse_office_plan(excel_snapshot(), raw)

        self.assertEqual([edit.locator for edit in plan.edits], ["Sheet1!A1", "Budget!C3"])
        self.assertTrue(all(isinstance(edit, ExcelCellEdit) for edit in plan.edits))
        self.assertEqual(plan.edits[1].formula, "=SUM(C1:C3)")

    def test_rejects_malformed_or_non_executable_envelopes(self):
        from office_edits import OfficePlanError, parse_office_plan

        with self.assertRaisesRegex(OfficePlanError, "valid JSON"):
            parse_office_plan(word_snapshot(), "```json {} ```")
        with self.assertRaisesRegex(OfficePlanError, "non-empty"):
            parse_office_plan(word_snapshot(), json.dumps({"answer": "", "plan": None}))

    def test_rejects_wrong_snapshot_duplicate_unknown_and_mismatched_content(self):
        from office_edits import OfficePlanError, parse_office_plan

        cases = [
            {"kind": "word", "snapshot_fingerprint": "wrong", "edits": []},
            {"kind": "word", "snapshot_fingerprint": "word-fingerprint", "edits": [
                {"type": "word_replace_text", "locator": "paragraph:2", "expected_value": "Old", "value": "New"},
            ]},
            {"kind": "word", "snapshot_fingerprint": "word-fingerprint", "edits": [
                {"type": "word_replace_text", "locator": "paragraph:1", "expected_value": "Changed", "value": "New"},
            ]},
            {"kind": "word", "snapshot_fingerprint": "word-fingerprint", "edits": [
                {"type": "word_replace_text", "locator": "paragraph:1", "expected_value": "Old", "value": "New"},
                {"type": "word_replace_text", "locator": "paragraph:1", "expected_value": "Old", "value": "Again"},
            ]},
        ]
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(OfficePlanError):
                parse_office_plan(word_snapshot(), json.dumps({"answer": "x", "plan": plan}))

    def test_rejects_excel_edit_with_both_value_and_formula(self):
        from office_edits import OfficePlanError, parse_office_plan

        raw = json.dumps({"answer": "x", "plan": {"kind": "excel", "snapshot_fingerprint": "excel-fingerprint", "edits": [
            {"type": "excel_set_cell", "sheet": "Sheet1", "address": "A1", "expected_value": "Month",
             "expected_formula": None, "value": "Text", "formula": "=A2"},
        ]}})
        with self.assertRaisesRegex(OfficePlanError, "exactly one"):
            parse_office_plan(excel_snapshot(), raw)

    def test_parses_explicit_word_insert_after_anchor(self):
        from office_edits import WordTextEdit, parse_office_plan

        snapshot = word_multi_paragraph_snapshot()
        raw = json.dumps({
            "answer": "I prepared a new paragraph.",
            "plan": {"kind": "word", "snapshot_fingerprint": snapshot.fingerprint, "edits": [
                {"type": "word_insert_paragraph_after", "locator": "paragraph:2",
                 "expected_value": "Last", "value": "Summary"},
            ]},
        })

        _, plan = parse_office_plan(snapshot, raw)

        self.assertEqual(plan.edits,
                         (WordTextEdit("paragraph:2", "Last", "Summary", "insert_paragraph_after"),))

    def test_accepts_last_nonempty_body_paragraph_before_trailing_empty_or_table_content(self):
        from office_edits import WordTextEdit, parse_office_plan

        snapshot = word_trailing_empty_snapshot()
        raw = json.dumps({
            "answer": "I prepared a new paragraph.",
            "plan": {"kind": "word", "snapshot_fingerprint": snapshot.fingerprint, "edits": [
                {"type": "word_insert_paragraph_after", "locator": "paragraph:2",
                 "expected_value": "Last", "value": "Summary"},
            ]},
        })

        _, plan = parse_office_plan(snapshot, raw)

        self.assertEqual(plan.edits,
                         (WordTextEdit("paragraph:2", "Last", "Summary", "insert_paragraph_after"),))

    def test_rejects_insert_after_an_earlier_body_paragraph(self):
        from office_edits import OfficePlanError, parse_office_plan

        snapshot = word_trailing_empty_snapshot()
        raw = json.dumps({
            "answer": "x",
            "plan": {"kind": "word", "snapshot_fingerprint": snapshot.fingerprint, "edits": [
                {"type": "word_insert_paragraph_after", "locator": "paragraph:1",
                 "expected_value": "Old", "value": "Summary"},
            ]},
        })

        with self.assertRaisesRegex(OfficePlanError, "last non-empty body paragraph"):
            parse_office_plan(snapshot, raw)

    def test_repairs_legacy_missing_end_paragraph_plan_to_bounded_insert(self):
        from office_edits import WordTextEdit, parse_office_plan

        snapshot = word_multi_paragraph_snapshot()
        raw = json.dumps({
            "answer": "I prepared a new paragraph.",
            "plan": {"kind": "word", "snapshot_fingerprint": snapshot.fingerprint, "edits": [
                {"type": "word_replace_text", "locator": "paragraph:3",
                 "expected_value": "", "value": "Summary"},
            ]},
        })

        _, plan = parse_office_plan(snapshot, raw)

        self.assertEqual(plan.edits,
                         (WordTextEdit("paragraph:2", "Last", "Summary", "insert_paragraph_after"),))

    def test_keeps_an_existing_trailing_empty_paragraph_as_a_direct_replacement(self):
        from office_edits import WordTextEdit, parse_office_plan

        snapshot = word_trailing_empty_snapshot()
        raw = json.dumps({
            "answer": "I prepared a new paragraph.",
            "plan": {"kind": "word", "snapshot_fingerprint": snapshot.fingerprint, "edits": [
                {"type": "word_replace_text", "locator": "paragraph:3",
                 "expected_value": "", "value": "Summary"},
            ]},
        })

        _, plan = parse_office_plan(snapshot, raw)

        self.assertEqual(plan.edits,
                         (WordTextEdit("paragraph:3", "", "Summary"),))

    def test_rejects_a_missing_paragraph_beyond_the_document_end(self):
        from office_edits import OfficePlanError, parse_office_plan

        snapshot = word_multi_paragraph_snapshot()
        raw = json.dumps({
            "answer": "x",
            "plan": {"kind": "word", "snapshot_fingerprint": snapshot.fingerprint, "edits": [
                {"type": "word_replace_text", "locator": "paragraph:4",
                 "expected_value": "", "value": "Summary"},
            ]},
        })

        with self.assertRaisesRegex(OfficePlanError, "not present"):
            parse_office_plan(snapshot, raw)

    def test_does_not_repair_legacy_end_insert_with_nonempty_expected_value(self):
        from office_edits import OfficePlanError, parse_office_plan

        snapshot = word_multi_paragraph_snapshot()
        raw = json.dumps({
            "answer": "x",
            "plan": {"kind": "word", "snapshot_fingerprint": snapshot.fingerprint, "edits": [
                {"type": "word_replace_text", "locator": "paragraph:3",
                 "expected_value": "not empty", "value": "Summary"},
            ]},
        })

        with self.assertRaisesRegex(OfficePlanError, "not present"):
            parse_office_plan(snapshot, raw)


class OfficeHistoryTests(unittest.TestCase):
    def test_records_word_change_and_builds_inverse_plan(self):
        from office_edits import OfficeEditPlan, WordTextEdit, inverse_plan, record_from_plan

        snapshot = word_snapshot()
        plan = OfficeEditPlan("word", snapshot.fingerprint,
                              (WordTextEdit("paragraph:1", "Old", "New"),))
        record = record_from_plan(plan, snapshot, "op-1")
        current = replace(snapshot, fingerprint="after", targets=(OfficeTarget("paragraph:1", "paragraph:1", "New"),))

        inverse = inverse_plan(record, current)

        self.assertEqual(record.operation_id, "op-1")
        self.assertEqual((inverse.edits[0].expected_value, inverse.edits[0].value), ("New", "Old"))

    def test_inverts_excel_formula_and_value_without_losing_expected_state(self):
        from office_edits import ExcelCellEdit, OfficeEditPlan, inverse_plan, record_from_plan

        snapshot = excel_snapshot()
        plan = OfficeEditPlan("excel", snapshot.fingerprint, (
            ExcelCellEdit("Budget!C3", 120, "=SUM(C1:C2)", formula="=SUM(C1:C3)"),
            ExcelCellEdit("Sheet1!A1", "Month", "", value="Period"),
        ))
        record = record_from_plan(plan, snapshot, "op-2")
        current = replace(snapshot, fingerprint="after", targets=(
            OfficeTarget("Sheet1!A1", "Sheet1!A1", "Period", ""),
            OfficeTarget("Budget!C3", "Budget!C3", 120, "=SUM(C1:C3)"),
        ))

        inverse = inverse_plan(record, current)

        self.assertEqual(inverse.edits[0].formula, "=SUM(C1:C2)")
        self.assertIsNone(inverse.edits[0].value)
        self.assertEqual((inverse.edits[1].value, inverse.edits[1].formula), ("Month", None))

    def test_rejects_inverse_when_current_target_was_changed_again(self):
        from office_edits import OfficeEditPlan, OfficePlanError, WordTextEdit, inverse_plan, record_from_plan

        snapshot = word_snapshot()
        plan = OfficeEditPlan("word", snapshot.fingerprint,
                              (WordTextEdit("paragraph:1", "Old", "New"),))
        record = record_from_plan(plan, snapshot, "op-3")
        current = replace(snapshot, targets=(OfficeTarget("paragraph:1", "paragraph:1", "Someone else"),))

        with self.assertRaisesRegex(OfficePlanError, "changed"):
            inverse_plan(record, current)

    def test_inverse_allows_a_word_paragraph_that_became_empty(self):
        from office_edits import OfficeEditPlan, WordTextEdit, inverse_plan, record_from_plan

        snapshot = word_snapshot()
        plan = OfficeEditPlan("word", snapshot.fingerprint,
                              (WordTextEdit("paragraph:1", "Old", ""),))
        record = record_from_plan(plan, snapshot, "op-empty")
        current = replace(snapshot, fingerprint="after", targets=())

        inverse = inverse_plan(record, current)

        self.assertEqual(inverse.edits,
                         (WordTextEdit("paragraph:1", "", "Old"),))

    def test_live_validation_allows_an_empty_word_paragraph(self):
        from office_edits import OfficeEditPlan, WordTextEdit, _validate_live_targets

        current = replace(word_snapshot(), targets=())
        plan = OfficeEditPlan("word", current.fingerprint,
                              (WordTextEdit("paragraph:1", "", "Old"),))

        _validate_live_targets(current, plan)

    def test_inverse_of_inserted_word_paragraph_deletes_the_created_paragraph(self):
        from office_edits import OfficeEditPlan, WordTextEdit, inverse_plan, record_from_plan

        snapshot = word_multi_paragraph_snapshot()
        plan = OfficeEditPlan(snapshot.kind, snapshot.fingerprint, (
            WordTextEdit("paragraph:2", "Last", "Summary", "insert_paragraph_after"),
        ))
        record = record_from_plan(plan, snapshot, "op-insert")
        current = replace(snapshot, fingerprint="after", targets=(
            OfficeTarget("paragraph:1", "paragraph:1", "Old"),
            OfficeTarget("paragraph:2", "paragraph:2", "Last"),
            OfficeTarget("paragraph:3", "paragraph:3", "Summary"),
        ), paragraph_count=3)

        inverse = inverse_plan(record, current)

        self.assertEqual(inverse.edits,
                         (WordTextEdit("paragraph:3", "Summary", "", "delete_paragraph"),))


class OfficePlanApplyTests(unittest.TestCase):
    def test_word_write_canonicalizes_embedded_line_breaks(self):
        from office_edits import OfficeEditPlan, WordTextEdit, _write_word_text

        source_range = Mock()
        writable = Mock()
        writable.Start = 10
        writable.End = 20
        source_range.Duplicate = writable
        edit = WordTextEdit("paragraph:1", "Old", "Old\nSummary")

        with patch("office_edits._resolve_word_range", return_value=source_range):
            _write_word_text(Mock(), edit)

        self.assertEqual(writable.Text, "\vSummary")

    def test_word_insert_after_writes_a_new_paragraph_before_the_anchor_marker(self):
        from office_edits import WordTextEdit, _write_word_text

        source_range = Mock()
        writable = Mock()
        writable.Start = 10
        writable.End = 20
        source_range.Duplicate = writable
        edit = WordTextEdit("paragraph:2", "Last", "Summary", "insert_paragraph_after")

        with patch("office_edits._resolve_word_range", return_value=source_range):
            _write_word_text(Mock(), edit)

        self.assertEqual(writable.End, 19)
        self.assertEqual(writable.Text, "\rSummary")

    def test_word_write_copies_nearest_font_to_inserted_range(self):
        from office_edits import _copy_word_format

        source = Mock()
        target = Mock()
        source.Font.Name = "等线"
        source.Font.Size = 12
        source.Font.Color = 0x112233
        source.Font.Bold = True
        source.Font.Italic = False

        _copy_word_format(source, target)

        self.assertEqual(target.Font.Name, "等线")
        self.assertEqual(target.Font.Size, 12)
        self.assertEqual(target.Font.Color, 0x112233)
        self.assertTrue(target.Font.Bold)
        self.assertFalse(target.Font.Italic)

    def test_word_focus_selects_changed_range_without_paragraph_marker(self):
        from office_edits import _focus_word_target

        target = Mock()
        target.Start = 10
        target.End = 20
        duplicate = Mock()
        duplicate.Start = 10
        duplicate.End = 20
        target.Duplicate = duplicate

        with patch("office_edits._resolve_word_range", return_value=target):
            self.assertTrue(_focus_word_target(Mock(), "paragraph:1"))

        self.assertEqual(duplicate.End, 19)
        duplicate.Select.assert_called_once_with()

    def test_excel_focus_selects_verified_cell(self):
        from office_edits import _focus_excel_cell

        cell = Mock()
        with patch("office_edits._resolve_excel_cell", return_value=cell):
            self.assertTrue(_focus_excel_cell(Mock(), "Sheet1!A1"))
        cell.Select.assert_called_once_with()

    def test_excel_write_keeps_existing_cell_format(self):
        from office_edits import ExcelCellEdit, _write_excel_cell

        cell = Mock()
        cell.NumberFormat = "@"
        cell.Font.Name = "等线"
        cell.Interior.Color = 123
        edit = ExcelCellEdit("Sheet1!A1", "Old", "", value="New")

        with patch("office_edits._resolve_excel_cell", return_value=cell):
            _write_excel_cell(Mock(), edit)

        self.assertEqual(cell.Value2, "New")
        cell.ClearFormats.assert_not_called()

    def test_excel_selection_failure_returns_fallback_status(self):
        from office_edits import ExcelCellEdit, OfficeEditPlan, _verify_targets

        plan = OfficeEditPlan(
            "excel", "excel-fingerprint",
            (ExcelCellEdit("Sheet1!A1", "Old", "", value="New"),),
        )
        cell = Mock(Value2="New", Formula="")

        with patch("office_edits._resolve_excel_cell", return_value=cell), \
                patch("office_edits._focus_excel_cell", return_value=False), \
                patch("office_edits._with_active_document") as with_document:
            with_document.side_effect = lambda kind, root, fingerprint, callback: callback(Mock())
            self.assertFalse(_verify_targets("excel", 202, plan))

    def test_word_verification_normalizes_word_paragraph_breaks(self):
        from office_edits import OfficeEditPlan, WordTextEdit, _verify_targets

        plan = OfficeEditPlan(
            "word", "word-fingerprint",
            (WordTextEdit("paragraph:3", "Old", "Summary\n\nSecond line"),),
        )
        word_range = Mock()
        word_range.Text = "Summary\rSecond line\r"

        with patch("office_edits._resolve_word_range", return_value=word_range), \
                patch("office_edits._with_active_document") as with_document:
            with_document.side_effect = lambda kind, root, fingerprint, callback: callback(Mock())
            _verify_targets("word", 101, plan)

    def test_word_insert_verification_reads_and_selects_the_created_paragraph(self):
        from office_edits import OfficeEditPlan, WordTextEdit, _verify_targets

        plan = OfficeEditPlan(
            "word", "word-fingerprint",
            (WordTextEdit("paragraph:2", "Last", "Summary", "insert_paragraph_after"),),
        )
        word_range = Mock()
        word_range.Text = "Summary\r"
        duplicate = Mock()
        duplicate.End = 20
        word_range.Duplicate = duplicate

        with patch("office_edits._resolve_word_range", return_value=word_range), \
                patch("office_edits._with_active_document") as with_document:
            with_document.side_effect = lambda kind, root, fingerprint, callback: callback(Mock())
            self.assertTrue(_verify_targets("word", 101, plan))

    def test_post_write_verification_does_not_require_prewrite_fingerprint(self):
        from office_edits import _with_active_document

        snapshot_after_write = replace(word_snapshot(), fingerprint="after-write")
        pythoncom = Mock()
        client = Mock()
        application = Mock()
        application.ActiveWindow.Hwnd = 101
        application.ActiveDocument = Mock()
        client.GetActiveObject.return_value = application
        visited = []

        with patch("office_edits.read_active_office_snapshot", return_value=snapshot_after_write), \
                patch("office_edits._load_com_modules", return_value=(pythoncom, client)), \
                patch("office_edits.root_window", side_effect=lambda hwnd: hwnd):
            _with_active_document("word", 101, None, visited.append)

        self.assertEqual(visited, [application.ActiveDocument])

    def test_changed_snapshot_prevents_all_writes(self):
        from office_edits import OfficeEditPlan, WordTextEdit, OfficePlanError, apply_office_plan

        snapshot = word_snapshot()
        plan = OfficeEditPlan("word", snapshot.fingerprint, (WordTextEdit("paragraph:1", "Old", "New"),))
        changed = replace(snapshot, fingerprint="changed-fingerprint")
        with patch("office_edits.read_active_office_snapshot", return_value=changed), \
                patch("office_edits._apply_targets") as apply_targets:
            with self.assertRaisesRegex(OfficePlanError, "changed"):
                apply_office_plan(snapshot, plan)
        apply_targets.assert_not_called()

    def test_apply_writes_then_verifies_without_saving(self):
        from office_edits import OfficeEditPlan, WordTextEdit, apply_office_plan

        snapshot = word_snapshot()
        plan = OfficeEditPlan("word", snapshot.fingerprint, (WordTextEdit("paragraph:1", "Old", "New"),))
        with patch("office_edits.read_active_office_snapshot", return_value=snapshot), \
                patch("office_edits._apply_targets") as apply_targets, \
                patch("office_edits._verify_targets") as verify_targets:
            result = apply_office_plan(snapshot, plan)

        self.assertEqual((result.applied, result.verified), (1, 1))
        self.assertIn("not saved", result.message)
        apply_targets.assert_called_once_with("word", snapshot.expected_root, plan)
        verify_targets.assert_called_once_with("word", snapshot.expected_root, plan)

    def test_apply_reports_preview_fallback_when_selection_fails(self):
        from office_edits import OfficeEditPlan, WordTextEdit, apply_office_plan

        snapshot = word_snapshot()
        plan = OfficeEditPlan("word", snapshot.fingerprint,
                              (WordTextEdit("paragraph:1", "Old", "New"),))
        with patch("office_edits.read_active_office_snapshot", return_value=snapshot), \
                patch("office_edits._apply_targets"), \
                patch("office_edits._verify_targets", return_value=False):
            result = apply_office_plan(snapshot, plan)

        self.assertIn("preview as the change marker", result.message)


if __name__ == "__main__":
    unittest.main()
