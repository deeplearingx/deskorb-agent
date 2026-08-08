import unittest
from unittest.mock import Mock, patch


class OfficeSourceTests(unittest.TestCase):
    def test_word_snapshot_uses_expected_window_when_active_window_is_unavailable(self):
        from office_sources import read_active_office_snapshot

        document = Mock()
        document.Name = "Background.docx"
        document.FullName = "C:/docs/Background.docx"
        document.ReadOnly = False
        document.ProtectionType = -1
        document.Saved = True
        body = Mock()
        body.Range.Text = "Readable Word content\r"
        body.Range.Tables.Count = 0
        document.Paragraphs = [body]
        document.Tables = []

        class Application:
            ActiveDocument = document

            @property
            def ActiveWindow(self):
                raise RuntimeError("ActiveWindow is temporarily unavailable")

        pythoncom = Mock()
        client = Mock()
        client.GetActiveObject.return_value = Application()

        with patch("office_sources.is_word_window", return_value=True), \
                patch("office_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("office_sources._load_com_modules", return_value=(pythoncom, client)):
            snapshot = read_active_office_snapshot("word", 101)

        self.assertEqual(snapshot.name, "Background.docx")
        self.assertEqual(snapshot.targets[0].value, "Readable Word content")

    def _word_application(self):
        body = Mock()
        body.Range.Text = "Project title\r"
        body.Range.Tables.Count = 0

        cell = Mock()
        cell.Range.Text = "Budget\r\x07"
        row = Mock()
        row.Cells = [cell]
        table = Mock()
        table.Rows = [row]

        document = Mock()
        document.Name = "Project.docx"
        document.FullName = "C:/docs/Project.docx"
        document.ReadOnly = False
        # Word COM uses wdNoProtection = -1 for an editable, unprotected document.
        document.ProtectionType = -1
        document.Saved = False
        document.Paragraphs = [body]
        document.Tables = [table]

        application = Mock()
        application.ActiveWindow.Hwnd = 101
        application.ActiveDocument = document
        return application

    def _excel_application(self):
        def cell(address, value, formula=""):
            result = Mock()
            result.Address.return_value = address
            result.Value2 = value
            result.Formula = formula
            return result

        first_range = Mock()
        first_range.Rows.Count = 1
        first_range.Columns.Count = 2
        first_range.Cells.side_effect = lambda row, column: {
            (1, 1): cell("$A$1", "Month"),
            (1, 2): cell("$B$1", 120, "=SUM(A1:A2)"),
        }[(row, column)]
        first_sheet = Mock()
        first_sheet.Name = "Sheet1"
        first_sheet.ProtectContents = False
        first_sheet.UsedRange = first_range

        second_range = Mock()
        second_range.Rows.Count = 1
        second_range.Columns.Count = 1
        second_range.Cells.side_effect = lambda row, column: cell("$C$3", 9)
        second_sheet = Mock()
        second_sheet.Name = "Budget"
        second_sheet.ProtectContents = False
        second_sheet.UsedRange = second_range

        workbook = Mock()
        workbook.Name = "Plan.xlsx"
        workbook.FullName = "C:/docs/Plan.xlsx"
        workbook.ReadOnly = False
        workbook.Saved = True
        workbook.Worksheets = [first_sheet, second_sheet]

        application = Mock()
        application.ActiveWindow.Hwnd = 202
        application.ActiveWorkbook = workbook
        return application

    def test_word_snapshot_enumerates_body_and_table_targets(self):
        from office_sources import read_active_office_snapshot

        pythoncom = Mock()
        client = Mock()
        client.GetActiveObject.return_value = self._word_application()

        with patch("office_sources.is_word_window", return_value=True), \
                patch("office_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("office_sources._load_com_modules", return_value=(pythoncom, client)):
            snapshot = read_active_office_snapshot("word", 101)

        self.assertEqual(snapshot.kind, "word")
        self.assertEqual(snapshot.name, "Project.docx")
        self.assertEqual([target.locator for target in snapshot.targets],
                         ["paragraph:1", "table:1/1/1"])
        self.assertEqual([target.value for target in snapshot.targets], ["Project title", "Budget"])
        self.assertEqual(snapshot.paragraph_count, 1)
        self.assertIn("paragraph:1", snapshot.rendered_text)
        self.assertTrue(snapshot.has_unsaved_changes)
        pythoncom.CoInitialize.assert_called_once_with()
        pythoncom.CoUninitialize.assert_called_once_with()

    def test_word_snapshot_keeps_body_numbering_when_empty_and_table_paragraphs_follow(self):
        from office_sources import read_active_office_snapshot

        application = self._word_application()
        second_body = Mock()
        second_body.Range.Text = "Last paragraph\r"
        second_body.Range.Tables.Count = 0
        trailing_empty = Mock()
        trailing_empty.Range.Text = "\r"
        trailing_empty.Range.Tables.Count = 0
        table_paragraph = Mock()
        table_paragraph.Range.Text = "\r"
        table_paragraph.Range.Tables.Count = 1
        application.ActiveDocument.Paragraphs = [
            application.ActiveDocument.Paragraphs[0], second_body, trailing_empty, table_paragraph,
        ]
        pythoncom = Mock()
        client = Mock()
        client.GetActiveObject.return_value = application

        with patch("office_sources.is_word_window", return_value=True), \
                patch("office_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("office_sources._load_com_modules", return_value=(pythoncom, client)):
            snapshot = read_active_office_snapshot("word", 101)

        self.assertEqual(snapshot.paragraph_count, 4)
        self.assertEqual([target.locator for target in snapshot.targets],
                         ["paragraph:1", "paragraph:2", "table:1/1/1"])

    def test_excel_snapshot_enumerates_every_worksheet_and_formula(self):
        from office_sources import read_active_office_snapshot

        pythoncom = Mock()
        client = Mock()
        client.GetActiveObject.return_value = self._excel_application()

        with patch("office_sources.is_excel_window", return_value=True), \
                patch("office_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("office_sources._load_com_modules", return_value=(pythoncom, client)):
            snapshot = read_active_office_snapshot("excel", 202)

        self.assertEqual(snapshot.kind, "excel")
        self.assertEqual([target.locator for target in snapshot.targets],
                         ["Sheet1!A1", "Sheet1!B1", "Budget!C3"])
        self.assertEqual(snapshot.targets[1].value, 120)
        self.assertEqual(snapshot.targets[1].formula, "=SUM(A1:A2)")
        self.assertIn("Budget!C3", snapshot.rendered_text)
        self.assertFalse(snapshot.has_unsaved_changes)

    def test_excel_snapshot_rejects_source_over_cell_limit(self):
        from office_sources import OfficeSourceError, read_active_office_snapshot

        pythoncom = Mock()
        client = Mock()
        client.GetActiveObject.return_value = self._excel_application()

        with patch("office_sources.is_excel_window", return_value=True), \
                patch("office_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("office_sources._load_com_modules", return_value=(pythoncom, client)), \
                patch("office_sources.OFFICE_MAX_NONEMPTY_CELLS", 2):
            with self.assertRaisesRegex(OfficeSourceError, "too large"):
                read_active_office_snapshot("excel", 202)

    def test_snapshot_rejects_changed_active_window(self):
        from office_sources import OfficeSourceError, read_active_office_snapshot

        pythoncom = Mock()
        client = Mock()
        app = self._word_application()
        app.ActiveWindow.Hwnd = 303
        client.GetActiveObject.return_value = app

        with patch("office_sources.is_word_window", return_value=True), \
                patch("office_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("office_sources._load_com_modules", return_value=(pythoncom, client)):
            with self.assertRaisesRegex(OfficeSourceError, "does not match"):
                read_active_office_snapshot("word", 101)

    def test_word_snapshot_rejects_read_only_document(self):
        from office_sources import OfficeSourceError, read_active_office_snapshot

        pythoncom = Mock()
        client = Mock()
        application = self._word_application()
        application.ActiveDocument.ReadOnly = True
        client.GetActiveObject.return_value = application

        with patch("office_sources.is_word_window", return_value=True), \
                patch("office_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("office_sources._load_com_modules", return_value=(pythoncom, client)):
            with self.assertRaisesRegex(OfficeSourceError, "read-only or protected"):
                read_active_office_snapshot("word", 101)

    def test_excel_snapshot_rejects_protected_worksheet(self):
        from office_sources import OfficeSourceError, read_active_office_snapshot

        pythoncom = Mock()
        client = Mock()
        application = self._excel_application()
        application.ActiveWorkbook.Worksheets[1].ProtectContents = True
        client.GetActiveObject.return_value = application

        with patch("office_sources.is_excel_window", return_value=True), \
                patch("office_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("office_sources._load_com_modules", return_value=(pythoncom, client)):
            with self.assertRaisesRegex(OfficeSourceError, "protected"):
                read_active_office_snapshot("excel", 202)


if __name__ == "__main__":
    unittest.main()
