import tempfile
import unittest
from pathlib import Path
from queue import Queue

from agent_runtime import AgentRuntime
from browser_cache import BrowserActionCache, build_parameterized_search_template, parse_browser_task_intent


TASK = (
    "打开 http://127.0.0.1:8123/dynamic_search.html，在搜索框中输入 Python asyncio，"
    "点击名为‘Python asyncio 入门’的下拉选项，最后提取标题和来源。"
)


class BrowserCacheRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = BrowserActionCache(
            Path(self.temp_dir.name) / "cache.sqlite3",
            key=b"runtime-cache-key-0123456789",
        )
        self.runtime = AgentRuntime(
            Queue(), "fixture-model", "https://example.test/v1",
            working_dir=self.temp_dir.name, browser_cache=self.cache,
        )

    def tearDown(self):
        if self.runtime.mcp:
            self.runtime.mcp.close()
        self.temp_dir.cleanup()

    def test_browser_cache_lookup_uses_same_key_as_persistent_cache(self):
        intent = parse_browser_task_intent(TASK, key=self.cache.key)
        self.assertIsNotNone(intent)
        self.cache.record_success(intent, build_parameterized_search_template(fields=intent.fields))

        lookup = self.runtime._browser_cache_lookup(TASK)

        self.assertEqual(lookup.status, "exact_hit")

    def test_tool_result_exposes_cache_metrics_without_arguments(self):
        self.runtime._publish_tool_result(
            "browser_action_batch",
            {"actions": [{"action": "fill_ref", "arguments": {"value": "secret"}}]},
            {
                "ok": True,
                "execution_source": "cache",
                "cache_status": "exact_hit",
                "model_fallback": False,
                "model_planning_requests": 0,
                "deterministic_steps": 5,
                "postcondition_passed": True,
                "action_steps": 5,
            },
        )

        kind, payload = self.runtime.ui.get_nowait()
        self.assertEqual(kind, "tool_result")
        self.assertEqual(payload["execution_source"], "cache")
        self.assertEqual(payload["cache_status"], "exact_hit")
        self.assertFalse(payload["model_fallback"])
        self.assertNotIn("secret", repr(payload))

    def test_cached_workflow_fails_closed_when_full_access_is_disabled(self):
        intent = parse_browser_task_intent(TASK, key=self.cache.key)
        entry = self.cache.record_success(
            intent, build_parameterized_search_template(fields=intent.fields)
        )
        self.runtime.full_access = False

        result = self.runtime._run_cached_browser_task(intent, entry)

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_cache_not_authorized")
        self.assertTrue(result["model_fallback"])

    def test_cached_browser_result_renderer_only_returns_allowlisted_fields(self):
        result = AgentRuntime._cached_browser_delta({
            "extraction": {"fields": {
                "title": "Python asyncio 入门", "source": "官方文档",
                "private": "must not be rendered",
            }}
        })
        self.assertIn("Python asyncio 入门", result)
        self.assertIn("官方文档", result)
        self.assertNotIn("must not be rendered", result)


if __name__ == "__main__":
    unittest.main()
