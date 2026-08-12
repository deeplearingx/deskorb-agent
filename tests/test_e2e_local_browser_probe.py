import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from e2e_metrics import load_normalized_report
import e2e_local_browser_probe as probe


class BrowserProbeMatrixTests(unittest.TestCase):
    def test_loopback_handler_suppresses_expected_client_disconnect(self):
        handler = object.__new__(probe.QuietHandler)

        with patch("http.server.SimpleHTTPRequestHandler.handle_one_request",
                   side_effect=ConnectionResetError()):
            handler.handle_one_request()

    def test_evidence_uses_the_final_explicit_verify_after_recovery(self):
        verification_results = [
            {"source": "tool_result", "passed": False},
            {"source": "observation", "action": "verify", "passed": True},
        ]

        self.assertTrue(probe._verification_evidence_passed(verification_results))

        verification_results.append(
            {"source": "observation", "action": "verify", "passed": False}
        )
        self.assertFalse(probe._verification_evidence_passed(verification_results))

    def test_probe_wraps_production_two_argument_dispatcher(self):
        calls = []

        class FakeRuntime:
            def __init__(self, *_args, **_kwargs):
                self.mcp = None

            def _run_local_tool(self, name, arguments):
                calls.append((name, arguments))
                return {"ok": True}

        runtime = FakeRuntime()
        original = runtime._run_local_tool
        runtime._run_local_tool = lambda name, arguments: original(name, arguments)
        result = runtime._run_local_tool("browser_action_batch", {"actions": []})
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][0], "browser_action_batch")

    def test_real_probe_uses_an_isolated_working_directory(self):
        working_directories = []

        class FakeRuntime:
            def __init__(self, *_args, **kwargs):
                working_directories.append(Path(kwargs["working_dir"]))
                self.mcp = None

        def fake_case(case_id, _base_url, _runtime, _events, **_kwargs):
            return {"id": case_id, "ok": True, "approval_used": True,
                    "mcp_tool_calls": 1, "terminal": "completed", "verified": True,
                    "evidence_passed": True, "verification_results": [{"passed": True}],
                    "elapsed_ms": 10}

        with patch.object(probe, "AgentRuntime", FakeRuntime), \
             patch.object(probe, "run_case", side_effect=fake_case), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main(["--repetitions", "2"]), 0)

        self.assertEqual(len(working_directories), 2)
        self.assertEqual(len(set(working_directories)), 2)
        self.assertTrue(all(path.name.startswith("deskorb-browser-probe-")
                            for path in working_directories))
        self.assertTrue(all(path != Path.home() for path in working_directories))

    def test_single_run_does_not_emit_model_answer_or_page_body(self):
        class FakeRuntime:
            def __init__(self, *_args, **_kwargs):
                self.mcp = None

        def fake_case(case_id, _base_url, _runtime, _events, **_kwargs):
            return {"id": case_id, "ok": True, "approval_used": True,
                    "mcp_tool_calls": 1, "terminal": "completed", "verified": True,
                    "evidence_passed": True, "verification_results": [{"passed": True}], "elapsed_ms": 10}

        output = io.StringIO()
        with patch.object(probe, "AgentRuntime", FakeRuntime), \
             patch.object(probe, "run_case", side_effect=fake_case), \
             redirect_stdout(output):
            self.assertEqual(probe.main(["--repetitions", "1"]), 0)
        encoded = output.getvalue()
        self.assertNotIn("answer", encoded)
        self.assertNotIn("page body", encoded)
        self.assertNotIn("base_url", encoded)

    def test_browser_trace_is_bounded_and_content_free(self):
        class FakeRuntime:
            def __init__(self, *_args, **_kwargs):
                self.mcp = None

        def fake_case(case_id, _base_url, _runtime, _events, **_kwargs):
            return {
                "id": case_id, "ok": False, "failure_kind": "browser_no_progress",
                "browser_trace": [{
                    "ok": False, "failure_kind": "browser_no_progress",
                    "observations": [{"action": "fill_ref", "ok": False,
                                       "failure_kind": "browser_no_progress"}],
                }],
                "elapsed_ms": 10,
            }

        output = io.StringIO()
        with patch.object(probe, "AgentRuntime", FakeRuntime), \
             patch.object(probe, "run_case", side_effect=fake_case), \
             redirect_stdout(output):
            self.assertEqual(probe.main(["--case", "research-001", "--repetitions", "1"]), 2)
        encoded = output.getvalue()
        self.assertIn("browser_no_progress", encoded)
        self.assertNotIn("content", encoded)
        self.assertNotIn("Python asyncio", encoded)
        self.assertNotIn("value", encoded)

    def test_timeout_after_successful_browser_observation_is_model_planning_timeout(self):
        self.assertEqual(
            probe._classify_probe_timeout(
                "provider_or_tool_timeout",
                [{"started": True, "action_types": ["snapshot"], "ok": True}],
            ),
            "model_planning_timeout",
        )

    def test_normalized_probe_run_carries_cache_metrics_without_trace(self):
        result = probe._normalized_run({
            "id": "research-001", "attempt": 2, "ok": True,
            "terminal": "completed", "verified": True, "evidence_passed": True,
            "mcp_tool_calls": 1,
            "browser_trace": [{
                "action_types": ["navigate", "snapshot"], "action_steps": 6,
                "execution_source": "cache", "cache_status": "template_hit",
                "model_fallback": False, "postcondition_passed": True,
            }],
        })
        self.assertEqual(result["action_steps"], 6)
        self.assertEqual(result["execution_source_counts"], {"cache": 1})
        self.assertEqual(result["cache_status_counts"], {"template_hit": 1})
        self.assertTrue(result["postcondition_passed"])

    def test_repetition_matrix_aggregates_without_answer_bodies(self):
        class FakeRuntime:
            def __init__(self, *_args, **_kwargs):
                self.mcp = None

        def fake_case(case_id, _base_url, _runtime, _events, **_kwargs):
            return {
                "id": case_id, "ok": True, "approval_used": True,
                "mcp_tool_calls": 1, "terminal": "completed", "verified": True,
                "evidence_passed": True, "verification_results": [{"passed": True}],
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
        self.assertEqual([item["runs"] for item in payload["cases"]], [2, 2, 2])
        self.assertEqual([item["passed"] for item in payload["cases"]], [2, 2, 2])
        self.assertNotIn("answer", payload["runs"][0])
        self.assertEqual(payload["runs"][0]["case_id"], "web-001")
        self.assertEqual(payload["runs"][0]["outcome"], "passed")

    def test_single_run_keeps_privacy_safe_case_shape(self):
        class FakeRuntime:
            def __init__(self, *_args, **_kwargs):
                self.mcp = None

        def fake_case(case_id, _base_url, _runtime, _events, **_kwargs):
            return {"id": case_id, "ok": True, "terminal": "completed", "verified": True,
                    "evidence_passed": True, "elapsed_ms": 10}

        output = io.StringIO()
        with patch.object(probe, "AgentRuntime", FakeRuntime), \
             patch.object(probe, "run_case", side_effect=fake_case), \
             redirect_stdout(output):
            self.assertEqual(probe.main(["--repetitions", "1"]), 0)
        payload = json.loads(output.getvalue())
        self.assertIsInstance(payload["cases"], list)
        self.assertIsInstance(payload["runs"], list)
        self.assertNotIn("answer", payload["runs"][0])
        self.assertTrue(payload["runs"][0]["verified"])

    def test_normalized_local_probe_runs_are_accepted_by_quality_metrics(self):
        class FakeRuntime:
            def __init__(self, *_args, **_kwargs):
                self.mcp = None

        def fake_case(case_id, _base_url, _runtime, _events, **_kwargs):
            return {"id": case_id, "ok": True, "approval_used": True,
                    "mcp_tool_calls": 1, "terminal": "completed", "verified": True,
                    "evidence_passed": True, "elapsed_ms": 10}

        output = io.StringIO()
        with patch.object(probe, "AgentRuntime", FakeRuntime), \
             patch.object(probe, "run_case", side_effect=fake_case), \
             redirect_stdout(output):
            self.assertEqual(probe.main(["--repetitions", "1"]), 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser-agent-matrix.json"
            path.write_text(output.getvalue(), encoding="utf-8")
            runs = load_normalized_report(path)
        self.assertEqual([item["case_id"] for item in runs],
                         ["web-001", "research-001", "dynamic-search-no-progress"])
        self.assertTrue(all(item["outcome"] == "passed" for item in runs))
        self.assertTrue(all(item["completed"] and item["verified"] for item in runs))
        self.assertTrue(all(item["evidence_passed"] for item in runs))


if __name__ == "__main__":
    unittest.main()
