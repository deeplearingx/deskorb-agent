import unittest
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from browser_actions import validate_browser_action_batch
from browser_evidence import (
    BrowserEvidenceLedger, extract_list_from_snapshot, normalize_capability_status,
    normalize_evidence_fields, observed_link_urls, parse_github_relative_date, parse_github_star_count,
)
from browser_runtime import BrowserExecutionSession, PlaywrightMCPBackend
from browser_task_spec import BrowserTaskSpec
from complex_browser_acceptance import (
    SCENARIOS, _login_flow_verified, _normalized_run, _status_for_failure, build_acceptance_prompt,
    evaluate_acceptance, main, validate_run_record,
)


class ActionProtocolTests(unittest.TestCase):
    def test_complex_actions_keep_one_state_action_at_the_end(self):
        actions, error = validate_browser_action_batch([
            {"action": "snapshot", "arguments": {}},
            {"action": "find_text", "arguments": {
                "query": "Trending", "observation_id": "obs-1",
            }},
            {"action": "scroll", "arguments": {
                "direction": "down", "observation_id": "obs-1",
            }},
        ])
        self.assertIsNone(error)
        self.assertEqual([item.action for item in actions], ["snapshot", "find_text", "scroll"])

    def test_invalid_regex_is_not_forwarded_to_find_text(self):
        actions, error = validate_browser_action_batch([{
            "action": "find_text", "arguments": {
                "query": ".*Trending", "observation_id": "obs-1",
            },
        }])
        self.assertEqual(actions, [])
        self.assertIn("plain text", error)

    def test_cross_origin_navigation_must_use_an_observed_link(self):
        page = [{"type": "text", "text": """### Page
- Page URL: https://source.example/
- link [ref=external]: "External"
  - /url: https://target.example/docs
"""}]
        self.assertEqual(observed_link_urls(page), ("https://target.example/docs",))

    def test_press_key_adapter_does_not_send_semantic_ref(self):
        class Bridge:
            def __init__(self):
                self.payload = None

            def call(self, name, payload):
                self.payload = (name, payload)
                return {"ok": True}

        bridge = Bridge()
        result = PlaywrightMCPBackend(bridge).call("press_key", {
            "ref": "search", "key": "Enter",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(bridge.payload, ("mcp_playwright_browser_press_key", {"key": "Enter"}))


class EvidenceTests(unittest.TestCase):
    def test_extract_list_is_bounded_deduplicated_and_link_attested(self):
        content = [{"type": "text", "text": """- Page URL: https://github.com/search
### Snapshot
- article [ref=card-1]:
  - heading 'LangGraph' [ref=title-1]
  - generic [ref=stars-1]: 'Stars: 12k'
  - link [ref=repo-1]: 'repository'
    - /url: https://github.com/langchain-ai/langgraph
- article [ref=card-2]:
  - heading 'LangGraph' [ref=title-2]
  - generic [ref=stars-2]: 'Stars: 12k'
  - link [ref=repo-2]: 'repository'
    - /url: https://github.com/langchain-ai/langgraph
"""}]
        result = extract_list_from_snapshot(content, ["title", "stars", "url"], limit=20,
                                             page_url="https://github.com/search", observation_id="obs-1")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["fields"]["url"], "https://github.com/langchain-ai/langgraph")
        self.assertEqual(result["items"][0]["observation_id"], "obs-1")

    def test_ledger_is_bounded_and_reports_hash_only_for_supporting_text(self):
        ledger = BrowserEvidenceLedger(max_records=2)
        for index in range(3):
            ledger.add(kind="item", tab_id="tab-1", observation_id=f"obs-{index}",
                       source_url="https://example.com/path?secret=value",
                       fields={"title": f"Item {index}"}, supporting_text="private page text")
        result = ledger.safe_dict()
        self.assertEqual(result["count"], 2)
        self.assertNotIn("private page text", str(result))
        self.assertNotIn("secret=value", str(result))

    def test_capability_status_never_turns_missing_evidence_into_no(self):
        self.assertEqual(normalize_capability_status("支持 MCP"), "yes")
        self.assertEqual(normalize_capability_status("不支持 MCP"), "no")
        self.assertEqual(normalize_capability_status("页面没有明确说明"), "unknown")

    def test_github_numeric_and_relative_date_fields_are_normalized(self):
        self.assertEqual(parse_github_star_count("12.4k"), 12400)
        self.assertEqual(parse_github_star_count("1,203"), 1203)
        now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
        self.assertEqual(parse_github_relative_date("2 days ago", now=now), "2026-08-12")
        self.assertEqual(parse_github_relative_date("on Aug 14, 2026", now=now), "2026-08-14")
        self.assertEqual(normalize_evidence_fields({"stars": "12k", "mcp": "not mentioned"}), {
            "stars": "12000", "mcp": "unknown",
        })


class BrowserLifecycleTests(unittest.TestCase):
    def test_confirmed_login_rebinds_to_fresh_observation_and_is_single_use(self):
        login_page = [{"type": "text", "text": """- Page URL: https://example.com/home
### Snapshot
- button [ref=login]: \"Log in\"
"""}]
        after_click = [{"type": "text", "text": """- Page URL: https://example.com/login
### Snapshot
- heading [ref=login-page]: \"Sign in\"
"""}]

        class Backend:
            def __init__(self):
                self.calls = []
                self.snapshots = [login_page, login_page, after_click]

            def call(self, action, arguments):
                self.calls.append((action, dict(arguments)))
                if action == "snapshot":
                    return {"ok": True, "content": self.snapshots.pop(0)}
                return {"ok": True, "content": [{"type": "text", "text": "clicked"}]}

        backend = Backend()
        session = BrowserExecutionSession(backend)
        first = session.execute([{"action": "snapshot", "arguments": {}}])
        old_actions = [{"action": "click_ref", "arguments": {
            "ref": "login", "observation_id": first["observation_id"],
        }}]
        descriptor = session.confirmation_descriptor(old_actions)
        refreshed = session.execute([{"action": "snapshot", "arguments": {}}])
        self.assertFalse(session.confirmation_matches(old_actions, descriptor))
        self.assertTrue(session.confirmation_target_matches(old_actions, descriptor))
        new_actions = [{"action": "click_ref", "arguments": {
            "ref": "login", "observation_id": refreshed["observation_id"],
        }}]
        session.authorize_high_risk_once(session.confirmation_descriptor(new_actions))
        result = session.execute(new_actions)
        self.assertTrue(result["ok"], result)
        self.assertEqual([item[0] for item in backend.calls], ["snapshot", "snapshot", "click_ref", "snapshot"])
        replay = session.execute(new_actions)
        self.assertEqual(replay["failure_kind"], "stale_browser_observation")

    def test_tab_refs_are_opaque_and_invalidated_after_topology_change(self):
        class Backend:
            def __init__(self):
                self.calls = []
                self.snapshots = [[{"type": "text", "text": "- Page URL: https://example.com/\n### Snapshot\n- link [ref=repo]: \"Repo\"\n  - /url: https://github.com/example/repo"}]]

            def call(self, action, arguments):
                self.calls.append((action, dict(arguments)))
                if action == "snapshot":
                    fallback = [{"type": "text", "text": "- Page URL: https://github.com/example/repo\n### Snapshot\n- heading [ref=repo-page]: \"Repo\""}]
                    return {"ok": True, "content": self.snapshots.pop(0) if self.snapshots else fallback}
                if action == "list_tabs":
                    return {"ok": True, "content": [{"type": "text", "text": "- 0: (current) [Home](https://example.com/)\n- 1: [Repo](https://github.com/example/repo)"}]}
                return {"ok": True, "content": [{"type": "text", "text": action}]}

        backend = Backend()
        session = BrowserExecutionSession(backend)
        observed = session.execute([{"action": "snapshot", "arguments": {}}])
        listed = session.execute([{"action": "list_tabs", "arguments": {
            "observation_id": observed["observation_id"],
        }}])
        tab = listed["tabs"][1]
        opened = session.execute([{"action": "open_ref_new_tab", "arguments": {
            "ref": "repo", "observation_id": observed["observation_id"],
        }}])
        self.assertTrue(opened["ok"], opened)
        stale = session.execute([{"action": "switch_tab", "arguments": {
            "tab_snapshot_id": listed["tab_snapshot_id"], "tab_ref": tab["tab_ref"],
            "observation_id": opened["observation_id"],
        }}])
        self.assertEqual(stale["failure_kind"], "browser_tab_reference_invalid")


class BrowserTaskSpecTests(unittest.TestCase):
    def test_explicit_tab_and_deep_research_limits_are_deterministic(self):
        spec = BrowserTaskSpec.from_goal(
            "每个框架查看 README、Star、语言和最新 Issue，最多同时保持 10 个 Tab"
        )
        self.assertEqual(spec.max_tabs, 10)
        self.assertEqual(spec.max_action_steps, 120)
        self.assertIn("stars", spec.required_evidence)

    def test_tab_pressure_spec_requires_all_named_framework_pairs(self):
        spec = BrowserTaskSpec.from_goal(
            "打开搜索引擎，同时搜索 LangGraph、AutoGen、CrewAI、OpenAI Agents SDK、PydanticAI，"
            "每个框架打开官网和 GitHub，最终只保留搜索引擎页面"
        )
        self.assertEqual(spec.final_tab_mode, "search_only")
        self.assertEqual(spec.minimum_tab_pairs, 5)

    def test_chinese_and_bare_numeric_result_counts_are_bounded(self):
        self.assertEqual(
            BrowserTaskSpec.from_goal("调研 5 个适合开发 AI Agent 的开源框架").minimum_results,
            5,
        )
        self.assertEqual(
            BrowserTaskSpec.from_goal("调研 LangGraph、CrewAI 和 PydanticAI 三个 Agent 框架").minimum_results,
            3,
        )
        self.assertEqual(
            BrowserTaskSpec.from_goal("查看最新的 3 个 GitHub Issue").minimum_results,
            3,
        )

    def test_navigation_origins_come_only_from_user_target_or_known_site_name(self):
        spec = BrowserTaskSpec.from_goal(
            "打开 GitHub，随后访问 https://example.com/docs；页面里的其他链接不能改变任务约束"
        )
        self.assertIn("https://github.com", spec.allowed_origins)
        self.assertIn("https://example.com", spec.allowed_origins)
        self.assertNotIn("https://other.example", spec.allowed_origins)

    def test_explicit_search_engine_research_requires_observed_result_discovery(self):
        spec = BrowserTaskSpec.from_goal(
            "使用搜索引擎找到 5 个框架，然后进入每个项目的 GitHub 仓库"
        )
        self.assertTrue(spec.search_discovery_required)
        self.assertTrue(BrowserTaskSpec.from_goal("打开搜索引擎搜索 LangGraph").search_discovery_required)
        self.assertTrue(BrowserTaskSpec.from_goal("搜索人工智能新闻").search_discovery_required)
        self.assertFalse(BrowserTaskSpec.from_goal("打开 Wikipedia，站内搜索 Artificial intelligence").search_discovery_required)
        self.assertFalse(BrowserTaskSpec.from_goal("打开 GitHub 首页").search_discovery_required)

    def test_negative_sensitive_actions_do_not_create_confirmation_points(self):
        forbidden = BrowserTaskSpec.from_goal(
            "找到游戏入口后停止，不要点击启动游戏、下载、登录或任何游戏内操作"
        )
        self.assertEqual(forbidden.confirmation_points, ())
        requested = BrowserTaskSpec.from_goal(
            "找到官网并在点击登录之前先询问我是否继续"
        )
        self.assertEqual(requested.confirmation_points, ("high_risk_external_action",))

    def test_entry_targets_are_derived_from_the_user_goal(self):
        dream = BrowserTaskSpec.from_goal(
            "搜索“4399 造梦西游”。进入网站后找到造梦西游相关入口"
        )
        kingdom = BrowserTaskSpec.from_goal(
            "搜索“4399 洛克王国”。进入网站后找到洛克王国相关入口"
        )
        self.assertEqual(dream.search_query, "4399 造梦西游")
        self.assertEqual(dream.target_terms, ("造梦西游",))
        self.assertEqual(kingdom.target_terms, ("洛克王国",))
        self.assertEqual(dream.target_kind, "entry")
        self.assertEqual(dream.max_action_steps, 50)

    def test_search_query_stops_before_follow_up_action_after_chinese_comma(self):
        spec = BrowserTaskSpec.from_goal("搜索4399，打开它")

        self.assertEqual(spec.search_query, "4399")
        # 4399 is a site/search term rather than an entry phrase; it should
        # not be promoted to a separate target constraint.
        self.assertEqual(spec.target_terms, ())
        self.assertTrue(spec.search_discovery_required)

    def test_find_and_open_target_is_bound_to_the_original_task(self):
        spec = BrowserTaskSpec.from_goal(
            "在浏览器里，点4399，找到并打开造梦西游"
        )

        self.assertEqual(spec.target_kind, "entry")
        self.assertEqual(spec.target_terms, ("造梦西游",))
        self.assertEqual(spec.max_action_steps, 50)


class ComplexAcceptanceReportTests(unittest.TestCase):
    @staticmethod
    def _run(case_id: str, attempt: int) -> dict:
        scenario = SCENARIOS[case_id]
        required = tuple(scenario.get("required_fields") or ())
        records: list[dict] = []
        if case_id == "case-09-github-trending":
            records = [
                {"title": f"Trending project {index}",
                 "url": f"https://github.com/example/trending-{index}"}
                for index in range(1, 6)
            ]
        elif case_id == "case-12-long-research":
            records = [
                {
                    "title": f"Framework {index}",
                    "url": f"https://github.com/example/framework-{index}",
                    "stars": str(1000 + index), "language": "Python",
                    "updated_at": "2026-08-14", "installation": "pip install framework",
                    "mcp": "unknown" if index == 1 else "yes", "memory": "no",
                    "multi_agent": "unknown", "tool_calling": "yes",
                }
                for index in range(1, 6)
            ]
        elif case_id == "case-15-batch-pressure":
            records = [
                {"installation": "pip install langgraph"},
                {"issue_title": "Issue 1"},
                {"issue_title": "Issue 2"},
            ]
        elif case_id == "case-17-boss":
            records = [
                {
                    "title": title, "url": f"https://github.com/example/{slug}",
                    "stars": "1200", "updated_at": "2026-08-14",
                    "installation": "pip install framework", "mcp": "unknown",
                    "memory": "yes", "multi_agent": "no", "tool_calling": "yes",
                    "issue_title": "Issue 1; Issue 2",
                }
                for title, slug in (("LangGraph", "langgraph"),
                                    ("CrewAI", "crewai"),
                                    ("PydanticAI", "pydantic-ai"))
            ]
        fields = {
            str(key) for record in records for key, value in record.items()
            if str(value).strip()
        }
        ledger_records = [
            {
                "kind": (
                    "github_page_extract"
                    if case_id in {"case-12-long-research", "case-17-boss"}
                    else "list_item"
                ), "tab_id": "tab-1",
                "observation_id": f"obs-{index}",
                "source_url": (
                    "https://github.com/trending/python"
                    if case_id == "case-09-github-trending"
                    else str(record.get("url") or "https://github.com/example/repository")
                ),
                "fields": record,
                "supporting_text_hash": "a" * 24,
            }
            for index, record in enumerate(records, start=1)
        ]
        if case_id == "case-17-boss":
            final_count = 3
            hosts = ["github.com"] * 3
        elif case_id == "case-13-tab-pressure":
            final_count = 1
            hosts = ["bing.com"]
        else:
            final_count = 1
            hosts = ["github.com"]
        return {
            "run_id": f"{case_id}-run-{attempt}", "case_id": case_id, "attempt": attempt,
            "status": "completed_verified", "failure_kind": None,
            "terminal": "completed", "verified": True, "completed_verified": True,
            "real_site": True, "state_violations": 0,
            "forbidden_actions": 0, "login_before_confirmation": False,
            "login_click_after_confirmation": case_id == "case-16-confirm-login",
            "login_flow_verified": case_id == "case-16-confirm-login",
            "return_after_rejection": case_id == "case-16-reject-login",
            "max_tabs_seen": int(scenario.get("max_tabs") or 6),
            "final_tab_count": final_count, "final_tab_hosts": hosts,
            "tab_postflight_verified": True,
            "evidence_records": len(records), "evidence_fields": sorted(fields),
            "evidence_ledger": {
                "max_records": 60, "count": len(ledger_records), "records": ledger_records,
            },
        }

    def test_twenty_one_real_run_gate_accepts_only_verified_runs(self):
        runs = [self._run(case_id, attempt)
                for case_id in SCENARIOS
                for attempt in range(1, 4)]
        report = evaluate_acceptance(runs)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["requested_runs"], 21)

    def test_parallel_runner_collects_isolated_runs_and_finalizes_checkpoint(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_run(case_id, attempt, *, working_dir, interactive_human=False):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return _normalized_run(
                case_id, attempt, status="blocked_external",
                failure_kind="provider_timeout_after_tools", started=time.monotonic(),
                events=[], calls=[], real_site=True,
                metrics={
                    "run_id": f"fake-{attempt}", "prompt_sha256": "a" * 64,
                    "model": "test", "model_provider": "test", "mcp_version": "test",
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "acceptance.json"
            markdown = Path(directory) / "acceptance.md"
            with patch("complex_browser_acceptance.run_case", side_effect=fake_run):
                exit_code = main([
                    "--live", "--case", "case-09-github-trending",
                    "--repetitions", "3", "--workers", "3",
                    "--output", str(output), "--markdown-output", str(markdown),
                ])
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 2)  # one-case probe is not the 21-run gate
        self.assertEqual(payload["received_runs"], 3)
        self.assertFalse(payload["partial"])
        self.assertEqual(len(payload["runs"]), 3)
        self.assertGreaterEqual(max_active, 2)

    def test_all_external_blocks_do_not_make_business_gate_green(self):
        runs = []
        for case_id in SCENARIOS:
            for attempt in range(1, 4):
                item = self._run(case_id, attempt)
                item["status"] = "blocked_external"
                item["terminal"] = "blocked"
                item["verified"] = False
                item["completed_verified"] = False
                runs.append(item)
        report = evaluate_acceptance(runs)
        self.assertFalse(report["ok"])

    def test_invalid_environment_requires_a_rerun(self):
        runs = [self._run(case_id, attempt)
                for case_id in SCENARIOS
                for attempt in range(1, 4)]
        item = runs[0]
        item.update(status="invalid_environment", terminal="", verified=False,
                    completed_verified=False, real_site=False)
        report = evaluate_acceptance(runs)
        self.assertFalse(report["ok"])
        self.assertEqual(report["invalid_environment_runs"], 1)

    def test_case_attempt_and_run_id_identity_is_strict(self):
        for mutation in (
            lambda runs: runs[1].update(attempt=1),
            lambda runs: runs[1].update(run_id=runs[0]["run_id"]),
            lambda runs: runs[0].update(case_id="case-unknown"),
            lambda runs: runs[0].update(attempt=4),
        ):
            with self.subTest(mutation=mutation):
                runs = [self._run(case_id, attempt)
                        for case_id in SCENARIOS
                        for attempt in range(1, 4)]
                mutation(runs)
                with self.assertRaises(ValueError):
                    evaluate_acceptance(runs)

    def test_completed_status_requires_completed_verified_terminal_state(self):
        run = self._run("case-16-confirm-login", 1)
        run.update(terminal="failed", verified=False, completed_verified=False)
        with self.assertRaises(ValueError):
            validate_run_record(run)

    def test_confirmed_login_normalization_has_a_verified_terminal_state(self):
        run = _normalized_run(
            "case-16-confirm-login", 1,
            status="completed_verified", failure_kind=None, started=time.monotonic(),
            events=[], calls=[], real_site=True,
            metrics={"login_click_after_confirmation": True, "login_flow_verified": True},
        )
        validate_run_record(run)
        self.assertEqual(run["terminal"], "completed")
        self.assertTrue(run["verified"])

    def test_official_platform_login_url_is_real_login_flow_evidence(self):
        runtime = SimpleNamespace(_browser_session=SimpleNamespace(
            _current_page_url="https://platform.openai.com/login",
            _last_snapshot="",
        ))
        self.assertTrue(_login_flow_verified(runtime))

    def test_waiting_human_is_not_collapsed_into_external_block(self):
        self.assertEqual(_status_for_failure("waiting_human"), "waiting_human")
        self.assertEqual(_status_for_failure("human_verification_required"), "waiting_human")
        self.assertEqual(_status_for_failure("provider_error_after_tools"), "blocked_external")
        self.assertEqual(_status_for_failure("provider_error_before_tools"), "invalid_environment")
        runs = [self._run(case_id, attempt)
                for case_id in SCENARIOS
                for attempt in range(1, 4)]
        for index in (0, 1):
            runs[index].update(status="waiting_human", terminal="waiting_human",
                               verified=False, completed_verified=False)
        report = evaluate_acceptance(runs)
        summary = report["case_summary"]["case-09-github-trending"]
        self.assertFalse(report["ok"])
        self.assertEqual(summary["completed_verified"], 1)
        self.assertEqual(summary["external_blocks"], 0)
        self.assertEqual(summary["waiting_human"], 2)

    def test_required_fields_are_checked_per_record_not_as_a_union(self):
        runs = [self._run(case_id, attempt)
                for case_id in SCENARIOS
                for attempt in range(1, 4)]
        items = [run for run in runs if run["case_id"] == "case-12-long-research"][:2]
        for item in items:
            item["evidence_ledger"]["records"][0]["fields"].pop("memory")
        report = evaluate_acceptance(runs)
        self.assertFalse(report["ok"])

    def test_issue_rows_are_supplemental_to_composite_framework_records(self):
        runs = [self._run(case_id, attempt)
                for case_id in SCENARIOS
                for attempt in range(1, 4)]
        item = next(run for run in runs
                    if run["case_id"] == "case-17-boss" and run["attempt"] == 1)
        item["evidence_ledger"]["records"].extend([
            {
                "kind": "list_item", "tab_id": "tab-issues", "observation_id": "obs-issues",
                "source_url": "https://github.com/example/langgraph/issues",
                "fields": {"issue_title": "Issue 1", "url": "https://github.com/example/langgraph/issues/1"},
                "supporting_text_hash": "b" * 24,
            },
            {
                "kind": "list_item", "tab_id": "tab-issues", "observation_id": "obs-issues",
                "source_url": "https://github.com/example/langgraph/issues",
                "fields": {"issue_title": "Issue 2", "url": "https://github.com/example/langgraph/issues/2"},
                "supporting_text_hash": "c" * 24,
            },
        ])
        item["evidence_ledger"]["count"] = len(item["evidence_ledger"]["records"])
        item["evidence_records"] = item["evidence_ledger"]["count"]

        report = evaluate_acceptance(runs)

        self.assertTrue(report["ok"], report)

    def test_duplicate_or_empty_evidence_cannot_satisfy_record_count(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                runs = [self._run(case_id, attempt)
                        for case_id in SCENARIOS
                        for attempt in range(1, 4)]
                items = [run for run in runs if run["case_id"] == "case-09-github-trending"][:2]
                for item in items:
                    if empty:
                        item["evidence_ledger"]["records"][0]["fields"] = {}
                    else:
                        item["evidence_ledger"]["records"][1]["fields"]["url"] = \
                            item["evidence_ledger"]["records"][0]["fields"]["url"]
                report = evaluate_acceptance(runs)
                self.assertFalse(report["ok"])

    def test_final_tab_hosts_are_exact_for_search_and_github_gates(self):
        cases = (
            ("case-13-tab-pressure", ["evil-search.example"]),
            ("case-17-boss", ["evilgithub.com"] * 3),
        )
        for case_id, hosts in cases:
            with self.subTest(case_id=case_id):
                runs = [self._run(case, attempt)
                        for case in SCENARIOS
                        for attempt in range(1, 4)]
                for item in [run for run in runs if run["case_id"] == case_id][:2]:
                    item["final_tab_hosts"] = hosts
                report = evaluate_acceptance(runs)
                self.assertFalse(report["ok"])

    def test_unknown_capability_is_an_explicit_value_not_a_positive_claim(self):
        item = self._run("case-12-long-research", 1)
        capability_values = [
            record["fields"]["mcp"]
            for record in item["evidence_ledger"]["records"]
        ]
        self.assertIn("unknown", capability_values)
        self.assertNotIn("yes", [value for value in capability_values if value == "unknown"])
        runs = [self._run(case_id, attempt)
                for case_id in SCENARIOS
                for attempt in range(1, 4)]
        self.assertTrue(evaluate_acceptance(runs)["ok"])

    def test_nested_private_evidence_values_are_rejected_without_echoing_them(self):
        private_values = (
            ("url", "https://example.com/result?token=do-not-leak#fragment"),
            ("notes", "Authorization: Bearer do-not-leak"),
            ("supporting_text", "Full page text that must never enter the report."),
        )
        for field, value in private_values:
            with self.subTest(field=field):
                run = self._run("case-16-confirm-login", 1)
                run["evidence_ledger"] = {
                    "max_records": 60, "count": 1,
                    "records": [{"fields": {field: value}}],
                }
                with self.assertRaises(ValueError) as error:
                    validate_run_record(run)
                self.assertNotIn(value, str(error.exception))

    def test_acceptance_prompt_protocol_augmentation_is_deterministic(self):
        prompt = build_acceptance_prompt("Goal")
        self.assertEqual(prompt, build_acceptance_prompt("Goal"))
        for clause in (
            "navigate is a one-item batch",
            "state-changing action is the last item",
            "state action returns a fresh observation",
            "do not place snapshot after navigate in the same batch",
            "use extract_list and verify",
        ):
            self.assertIn(clause.casefold(), prompt.casefold())
        self.assertNotIn("api_key", prompt)

    def test_case_prompt_contract_requires_structured_trending_verification(self):
        prompt = build_acceptance_prompt("Goal", case_id="case-09-github-trending")
        self.assertIn('fields ["title", "url"]', prompt)
        self.assertIn('min_items 5', prompt)
        self.assertIn("Do not claim completion unless that verification passes", prompt)

    def test_batch_prompt_contract_requires_fresh_refs_after_github_navigation(self):
        prompt = build_acceptance_prompt("Goal", case_id="case-15-batch-pressure")
        self.assertIn("fresh snapshot before using find_text, extract, or any ref", prompt)
        self.assertIn("never reuse a ref", prompt)
        self.assertIn("do not extract again on the readme", prompt.casefold())
        self.assertIn("same-origin `/issues` URL", prompt)

    def test_login_prompt_contract_requires_find_and_high_risk_pause(self):
        prompt = build_acceptance_prompt("Goal", case_id="case-16-confirm-login")
        self.assertIn("find_text", prompt)
        self.assertIn("do not call extract_list or verify", prompt.casefold())
        self.assertIn("independent high-risk confirmation", prompt)
        self.assertIn("click_ref as a one-item batch", prompt)
        self.assertIn("find_text.matched_refs", prompt)

    def test_private_report_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_run_record({"status": "completed_verified", "prompt": "secret"})


if __name__ == "__main__":
    unittest.main()
