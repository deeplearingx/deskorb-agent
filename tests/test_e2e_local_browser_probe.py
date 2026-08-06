import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import e2e_local_browser_probe as probe


class BrowserProbeMatrixTests(unittest.TestCase):
    def test_repetition_matrix_aggregates_without_answer_bodies(self):
        class FakeRuntime:
            def __init__(self, *_args, **_kwargs):
                self.mcp = None

        def fake_case(case_id, _base_url, _runtime, _events, *, include_answer=True):
            return {
                "id": case_id, "ok": True, "approval_used": True,
                "mcp_tool_calls": 1, "missing_evidence": [],
                "terminal": "completed", "verified": True,
                "verification_results": [{"passed": True}],
                "answer": "secret page body" if include_answer else None,
                "elapsed_ms": 20,
            }

        output = io.StringIO()
        with patch.object(probe, "AgentRuntime", FakeRuntime), \
             patch.object(probe, "run_case", side_effect=fake_case), \
             redirect_stdout(output):
            self.assertEqual(probe.main(["--repetitions", "2"]), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["repetitions"], 2)
        self.assertEqual([item["runs"] for item in payload["cases"]], [2, 2])
        self.assertEqual([item["passed"] for item in payload["cases"]], [2, 2])
        self.assertIsNone(payload["runs"][0]["answer"])

    def test_single_run_keeps_legacy_case_shape(self):
        class FakeRuntime:
            def __init__(self, *_args, **_kwargs):
                self.mcp = None

        def fake_case(case_id, _base_url, _runtime, _events, *, include_answer=True):
            return {"id": case_id, "ok": True, "answer": "safe fixture answer", "elapsed_ms": 10}

        output = io.StringIO()
        with patch.object(probe, "AgentRuntime", FakeRuntime), \
             patch.object(probe, "run_case", side_effect=fake_case), \
             redirect_stdout(output):
            self.assertEqual(probe.main(["--repetitions", "1"]), 0)
        payload = json.loads(output.getvalue())
        self.assertIsInstance(payload["cases"], list)
        self.assertNotIn("runs", payload)
        self.assertEqual(payload["cases"][0]["answer"], "safe fixture answer")


if __name__ == "__main__":
    unittest.main()
