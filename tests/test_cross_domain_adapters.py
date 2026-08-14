import tempfile
import unittest
from pathlib import Path

from cross_domain_adapters import CrossDomainAdapters


class FakeRuntime:
    def __init__(self, root):
        self.working_dir = Path(root)
        self.calls = []

    def _run_local_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "filesystem_write":
            path = self.working_dir / str(arguments["path"])
            path.write_text(str(arguments["text"]), encoding="utf-8")
            return {"ok": True, "verified": True}
        if name == "filesystem_read_text":
            path = self.working_dir / str(arguments["path"])
            return {"ok": True, "text": path.read_text(encoding="utf-8")}
        return {"ok": False, "failure_kind": "fixture_missing"}


class CrossDomainAdapterTests(unittest.TestCase):
    def test_file_handoff_requires_verified_structured_fields_and_readback(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = FakeRuntime(root)
            adapter = CrossDomainAdapters(runtime)
            result = adapter.write_file(
                {"title": "Candidate", "source": "Official docs", "price": "128"},
                "result.txt",
                evidence_verified=True,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["postcondition_kind"], "file_readback")
            self.assertEqual([item[0] for item in runtime.calls], [
                "filesystem_write", "filesystem_read_text",
            ])
            self.assertNotIn("Candidate", str(result))

    def test_unverified_page_data_cannot_open_the_file_stage(self):
        with tempfile.TemporaryDirectory() as root:
            result = CrossDomainAdapters(FakeRuntime(root)).write_file(
                {"title": "Candidate"}, "result.txt", evidence_verified=False,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "cross_domain_evidence_required")


if __name__ == "__main__":
    unittest.main()
