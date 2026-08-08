import unittest
from unittest.mock import Mock, patch


class WordSourceTests(unittest.TestCase):
    def test_resolves_word_application_that_owns_expected_window(self):
        from word_sources import resolve_word_application_for_window

        class Window:
            def __init__(self, hwnd):
                self.Hwnd = hwnd

        class Application:
            def __init__(self, hwnd):
                self.Windows = [Window(hwnd)]

        wrong = Application(202)
        right = Application(101)

        class Moniker:
            def __init__(self, value):
                self.value = value

            def GetDisplayName(self, _context, _reserved):
                return "!{000209FF-0000-0000-C000-000000000046}"

            def BindToObject(self, _context, _reserved, _iid):
                return self.value

        class Enum:
            def Next(self, _count):
                return (Moniker(wrong), Moniker(right))

        class Rot:
            def EnumRunning(self):
                return Enum()

        class PythonCom:
            IID_IDispatch = "IDispatch"

            @staticmethod
            def GetRunningObjectTable():
                return Rot()

            @staticmethod
            def CreateBindCtx(_reserved):
                return object()

        class Client:
            @staticmethod
            def Dispatch(value):
                return value

        resolved = resolve_word_application_for_window(PythonCom, Client, 101)
        self.assertIs(resolved, right)

    def test_reads_document_when_application_active_window_is_unavailable(self):
        from word_sources import read_active_word_document

        document = Mock()
        document.Name = "Background.docx"
        document.Saved = True
        document.Content.Text = "Readable Word content\r"

        class Application:
            ActiveDocument = document

            @property
            def ActiveWindow(self):
                raise RuntimeError("ActiveWindow is temporarily unavailable")

        pythoncom = Mock()
        client = Mock()
        client.GetActiveObject.return_value = Application()

        with patch("word_sources.is_word_window", return_value=True), \
                patch("word_sources.root_window", side_effect=lambda hwnd: hwnd), \
                patch("word_sources._load_com_modules", return_value=(pythoncom, client)):
            material = read_active_word_document(101)

        self.assertEqual(material.name, "Background.docx")
        self.assertEqual(material.text, "Readable Word content")

    def test_reads_expected_document_and_normalizes_word_text(self):
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

    def test_rejects_document_when_com_active_window_does_not_match(self):
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

    def test_rejects_empty_document(self):
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

        pythoncom.CoUninitialize.assert_called_once_with()

    def test_reports_missing_pywin32(self):
        from word_sources import WordMaterialError, read_active_word_document

        with patch("word_sources.is_word_window", return_value=True), \
                patch("word_sources._load_com_modules", side_effect=WordMaterialError("Word support is unavailable; install pywin32 and restart.")), \
                self.assertRaisesRegex(WordMaterialError, "pywin32"):
            read_active_word_document(101)


if __name__ == "__main__":
    unittest.main()
