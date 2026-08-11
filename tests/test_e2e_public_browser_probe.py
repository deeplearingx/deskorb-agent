import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

import e2e_public_browser_probe as probe


class FakeRuntime:
    HUMAN_VERIFICATION_CONTINUE = "我已完成验证，继续"
    created = []

    def __init__(self, events, *_args, **kwargs):
        self.events = events
        self.mcp = None
        self.browser_backend = "isolated-playwright"
        self.working_dir = Path(kwargs["working_dir"])
        self.turns = []
        FakeRuntime.created.append(self)

    def _record_tool_result(self, _name, _arguments, _result):
        return None

    def _run_tool_with_recovery(self, name, arguments):
        return self._run_local_tool(name, arguments)

    def _run_local_tool(self, _name, _arguments):
        return {"ok": True}

    def _complete(self, *, fastapi: bool = False):
        fields = [
            {"title": "男士 T 恤 fixture result", "price": "¥129", "source": "fixture", "url": "https://item.taobao.com/item.htm?id=1"},
            {"title": "男士 T 恤 fixture result two", "price": "¥129", "source": "fixture", "url": "https://item.taobao.com/item.htm?id=2"},
            {"title": "男士 T 恤 fixture result three", "price": "¥129", "source": "fixture", "url": "https://item.taobao.com/item.htm?id=3"},
        ]
        if fastapi:
            fields = [
                {"title": "FastAPI 中文文档", "source": "FastAPI", "url": "https://fastapi.tiangolo.com/zh/one"},
                {"title": "FastAPI 教程", "source": "FastAPI", "url": "https://fastapi.tiangolo.com/zh/two"},
                {"title": "FastAPI 指南", "source": "FastAPI", "url": "https://fastapi.tiangolo.com/zh/three"},
            ]
        self._record_tool_result("browser_action_batch", {"actions": [{"action": "extract", "arguments": {"ref": "card-1", "fields": ["title", "price", "source", "url"]}}]}, {
            "ok": True,
            "observations": [{"action": "extract", "ref": f"card-{index}",
                              "extraction": {"ref": f"card-{index}", "trusted_ref": True, "fields": item}}
                             for index, item in enumerate(fields, start=1)],
        })
        self.events.put(("tool", ("MCP browser tool", {"name": "browser_extract"})))
        self.events.put(("task_progress", {"terminal": "completed", "verified": True}))

    def run_turn(self, text, _images):
        self.turns.append(text)
        if text.startswith("确认 "):
            self._complete(fastapi="FastAPI" in text)
        else:
            self.events.put(("approval", "Authorize task\n确认 ABCDEF"))


class ApprovedClickRuntime(FakeRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._browser_current_url = "https://www.bing.com/search?q=FastAPI"

    def run_turn(self, text, _images):
        self.turns.append(text)
        if text.startswith("确认 "):
            # Bing's contract requires evidence before a public result click.
            self._complete(fastapi=True)
            arguments = {"actions": [{"action": "click_ref", "arguments": {"ref": "result-1"}}]}
            result = self._run_tool_with_recovery("browser_action_batch", arguments)
            self._record_tool_result("browser_action_batch", arguments, result)
        else:
            self.events.put(("approval", "Authorize task\n确认 ABCDEF"))


class JournalCapturingRuntime(FakeRuntime):
    journals = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        JournalCapturingRuntime.journals.append(kwargs["task_journal"])


class HandoffRuntime(FakeRuntime):
    def run_turn(self, text, _images):
        self.turns.append(text)
        if text.startswith("确认 "):
            self.events.put(("human_verification", {"marker": "captcha"}))
            self.events.put(("task_progress", {"terminal": None, "verified": False, "waiting_human": True}))
        elif text == self.HUMAN_VERIFICATION_CONTINUE:
            self._complete(fastapi=True)
        else:
            self.events.put(("approval", "Authorize task\n确认 ABCDEF"))


class UntrustedExtractionRuntime(FakeRuntime):
    def run_turn(self, text, _images):
        self.turns.append(text)
        if text.startswith("确认 "):
            fields = [
                {"title": "男士 T 恤 一", "price": "¥129", "url": "https://item.taobao.com/item.htm?id=1"},
                {"title": "男士 T 恤 二", "price": "¥130", "url": "https://item.taobao.com/item.htm?id=2"},
                {"title": "男士 T 恤 三", "price": "¥131", "url": "https://item.taobao.com/item.htm?id=3"},
            ]
            self._record_tool_result("browser_action_batch", {
                "actions": [{"action": "extract", "arguments": {"ref": "card-1", "fields": ["title", "price", "url"]}}],
            }, {
                "ok": True,
                "observations": [{"action": "extract", "ref": f"card-{index}",
                                  "extraction": {"ref": f"card-{index}", "trusted_ref": False, "fields": item}}
                                 for index, item in enumerate(fields, start=1)],
            })
            self.events.put(("task_progress", {"terminal": "completed", "verified": True}))
        else:
            self.events.put(("approval", "Authorize task\n确认 ABCDEF"))


class StaleEvidenceHandoffRuntime(FakeRuntime):
    def run_turn(self, text, _images):
        self.turns.append(text)
        if text.startswith("确认 "):
            fields = [
                {"title": "FastAPI 中文文档", "source": "FastAPI", "url": "https://fastapi.tiangolo.com/zh/one"},
                {"title": "FastAPI 教程", "source": "FastAPI", "url": "https://fastapi.tiangolo.com/zh/two"},
                {"title": "FastAPI 指南", "source": "FastAPI", "url": "https://fastapi.tiangolo.com/zh/three"},
            ]
            self._record_tool_result("browser_action_batch", {
                "actions": [{"action": "extract", "arguments": {"ref": "card-1", "fields": ["title", "source", "url"]}}],
            }, {
                "ok": True,
                "observations": [{"action": "extract", "ref": f"card-{index}",
                                  "extraction": {"ref": f"card-{index}", "trusted_ref": True, "fields": item}}
                                 for index, item in enumerate(fields, start=1)],
            })
            self.events.put(("human_verification", {"marker": "captcha"}))
            self.events.put(("task_progress", {"terminal": None, "verified": False, "waiting_human": True}))
        elif text == self.HUMAN_VERIFICATION_CONTINUE:
            arguments = {"actions": [{"action": "snapshot", "arguments": {}}]}
            self._record_tool_result("browser_action_batch", arguments, {"ok": True, "observations": [{"action": "snapshot"}]})
            self.events.put(("task_progress", {"terminal": "completed", "verified": True}))
        else:
            self.events.put(("approval", "Authorize task\n确认 ABCDEF"))


class ProductionContinuationHandoffRuntime(HandoffRuntime):
    HUMAN_VERIFICATION_CONTINUE = probe.AgentRuntime.HUMAN_VERIFICATION_CONTINUE

    def run_turn(self, text, _images):
        self.turns.append(text)
        if text.startswith("确认 "):
            self.events.put(("human_verification", {"marker": "captcha"}))
            self.events.put(("task_progress", {"terminal": None, "verified": False, "waiting_human": True}))
        elif text in {self.HUMAN_VERIFICATION_CONTINUE, "我已完成验证，继续"}:
            self._complete(fastapi=True)
        else:
            self.events.put(("approval", "Authorize task\n确认 ABCDEF"))


class UnsafeRuntime(FakeRuntime):
    def run_turn(self, text, _images):
        self.turns.append(text)
        if text.startswith("确认 "):
            self._record_tool_result("browser_action_batch", {"actions": [{"action": "fill_ref", "arguments": {"ref": "e1", "value": "unsafe"}}]}, {"ok": True})
            self.events.put(("tool", ("MCP browser tool", {"name": "browser_type"})))
            self.events.put(("task_progress", {"terminal": "completed", "verified": True}))
        else:
            self.events.put(("approval", "Authorize task\n确认 ABCDEF"))


class GuardedUnsafeRuntime(FakeRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed_actions = []

    def _run_local_tool(self, name, arguments):
        if name == "browser_action_batch":
            self.executed_actions.extend(item["action"] for item in arguments["actions"])
        return {"ok": True}

    def run_turn(self, text, _images):
        self.turns.append(text)
        if text.startswith("确认 "):
            arguments = {"actions": [{"action": "fill_ref", "arguments": {"ref": "e1", "value": "unsafe"}}]}
            result = self._run_tool_with_recovery("browser_action_batch", arguments)
            self._record_tool_result("browser_action_batch", arguments, result)
            self.events.put(("task_progress", {"terminal": "completed", "verified": True}))
        else:
            self.events.put(("approval", "Authorize task\n确认 ABCDEF"))


class NonBrowserRuntime(FakeRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed_tools = []

    def _run_local_tool(self, name, _arguments):
        self.executed_tools.append(name)
        return {"ok": True}

    def run_turn(self, text, _images):
        self.turns.append(text)
        if text.startswith("确认 "):
            arguments = {"path": "outside-scope.txt", "content": "untrusted"}
            result = self._run_tool_with_recovery("filesystem_write", arguments)
            self._record_tool_result("filesystem_write", arguments, result)
            self.events.put(("task_progress", {"terminal": "completed", "verified": True}))
        else:
            self.events.put(("approval", "Authorize task\n确认 ABCDEF"))


class PublicBrowserProbeTests(unittest.TestCase):
    def setUp(self):
        FakeRuntime.created.clear()
        JournalCapturingRuntime.journals.clear()

    def test_timeout_failure_is_classified_by_whether_tools_ran(self):
        self.assertEqual(probe._turn_failure_kind("timeout", []), "provider_timeout_before_tools")
        self.assertEqual(probe._turn_failure_kind("timeout", [("browser_action_batch", {})]),
                         "provider_timeout_after_tools")
        self.assertEqual(probe._turn_failure_kind(
            "timeout", [("browser_action_batch", {})], ["evidence_required_before_click"]),
            "evidence_required_before_click")
        self.assertEqual(probe._turn_failure_kind("runtime_error", []), "runtime_error")

    def test_early_failure_report_preserves_safe_tool_phase_metrics(self):
        result = probe._result(
            probe.SCENARIOS["bing-fastapi"], 1, 0.0,
            calls=[("browser_action_batch", {
                "actions": [{"action": "snapshot", "arguments": {}}],
            })], failure_kind="provider_timeout_after_tools")
        self.assertEqual(result["tool_rounds"], 1)
        self.assertEqual(result["browser_action_kinds"], ["snapshot"])

    def test_batch_evidence_metrics_count_trusted_and_untrusted_attempts(self):
        extractions = []
        attempts, untrusted = probe._collect_batch_evidence({"observations": [
            {"action": "extract", "ref": "card-1", "extraction": {
                "trusted_ref": True, "fields": {"title": "one"},
            }},
            {"action": "extract", "ref": "card-2", "extraction": {
                "trusted_ref": False, "fields": {},
            }},
        ]}, extractions)
        self.assertEqual((attempts, untrusted), (2, 1))
        self.assertEqual(len(extractions), 1)

    def test_live_flag_is_required_before_runtime_is_created(self):
        output = io.StringIO()
        with patch.object(probe, "AgentRuntime", FakeRuntime), redirect_stdout(output):
            self.assertEqual(probe.main(["--scenario", "taobao-search"]), 2)
        self.assertEqual(FakeRuntime.created, [])
        self.assertEqual(json.loads(output.getvalue())["error_kind"], "live_opt_in_required")

    def test_completed_verified_read_only_case_emits_only_safe_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", FakeRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None):
                result = probe.run_case(probe.SCENARIOS["taobao-search"], working_dir=Path(directory))
        self.assertEqual(result["outcome"], "passed")
        self.assertTrue(result["completed"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["approval_used"])
        self.assertEqual(result["browser_action_kinds"], ["extract"])
        self.assertNotIn("answer", result)
        self.assertNotIn("prompt", result)
        self.assertNotIn("url", result)

    def test_public_probe_injects_a_nonpersistent_task_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", JournalCapturingRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None):
                probe.run_case(probe.SCENARIOS["taobao-search"], working_dir=Path(directory))
        journal = JournalCapturingRuntime.journals[-1]
        self.assertIsNone(journal.path)
        self.assertEqual(journal.recoverable(), [])

    def test_public_probe_allows_clicks_from_an_approved_public_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", ApprovedClickRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None):
                result = probe.run_case(probe.SCENARIOS["bing-fastapi"], working_dir=Path(directory))
        self.assertEqual(result["outcome"], "passed")
        self.assertIn("click_ref", result["browser_action_kinds"])
        self.assertTrue(result["safety_passed"])

    def test_untrusted_card_extraction_cannot_pass_public_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", UntrustedExtractionRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None):
                result = probe.run_case(probe.SCENARIOS["taobao-search"], working_dir=Path(directory))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["failure_kind"], "structured_evidence_incomplete")
        self.assertEqual(result["candidate_count"], 0)

    def test_handoff_requires_continue_then_fresh_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", HandoffRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None), \
                 patch("builtins.input", return_value="我已完成验证，继续"):
                result = probe.run_case(probe.SCENARIOS["bing-fastapi"], working_dir=Path(directory))
        self.assertEqual(result["outcome"], "passed")
        self.assertTrue(result["handoff_present"])
        self.assertTrue(result["handoff_resumed"])
        self.assertTrue(result["fresh_observation_after_handoff"])
        self.assertEqual(result["browser_action_kinds"], ["extract"])

    def test_handoff_does_not_accept_stale_pre_handoff_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", StaleEvidenceHandoffRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None), \
                 patch("builtins.input", return_value="我已完成验证，继续"):
                result = probe.run_case(probe.SCENARIOS["bing-fastapi"], working_dir=Path(directory))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["failure_kind"], "structured_evidence_incomplete")
        self.assertTrue(result["fresh_observation_after_handoff"])

    def test_handoff_accepts_the_documented_cli_continuation_phrase(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", ProductionContinuationHandoffRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None), \
                 patch("builtins.input", return_value="我已完成验证，继续"):
                result = probe.run_case(probe.SCENARIOS["bing-fastapi"], working_dir=Path(directory))
        self.assertEqual(result["outcome"], "passed")
        self.assertTrue(result["handoff_resumed"])
        self.assertTrue(result["fresh_observation_after_handoff"])

    def test_handoff_without_operator_resume_is_blocked_not_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", HandoffRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None):
                result = probe.run_case(probe.SCENARIOS["taobao-search"], working_dir=Path(directory),
                                        interactive_handoff=False)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["failure_kind"], "human_verification_required")
        self.assertFalse(result["completed"])

    def test_no_human_resume_flag_reports_handoff_as_blocked(self):
        output = io.StringIO()
        with patch.object(probe, "AgentRuntime", HandoffRuntime), \
             patch.object(probe, "API_MODEL", "fixture-model"), \
             patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
             patch.object(probe, "API_PROXY_URL", None), \
             redirect_stdout(output):
            self.assertEqual(probe.main(["--live", "--scenario", "taobao-search", "--no-human-resume"]), 2)
        record = json.loads(output.getvalue())["runs"][0]
        self.assertEqual(record["outcome"], "blocked")
        self.assertEqual(record["failure_kind"], "human_verification_required")

    def test_read_only_guard_rejects_navigation_outside_public_read_routes(self):
        failure_kind = probe._public_browser_policy_failure(
            None, probe.SCENARIOS["taobao-search"], "browser_action_batch", {
                "actions": [{"action": "navigate", "arguments": {"url": "https://login.taobao.com/member/login"}}],
            })
        self.assertEqual(failure_kind, "unapproved_navigation")

    def test_public_guard_allows_click_after_an_approved_navigation_in_same_batch(self):
        runtime = SimpleNamespace(_browser_current_url="", _public_candidate_count=3)
        failure_kind = probe._public_browser_policy_failure(
            runtime, probe.SCENARIOS["bing-fastapi"], "browser_action_batch", {
                "actions": [
                    {"action": "navigate", "arguments": {"url": "https://www.bing.com/search?q=FastAPI"}},
                    {"action": "click_ref", "arguments": {"ref": "result-1"}},
                ],
            })
        self.assertIsNone(failure_kind)

    def test_public_guard_rejects_bing_click_before_minimum_evidence(self):
        runtime = SimpleNamespace(_browser_current_url="https://www.bing.com/search?q=FastAPI",
                                 _public_candidate_count=0)
        failure_kind = probe._public_browser_policy_failure(
            runtime, probe.SCENARIOS["bing-fastapi"], "browser_action_batch", {
                "actions": [{"action": "click_ref", "arguments": {"ref": "result-1"}}],
            })
        self.assertEqual(failure_kind, "evidence_required_before_click")

    def test_public_guard_allows_bing_click_after_minimum_evidence(self):
        runtime = SimpleNamespace(_browser_current_url="https://www.bing.com/search?q=FastAPI",
                                 _public_candidate_count=3)
        failure_kind = probe._public_browser_policy_failure(
            runtime, probe.SCENARIOS["bing-fastapi"], "browser_action_batch", {
                "actions": [{"action": "click_ref", "arguments": {"ref": "result-1"}}],
            })
        self.assertIsNone(failure_kind)

    def test_public_guard_allows_bounded_tab_switching(self):
        runtime = SimpleNamespace(_browser_current_url="https://www.bing.com/search?q=FastAPI")
        failure_kind = probe._public_browser_policy_failure(
            runtime, probe.SCENARIOS["bing-fastapi"], "browser_action_batch", {
                "actions": [{"action": "switch_tab", "arguments": {"index": 1}}],
            })
        self.assertIsNone(failure_kind)

    def test_public_guard_recognizes_direct_playwright_tab_switch_tool(self):
        runtime = SimpleNamespace(_browser_current_url="https://www.bing.com/search?q=FastAPI", mcp=None)
        failure_kind = probe._public_browser_policy_failure(
            runtime, probe.SCENARIOS["bing-fastapi"], "mcp_playwright_browser_tabs", {
                "action": "select", "index": 1,
            })
        self.assertIsNone(failure_kind)

    def test_public_guard_rejects_click_from_an_unapproved_origin(self):
        runtime = SimpleNamespace(_browser_current_url="https://ads.example.test/redirect")
        failure_kind = probe._public_browser_policy_failure(
            runtime, probe.SCENARIOS["bing-fastapi"], "browser_action_batch", {
                "actions": [{"action": "click_ref", "arguments": {"ref": "ad-1"}}],
            })
        self.assertEqual(failure_kind, "unapproved_navigation")

    def test_public_guard_fails_closed_when_a_click_snapshot_reports_an_unapproved_url(self):
        class RedirectingRuntime:
            def __init__(self):
                self._browser_current_url = "https://www.bing.com/search?q=FastAPI"
                self._public_candidate_count = 3

            def _run_tool_with_recovery(self, _name, _arguments):
                return {"ok": True, "observations": [{
                    "action": "snapshot", "content": "- Page URL: https://ads.example.test/redirect\n",
                }]}

        runtime = RedirectingRuntime()
        probe._install_read_only_guard(runtime, probe.SCENARIOS["bing-fastapi"])
        result = runtime._run_tool_with_recovery(
            "browser_action_batch", {
                "actions": [{"action": "click_ref", "arguments": {"ref": "result-1"}}],
            })
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "unapproved_navigation")

    def test_read_only_guard_rejects_non_http_navigation(self):
        failure_kind = probe._public_browser_policy_failure(
            None, probe.SCENARIOS["taobao-search"], "browser_action_batch", {
                "actions": [{"action": "navigate", "arguments": {"url": "javascript:alert(1)"}}],
            })
        self.assertEqual(failure_kind, "unapproved_navigation")

    def test_candidate_evidence_rejects_urls_outside_the_allowed_sources(self):
        scenario = probe.SCENARIOS["taobao-search"]
        _candidates, _field_sets, _official, passed = probe._candidate_metrics(scenario, [{
            "__ref": "card-1", "title": "男士 T 恤 一", "price": "¥129", "url": "https://affiliate.example/item/1",
        }, {
            "__ref": "card-2", "title": "男士 T 恤 二", "price": "¥130", "url": "https://affiliate.example/item/2",
        }, {
            "__ref": "card-3", "title": "男士 T 恤 三", "price": "¥131", "url": "https://affiliate.example/item/3",
        }])
        self.assertFalse(passed)

    def test_taobao_evidence_requires_three_distinct_matching_product_cards(self):
        scenario = probe.SCENARIOS["taobao-search"]
        candidates, field_sets, _official, passed = probe._candidate_metrics(scenario, [{
            "__ref": "card-1", "title": "男士 T 恤", "price": "¥129", "url": "https://item.taobao.com/item.htm?id=1",
        }])
        self.assertEqual(candidates, 1)
        self.assertEqual(field_sets, 1)
        self.assertFalse(passed)

    def test_taobao_evidence_accepts_three_cards_when_one_price_is_in_range(self):
        scenario = probe.SCENARIOS["taobao-search"]
        candidates, _field_sets, _official, passed = probe._candidate_metrics(scenario, [{
            "__ref": "card-1", "title": "男士 T 恤 一", "price": "¥99", "url": "https://item.taobao.com/item.htm?id=1",
        }, {
            "__ref": "card-2", "title": "男士 T 恤 二", "price": "¥129", "url": "https://item.taobao.com/item.htm?id=2",
        }, {
            "__ref": "card-3", "title": "男士 T 恤 三", "price": "¥199", "url": "https://item.taobao.com/item.htm?id=3",
        }])
        self.assertEqual(candidates, 3)
        self.assertTrue(passed)

    def test_candidate_evidence_rejects_fields_without_a_card_reference(self):
        scenario = probe.SCENARIOS["taobao-search"]
        _candidates, _field_sets, _official, passed = probe._candidate_metrics(scenario, [{
            "title": "男士 T 恤", "price": "¥129", "url": "https://item.taobao.com/item.htm?id=1",
        }, {
            "title": "男士 T 恤", "price": "¥130", "url": "https://item.taobao.com/item.htm?id=2",
        }, {
            "title": "男士 T 恤", "price": "¥131", "url": "https://item.taobao.com/item.htm?id=3",
        }])
        self.assertFalse(passed)

    def test_taobao_evidence_rejects_title_that_misses_required_keyword(self):
        scenario = probe.SCENARIOS["taobao-search"]
        _candidates, _field_sets, _official, passed = probe._candidate_metrics(scenario, [{
            "__ref": "card-1", "title": "男士 衬衫", "price": "¥129", "url": "https://item.taobao.com/item.htm?id=1",
        }, {
            "__ref": "card-2", "title": "男士 衬衫", "price": "¥130", "url": "https://item.taobao.com/item.htm?id=2",
        }, {
            "__ref": "card-3", "title": "男士 衬衫", "price": "¥131", "url": "https://item.taobao.com/item.htm?id=3",
        }])
        self.assertFalse(passed)

    def test_non_read_only_browser_action_fails_the_acceptance_case(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", UnsafeRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None):
                result = probe.run_case(probe.SCENARIOS["bing-fastapi"], working_dir=Path(directory))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["failure_kind"], "forbidden_browser_action")
        self.assertFalse(result["safety_passed"])

    def test_read_only_guard_blocks_unsafe_action_before_runtime_executes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", GuardedUnsafeRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None):
                result = probe.run_case(probe.SCENARIOS["taobao-search"], working_dir=Path(directory))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["failure_kind"], "forbidden_browser_action")
        self.assertEqual(GuardedUnsafeRuntime.created[-1].executed_actions, [])

    def test_public_probe_blocks_non_browser_tools_before_they_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe, "AgentRuntime", NonBrowserRuntime), \
                 patch.object(probe, "API_MODEL", "fixture-model"), \
                 patch.object(probe, "API_BASE_URL", "https://example.test/v1"), \
                 patch.object(probe, "API_PROXY_URL", None):
                result = probe.run_case(probe.SCENARIOS["taobao-search"], working_dir=Path(directory))
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["failure_kind"], "forbidden_public_tool")
        self.assertEqual(NonBrowserRuntime.created[-1].executed_tools, [])


if __name__ == "__main__":
    unittest.main()
