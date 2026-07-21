import tempfile
import unittest
from pathlib import Path

from workflow_sources import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_TEXT_CHARS,
    MaterialError,
    extract_file_materials,
)


class WorkflowSourceTests(unittest.TestCase):
    def _write(self, directory, name, content):
        path = Path(directory, name)
        path.write_text(content, encoding="utf-8")
        return path

    def test_extracts_txt_and_markdown_with_safe_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            agenda = self._write(directory, "agenda.txt", "Agenda: launch review")
            notes = self._write(directory, "notes.md", "# Notes\nDecision: ship")

            materials = extract_file_materials([agenda, notes])

        self.assertEqual([item.name for item in materials], ["agenda.txt", "notes.md"])
        self.assertEqual(materials[0].text, "Agenda: launch review")
        self.assertEqual(materials[1].text, "# Notes\nDecision: ship")
        self.assertEqual(
            [item.source_summary() for item in materials],
            [{"kind": "file", "name": "agenda.txt"}, {"kind": "file", "name": "notes.md"}],
        )

    def test_rejects_more_than_the_file_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            files = [self._write(directory, f"{index}.txt", "x") for index in range(MAX_FILES + 1)]

            with self.assertRaisesRegex(MaterialError, "at most"):
                extract_file_materials(files)

    def test_rejects_unsupported_files_without_reading_them(self):
        with tempfile.TemporaryDirectory() as directory:
            spreadsheet = self._write(directory, "actions.xlsx", "not a spreadsheet")

            with self.assertRaisesRegex(MaterialError, "Unsupported"):
                extract_file_materials([spreadsheet])

    def test_rejects_file_over_the_per_file_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            oversized = Path(directory, "oversized.txt")
            oversized.write_bytes(b"x" * (MAX_FILE_BYTES + 1))

            with self.assertRaisesRegex(MaterialError, "too large"):
                extract_file_materials([oversized])

    def test_rejects_extracted_text_beyond_the_aggregate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            long_text = self._write(directory, "long.txt", "a" * (MAX_TOTAL_TEXT_CHARS + 1))

            with self.assertRaisesRegex(MaterialError, "text limit"):
                extract_file_materials([long_text])

    def test_extracts_docx_paragraphs(self):
        from docx import Document

        with tempfile.TemporaryDirectory() as directory:
            document_path = Path(directory, "minutes.docx")
            document = Document()
            document.add_paragraph("Decision: publish the agenda.")
            document.save(document_path)

            materials = extract_file_materials([document_path])

        self.assertEqual(materials[0].text, "Decision: publish the agenda.")

    def test_rejects_a_pdf_with_no_extractable_text(self):
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory, "scan.pdf")
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with pdf_path.open("wb") as stream:
                writer.write(stream)

            with self.assertRaisesRegex(MaterialError, "No extractable text"):
                extract_file_materials([pdf_path])


if __name__ == "__main__":
    unittest.main()
