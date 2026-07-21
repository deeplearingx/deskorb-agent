import unittest
from unittest.mock import Mock, patch


class WordSourceTests(unittest.TestCase):
    def test_reads_active_document_and_normalizes_word_control_characters(self):
        from word_sources import read_active_word_document

        document = Mock()
        document.Name = "Project brief.docx"
        document.Saved = False
        document.Content.Text = "Title\rFirst cell\r\x07Second cell\r\x07\x0cFinal paragraph\r"
        application = Mock()
        application.ActiveWindow.Hwnd = 101
        application.ActiveDocument = document
        pythoncom = Mock()
        client = Mock()
        client.GetActiveObject.return_value = application

        with patch("word_sources.is_word_window", return_value=True), \
                patch("word_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("word_sources._load_com_modules", return_value=(pythoncom, client)):
            material = read_active_word_document(101)

        self.assertEqual(material.document_id, "word-window:101")
        self.assertEqual(material.name, "Project brief.docx")
        self.assertTrue(material.has_unsaved_changes)
        self.assertEqual(material.text, "Title\nFirst cell\nSecond cell\nFinal paragraph")
        self.assertEqual(material.character_count, len(material.text))
        pythoncom.CoInitialize.assert_called_once_with()
        pythoncom.CoUninitialize.assert_called_once_with()

    def test_rejects_a_word_document_when_com_active_window_does_not_match(self):
        from word_sources import WordMaterialError, read_active_word_document

        application = Mock()
        application.ActiveWindow.Hwnd = 202
        pythoncom = Mock()
        client = Mock()
        client.GetActiveObject.return_value = application

        with patch("word_sources.is_word_window", return_value=True), \
                patch("word_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("word_sources._load_com_modules", return_value=(pythoncom, client)), \
                self.assertRaisesRegex(WordMaterialError, "does not match"):
            read_active_word_document(101)

        pythoncom.CoUninitialize.assert_called_once_with()

    def test_rejects_an_empty_document(self):
        from word_sources import WordMaterialError, read_active_word_document

        document = Mock()
        document.Name = "Empty.docx"
        document.Saved = True
        document.Content.Text = "\r\x07\x0c"
        application = Mock()
        application.ActiveWindow.Hwnd = 101
        application.ActiveDocument = document
        pythoncom = Mock()
        client = Mock()
        client.GetActiveObject.return_value = application

        with patch("word_sources.is_word_window", return_value=True), \
                patch("word_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("word_sources._load_com_modules", return_value=(pythoncom, client)), \
                self.assertRaisesRegex(WordMaterialError, "No readable text"):
            read_active_word_document(101)


if __name__ == "__main__":
    unittest.main()
