import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from browser_cache import (
    BrowserActionCache,
    LocatorDescriptor,
    build_parameterized_search_template,
    parse_browser_task_intent,
    parse_snapshot_candidates,
    resolve_locator,
)


SEARCH_TASK = (
    "打开 http://127.0.0.1:8123/dynamic_search.html，在搜索框中输入 Python asyncio，"
    "点击名为‘Python asyncio 入门’的下拉选项，最后提取标题和来源。"
)


class BrowserActionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "browser-action-cache.sqlite3"
        self.cache = BrowserActionCache(self.path, key=b"test-cache-key-0123456789")
        self.intent = parse_browser_task_intent(SEARCH_TASK)
        self.assertIsNotNone(self.intent)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parameterized_template_round_trips_for_exact_and_changed_query(self):
        template = build_parameterized_search_template(
            fields=("title", "source"),
        )
        self.cache.record_success(self.intent, template)

        exact = self.cache.lookup(self.intent)
        changed = parse_browser_task_intent(
            SEARCH_TASK.replace("Python asyncio", "Python typing")
        )

        self.assertEqual(exact.status, "exact_hit")
        self.assertIsNotNone(changed)
        self.assertEqual(self.cache.lookup(changed).status, "template_hit")
        self.assertEqual(exact.entry.template["version"], 1)

    def test_cache_storage_contains_no_prompt_url_query_or_result_text(self):
        self.cache.record_success(
            self.intent,
            build_parameterized_search_template(fields=("title", "source")),
        )

        with closing(sqlite3.connect(self.path)) as connection:
            serialized = repr(connection.execute(
                "SELECT exact_key, template_key, template_json FROM browser_workflows"
            ).fetchall())

        self.assertNotIn("Python asyncio", serialized)
        self.assertNotIn("127.0.0.1", serialized)
        self.assertNotIn("dynamic_search", serialized)
        self.assertNotIn("官方文档", serialized)
        self.assertNotIn(SEARCH_TASK, serialized)

    def test_learned_locator_digest_is_allowed_but_plain_name_is_rejected(self):
        template = build_parameterized_search_template(fields=("title", "source"))
        template["steps"][2]["locator"]["name_digest"] = "a" * 64
        self.cache.record_success(self.intent, template)
        template["steps"][2]["locator"]["name_digest"] = "Python asyncio"
        with self.assertRaises(ValueError):
            self.cache.record_success(self.intent, template)

    def test_localhost_port_changes_share_the_parameterized_template_scope(self):
        first = parse_browser_task_intent(SEARCH_TASK, key=self.cache.key)
        second = parse_browser_task_intent(
            SEARCH_TASK.replace("8123", "49821"), key=self.cache.key
        )
        self.cache.record_success(
            first, build_parameterized_search_template(fields=first.fields)
        )

        self.assertEqual(self.cache.lookup(second).status, "template_hit")

    def test_url_parser_stops_before_adjacent_sentence_text(self):
        intent = parse_browser_task_intent(
            "打开 http://127.0.0.1:8123/dynamic_search.html。在搜索框中输入 Python asyncio，"
            "点击名为‘Python asyncio 入门’的下拉选项，最后提取标题和来源。"
        )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.start_url, "http://127.0.0.1:8123/dynamic_search.html")

    def test_ipv6_loopback_ports_share_the_parameterized_template_scope(self):
        first = parse_browser_task_intent(
            SEARCH_TASK.replace("127.0.0.1:8123", "[::1]:8123"), key=self.cache.key
        )
        second = parse_browser_task_intent(
            SEARCH_TASK.replace("127.0.0.1:8123", "[::1]:49821"), key=self.cache.key
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.cache.record_success(first, build_parameterized_search_template(fields=first.fields))
        self.assertEqual(self.cache.lookup(second).status, "template_hit")

    def test_failed_entry_is_disabled_before_it_can_be_replayed(self):
        now = [1000.0]
        cache = BrowserActionCache(self.path, key=b"test-cache-key-0123456789", clock=lambda: now[0])
        cache.record_success(
            self.intent,
            build_parameterized_search_template(fields=("title", "source")),
        )
        entry = cache.lookup(self.intent).entry
        self.assertIsNotNone(entry)

        cache.record_failure(entry.entry_id)

        self.assertEqual(cache.lookup(self.intent).status, "disabled")
        now[0] += 24 * 60 * 60 + 1
        self.assertIn(cache.lookup(self.intent).status, {"exact_hit", "template_hit"})


class SelfHealingLocatorTests(unittest.TestCase):
    def test_locator_rebinds_after_accessibility_ref_changes(self):
        old = parse_snapshot_candidates(
            """### Snapshot
            - listbox [ref=list-old]:
              - option [ref=option-old]: \"Python asyncio 入门\"
            """
        )
        new = parse_snapshot_candidates(
            """### Snapshot
            - listbox [ref=list-new]:
              - option [ref=option-new]: \"Python asyncio 入门\"
            """
        )
        descriptor = LocatorDescriptor(role="option", placeholder="$target_label")

        self.assertEqual(resolve_locator(descriptor, old, {"target_label": "Python asyncio 入门"}),
                         "option-old")
        self.assertEqual(resolve_locator(descriptor, new, {"target_label": "Python asyncio 入门"}),
                         "option-new")

    def test_unique_role_locator_has_a_safe_confidence_without_persisting_name(self):
        candidates = parse_snapshot_candidates(
            """### Snapshot
            - combobox [ref=search-new]: \"Python asyncio\"
            """
        )
        descriptor = LocatorDescriptor(role="combobox", placeholder="$query")

        self.assertEqual(resolve_locator(descriptor, candidates, {}), "search-new")

    def test_ambiguous_same_label_does_not_click_either_candidate(self):
        candidates = parse_snapshot_candidates(
            """### Snapshot
            - listbox [ref=list]:
              - option [ref=option-a]: \"Python asyncio 入门\"
              - option [ref=option-b]: \"Python asyncio 入门\"
            """
        )
        descriptor = LocatorDescriptor(role="option", placeholder="$target_label")

        self.assertIsNone(resolve_locator(
            descriptor, candidates, {"target_label": "Python asyncio 入门"}
        ))


if __name__ == "__main__":
    unittest.main()
