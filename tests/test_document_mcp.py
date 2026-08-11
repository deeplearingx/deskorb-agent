import tempfile
import unittest
import json
import sys
from pathlib import Path

from document_mcp import DocumentAdapter, DocumentError
from mcp_client import MCPServerSpec, StdioMCPClient


class DocumentMcpTests(unittest.TestCase):
    def test_converts_a_text_fixture_with_bounded_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notes.txt"
            source.write_text("DeskOrb fixture\nsecond line", encoding="utf-8")

            result = DocumentAdapter((root,)).convert_file(source)

        self.assertTrue(result["ok"])
        self.assertEqual(result["format"], "txt")
        self.assertEqual(result["text"], "DeskOrb fixture\nsecond line")
        self.assertNotIn("path", result)

    def test_rejects_a_file_outside_the_configured_root(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            source = Path(outside) / "secret.txt"
            source.write_text("private", encoding="utf-8")
            with self.assertRaisesRegex(DocumentError, "outside"):
                DocumentAdapter((root,)).convert_file(source)

    def test_rejects_binary_documents_when_markitdown_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "report.pdf"
            source.write_bytes(b"%PDF-1.7 fixture")
            with self.assertRaisesRegex(DocumentError, "dependency"):
                DocumentAdapter((Path(directory),), markitdown_factory=lambda: None).convert_file(source)

    def test_stdio_server_lists_and_reads_without_a_console_or_raw_path_result(self):
        script = Path(__file__).resolve().parents[1] / "document_mcp.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "notes.txt"
            source.write_text("stdio fixture", encoding="utf-8")
            spec = MCPServerSpec(
                name="documents", command=sys.executable, args=(str(script),),
                env={"DESKORB_AGENT_DOCUMENT_ROOTS": str(root)}, cwd=str(root),
                allowed_tools=("document_status", "document_convert_file"),
                read_only_tools=("document_status", "document_convert_file"),
            )
            client = StdioMCPClient(spec, timeout_seconds=5)
            try:
                names = [item["name"] for item in client.list_tools()]
                result = client.call_tool("document_convert_file", {"path": str(source)})
            finally:
                client.close()
        self.assertIn("document_convert_file", names)
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertNotIn("path", payload)


if __name__ == "__main__":
    unittest.main()
