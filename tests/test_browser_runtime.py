import tempfile
import unittest
from pathlib import Path

from browser_runtime import BrowserExecutionSession, PlaywrightMCPBackend


def snapshot(ref: str, value: str, *, selected: str = "") -> list[dict[str, str]]:
    return [{"type": "text", "text": f"""### Page
- Page URL: http://127.0.0.1/search
### Snapshot
```yaml
- combobox [ref={ref}]: \"{value}\"
- option [ref=option-1]: \"{selected or 'Python asyncio 入门'}\"
```"""}]


class FakeBackend:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = []

    def call(self, action, arguments):
        self.calls.append((action, dict(arguments)))
        if action in {"snapshot", "extract"}:
            content = self.snapshots.pop(0) if self.snapshots else snapshot("search", "")
            return {"ok": True, "content": content}
        return {"ok": True, "content": [{"type": "text", "text": action + " accepted"}]}


class RaisingSnapshotBackend(FakeBackend):
    def call(self, action, arguments):
        if action == "snapshot":
            raise RuntimeError("simulated MCP disconnect")
        return super().call(action, arguments)


class InvalidResultBackend(FakeBackend):
    def call(self, action, arguments):
        if action in {"fill_ref", "extract"}:
            return []
        return super().call(action, arguments)


class FakeBridge:
    def __init__(self):
        self.calls = []

    def owns(self, name):
        return name in {
            "mcp_playwright_browser_snapshot", "mcp_playwright_browser_click",
            "mcp_playwright_browser_type", "mcp_playwright_browser_select_option",
            "mcp_playwright_browser_navigate", "mcp_playwright_browser_wait_for",
            "mcp_playwright_browser_tabs",
        }

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return {"ok": True, "content": [{"type": "text", "text": "accepted"}]}


class BrowserExecutionSessionTests(unittest.TestCase):
    def test_snapshot_backend_exception_becomes_bounded_failure(self):
        session = BrowserExecutionSession(RaisingSnapshotBackend([]))
        result = session.execute([{"action": "snapshot", "arguments": {}}])

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_backend_failure")
        self.assertTrue(result["requires_reobservation"])

    def test_invalid_backend_action_result_fails_closed(self):
        backend = InvalidResultBackend([snapshot("search", "")])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        result = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python", "observation_id": observation_id,
        }}])

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_backend_failure")

    def test_navigation_can_start_without_an_observation(self):
        backend = FakeBackend([snapshot("search", "")])
        session = BrowserExecutionSession(backend)
        result = session.execute([{"action": "navigate", "arguments": {"url": "http://127.0.0.1/"}}])
        self.assertTrue(result["ok"], result)
        self.assertEqual([item[0] for item in backend.calls], ["navigate", "snapshot"])

    def test_state_action_is_followed_by_observation_and_reports_progress(self):
        backend = FakeBackend([
            snapshot("search", ""),
            snapshot("search", "Python", selected="Python asyncio 入门"),
        ])
        session = BrowserExecutionSession(backend)

        first = session.execute([{"action": "snapshot", "arguments": {}}])
        observation_id = first["observation_id"]
        result = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python", "observation_id": observation_id,
        }}])

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["state_changed"])
        self.assertEqual([item[0] for item in backend.calls], ["snapshot", "fill_ref", "snapshot"])
        self.assertNotEqual(observation_id, result["observation_id"])

    def test_wait_accepts_millisecond_alias_and_observation_binding(self):
        backend = FakeBackend([snapshot("search", ""), snapshot("search", "ready")])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        result = session.execute([{"action": "wait", "arguments": {
            "observation_id": observation_id, "ms": 250,
        }}])
        self.assertTrue(result["ok"])
        self.assertEqual(backend.calls[1], ("wait", {"observation_id": observation_id, "ms": 250}))

    def test_plain_text_field_is_not_locked_as_autocomplete(self):
        content = [{"type": "text", "text": """### Snapshot
- textbox [ref=plain]: ""
"""}]
        after_first = [{"type": "text", "text": """### Snapshot
- textbox [ref=plain]: "first"
"""}]
        after_second = [{"type": "text", "text": """### Snapshot
- textbox [ref=plain]: "second"
"""}]
        backend = FakeBackend([content, after_first, after_second])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]

        first = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "plain", "value": "first", "observation_id": observation_id,
        }}])
        second = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "plain", "value": "second", "observation_id": first["observation_id"],
        }}])

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertNotIn("wait", [item[0] for item in backend.calls])

    def test_identical_no_progress_input_is_handed_off_before_replay(self):
        backend = FakeBackend([snapshot("search", ""), snapshot("search", "")])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        action = {"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python", "observation_id": observation_id,
        }}

        first = session.execute([action])
        refreshed = session.execute([{"action": "snapshot", "arguments": {}}])
        second = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python", "observation_id": refreshed["observation_id"],
        }}])

        self.assertFalse(first["ok"])
        self.assertEqual(first["failure_kind"], "browser_no_progress")
        self.assertTrue(first["requires_reobservation"])
        self.assertFalse(second["ok"])
        self.assertTrue(second["handoff_required"])
        self.assertEqual(second["failure_kind"], "browser_no_progress")
        self.assertEqual([item[0] for item in backend.calls], ["snapshot", "fill_ref", "snapshot", "snapshot"])

    def test_handoff_resume_requires_a_fresh_observation_before_any_ref_action(self):
        backend = FakeBackend([snapshot("search", "")])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python", "observation_id": observation_id,
        }}])
        refreshed = session.execute([{"action": "snapshot", "arguments": {}}])
        handoff = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python", "observation_id": refreshed["observation_id"],
        }}])
        self.assertTrue(handoff["handoff_required"])
        session.resume_after_handoff()
        stale = session.execute([{"action": "click_ref", "arguments": {
            "ref": "option-1", "observation_id": refreshed["observation_id"],
        }}])
        self.assertEqual(stale["failure_kind"], "stale_browser_observation")
        fresh = session.execute([{"action": "snapshot", "arguments": {}}])
        self.assertTrue(fresh["ok"])

    def test_handoff_blocks_snapshot_until_resume(self):
        backend = FakeBackend([snapshot("search", "")])
        session = BrowserExecutionSession(backend)
        session._handoff_required = True
        result = session.execute([{"action": "snapshot", "arguments": {}}])
        self.assertEqual(result["failure_kind"], "browser_handoff_required")
        self.assertEqual(backend.calls, [])

    def test_submit_like_browser_action_is_rejected_before_backend_call(self):
        backend = FakeBackend([snapshot("search", "")])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        result = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "login-password", "value": "secret", "observation_id": observation_id,
        }}])
        self.assertEqual(result["failure_kind"], "browser_high_risk_confirmation_required")
        self.assertEqual([item[0] for item in backend.calls], ["snapshot"])

    def test_old_observation_id_is_rejected_without_calling_backend(self):
        backend = FakeBackend([snapshot("search", ""), snapshot("search", "Python")])
        session = BrowserExecutionSession(backend)
        old_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python", "observation_id": old_id,
        }}])
        current_id = session.observation_id
        result = session.execute([{"action": "click_ref", "arguments": {
            "ref": "option-1", "observation_id": old_id,
        }}])

        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "stale_browser_observation")
        self.assertEqual(result["observation_id"], current_id)
        self.assertEqual([item[0] for item in backend.calls], ["snapshot", "fill_ref", "snapshot"])

    def test_extract_and_verify_require_structured_ref_evidence(self):
        content = [{"type": "text", "text": """### Page
- Page URL: http://127.0.0.1/search
### Snapshot
```yaml
- article [ref=card-1]:
  - heading \"Python asyncio 入门\" [ref=title-1]
  - paragraph [ref=price-1]: \"price: ¥129\"
  - link [ref=url-1]:
    - /url: http://127.0.0.1/python
```"""}]
        backend = FakeBackend([content, content])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        extracted = session.execute([{"action": "extract", "arguments": {
            "ref": "card-1", "fields": ["title", "price", "url"],
            "observation_id": observation_id,
        }}])
        verified = session.execute([{"action": "verify", "arguments": {
            "required_fields": ["title", "price", "url"], "price_min": 100, "price_max": 150,
        }}])

        self.assertTrue(extracted["ok"])
        self.assertTrue(extracted["verification"]["passed"])
        self.assertTrue(verified["ok"])
        self.assertTrue(verified["verification"]["passed"])
        self.assertTrue(verified["postcondition_passed"])

    def test_extract_observation_contains_provenance_for_reporters(self):
        content = [{"type": "text", "text": """### Page
- Page URL: http://127.0.0.1/search
### Snapshot
```yaml
- article [ref=card-1]:
  - heading \"Python asyncio\" [ref=title-1]
  - paragraph [ref=price-1]: \"price: 129\"
```"""}]
        backend = FakeBackend([content, content])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        result = session.execute([{"action": "extract", "arguments": {
            "ref": "card-1", "fields": ["title", "price"], "observation_id": observation_id,
        }}])
        observation = next(item for item in result["observations"] if item["action"] == "extract")
        self.assertEqual(observation["ref"], "card-1")
        self.assertEqual(observation["extraction"]["observation_id"], observation_id)

    def test_verify_expected_field_values_are_checked_against_extraction(self):
        content = [{"type": "text", "text": """### Page
### Snapshot
```yaml
- article [ref=card-1]:
  - heading [ref=title-1]: \"Python asyncio\"
  - paragraph [ref=source-1]: \"source: 官方文档\"
```
"""}]
        backend = FakeBackend([content, content])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        extracted = session.execute([{"action": "extract", "arguments": {
            "ref": "card-1", "fields": ["title", "source"], "observation_id": observation_id,
        }}])
        self.assertTrue(extracted["ok"])
        verified = session.execute([{"action": "verify", "arguments": {
            "expected": {"title": "Python asyncio", "source": "官方文档"},
            "required_fields": ["title", "source"],
        }}])
        self.assertTrue(verified["verification"]["passed"])

    def test_same_ref_extraction_is_not_repeated_on_one_observation(self):
        content = [{"type": "text", "text": """### Page
### Snapshot
```yaml
- article [ref=card-1]:
  - heading [ref=title-1]: \"Python asyncio\"
```
"""}]
        backend = FakeBackend([content, content])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        arguments = {"ref": "card-1", "fields": ["title"], "observation_id": observation_id}

        first = session.execute([{"action": "extract", "arguments": arguments}])
        repeated = session.execute([{"action": "extract", "arguments": arguments}])

        self.assertTrue(first["ok"])
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["failure_kind"], "browser_evidence_loop")
        self.assertEqual([item[0] for item in backend.calls], ["snapshot", "extract"])

    def test_new_observation_invalidates_previous_extraction_before_verify(self):
        content = [{"type": "text", "text": """### Page
### Snapshot
```yaml
- article [ref=card-1]:
  - heading [ref=title-1]: \"Python asyncio\"
```
"""}]
        backend = FakeBackend([content, content, content])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        session.execute([{"action": "extract", "arguments": {
            "ref": "card-1", "fields": ["title"], "observation_id": observation_id,
        }}])
        session.execute([{"action": "snapshot", "arguments": {}}])
        result = session.execute([{"action": "verify", "arguments": {"required_fields": ["title"]}}])

        self.assertFalse(result["verification"]["passed"])
        self.assertEqual(result["failure_kind"], "browser_stale_evidence")

    def test_same_verification_is_not_repeated_on_one_extraction(self):
        content = [{"type": "text", "text": """### Page
### Snapshot
```yaml
- article [ref=card-1]:
  - heading [ref=title-1]: \"Python asyncio\"
```
"""}]
        backend = FakeBackend([content, content])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        session.execute([{"action": "extract", "arguments": {
            "ref": "card-1", "fields": ["title"], "observation_id": observation_id,
        }}])
        arguments = {"required_fields": ["title"]}
        first = session.execute([{"action": "verify", "arguments": arguments}])
        repeated = session.execute([{"action": "verify", "arguments": arguments}])

        self.assertTrue(first["verification"]["passed"])
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["failure_kind"], "browser_evidence_loop")

    def test_failed_extraction_allows_one_relocation_then_handoffs(self):
        incomplete = [{"type": "text", "text": """### Page
### Snapshot
```yaml
- article [ref=card-1]:
  - heading [ref=title-1]: \"Python asyncio\"
```
"""}]
        backend = FakeBackend([incomplete, incomplete, incomplete, incomplete])
        session = BrowserExecutionSession(backend)
        first_observation = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        first = session.execute([{"action": "extract", "arguments": {
            "ref": "card-1", "fields": ["title", "source"],
            "observation_id": first_observation,
        }}])

        self.assertFalse(first["ok"])
        self.assertEqual(first["failure_kind"], "browser_evidence_insufficient")
        self.assertTrue(first["requires_reobservation"])
        self.assertEqual(session.execute([{"action": "extract", "arguments": {
            "ref": "card-1", "fields": ["title", "source"],
            "observation_id": first_observation,
        }}])["failure_kind"], "browser_reobservation_required")

        second_observation = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        second = session.execute([{"action": "extract", "arguments": {
            "ref": "card-1", "fields": ["title", "source"],
            "observation_id": second_observation,
        }}])
        self.assertFalse(second["ok"])
        self.assertTrue(second["handoff_required"])
        self.assertEqual(second["failure_kind"], "browser_no_progress")

    def test_repeated_successful_state_action_is_blocked_before_backend_replay(self):
        backend = FakeBackend([snapshot("search", ""), snapshot("search", "Python"),
                               snapshot("search", "Python")])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        action = {"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python", "observation_id": observation_id,
        }}
        first = session.execute([action])
        repeated = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python", "observation_id": first["observation_id"],
        }}])

        self.assertTrue(first["ok"])
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["failure_kind"], "browser_repeated_state_action")
        self.assertEqual([item[0] for item in backend.calls], ["snapshot", "fill_ref", "snapshot"])

    def test_dynamic_search_locks_input_until_options_are_observed(self):
        initial = [{"type": "text", "text": """### Snapshot
- combobox [ref=search]: ""
"""}]
        after_fill = [{"type": "text", "text": """### Snapshot
- combobox [ref=search]: "Python asyncio"
"""}]
        with_options = snapshot("search-new", "Python asyncio", selected="Python asyncio 入门")
        backend = FakeBackend([initial, after_fill, with_options])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]

        first = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python asyncio", "observation_id": observation_id,
        }}])
        repeated = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python typing", "observation_id": first["observation_id"],
        }}])

        self.assertTrue(first["ok"])
        self.assertEqual(first["interaction_stage"], "ready_to_choose")
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["failure_kind"], "browser_input_stage_locked")
        self.assertEqual([item[0] for item in backend.calls], [
            "snapshot", "fill_ref", "snapshot", "wait", "snapshot",
        ])

    def test_handoff_resume_clears_input_stage_before_fresh_observation(self):
        initial = [{"type": "text", "text": """### Snapshot
- combobox [ref=search]: ""
"""}]
        options = snapshot("search", "Python asyncio", selected="Python asyncio 入门")
        backend = FakeBackend([initial, options, options, options])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]
        filled = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python asyncio", "observation_id": observation_id,
        }}])
        self.assertEqual(filled["interaction_stage"], "ready_to_choose")
        session._handoff_required = True

        session.resume_after_handoff()
        fresh = session.execute([{"action": "snapshot", "arguments": {}}])
        allowed = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search", "value": "Python asyncio", "observation_id": fresh["observation_id"],
        }}])

        self.assertNotEqual(allowed.get("failure_kind"), "browser_input_stage_locked")

    def test_dynamic_search_lock_survives_input_ref_rerender(self):
        initial = [{"type": "text", "text": """### Snapshot
- combobox [ref=search-old]: ""
"""}]
        after_fill = [{"type": "text", "text": """### Snapshot
- combobox [ref=search-new]: "Python asyncio"
"""}]
        with_options = snapshot("search-new", "Python asyncio", selected="Python asyncio 入门")
        backend = FakeBackend([initial, after_fill, with_options])
        session = BrowserExecutionSession(backend)
        observation_id = session.execute([{"action": "snapshot", "arguments": {}}])["observation_id"]

        first = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search-old", "value": "Python asyncio", "observation_id": observation_id,
        }}])
        repeated = session.execute([{"action": "fill_ref", "arguments": {
            "ref": "search-new", "value": "Python asyncio", "observation_id": first["observation_id"],
        }}])

        self.assertTrue(first["ok"])
        self.assertEqual(first["interaction_stage"], "ready_to_choose")
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["failure_kind"], "browser_input_stage_locked")
        self.assertEqual([item[0] for item in backend.calls], [
            "snapshot", "fill_ref", "snapshot", "wait", "snapshot",
        ])

    def test_cached_search_rebinds_changed_refs_and_requires_verified_evidence(self):
        initial = [{"type": "text", "text": """### Snapshot
- combobox [ref=search-a]: ""
"""}]
        after_fill = snapshot("search-b", "Python asyncio", selected="Python asyncio 入门")
        after_click = [{"type": "text", "text": """### Snapshot
- article [ref=card-new]:
  - heading [ref=title-new]: "Python asyncio 入门"
  - paragraph [ref=source-new]: "来源：官方文档"
"""}]
        extracted = after_click
        backend = FakeBackend([initial, initial, after_fill, after_click, extracted])
        session = BrowserExecutionSession(backend, locator_key=b"runtime-cache-key-0123456789")
        template = {
            "version": 1,
            "kind": "read_only_search",
            "read_only": True,
            "steps": [
                {"action": "navigate", "url": "$start_url"},
                {"action": "snapshot"},
                {"action": "fill_ref", "locator": {"role": "combobox", "placeholder": "$query"}, "value": "$query"},
                {"action": "click_ref", "locator": {"role": "option", "placeholder": "$target_label"}},
                {"action": "extract", "locator": {"role": "article", "placeholder": "$result_root"},
                 "fields": ["title", "source"]},
                {"action": "verify", "required_fields": ["title", "source"]},
            ],
        }
        from browser_cache import parse_browser_task_intent
        intent = parse_browser_task_intent(
            "打开 http://127.0.0.1/search，在搜索框中输入 Python asyncio，"
            "点击名为‘Python asyncio 入门’的下拉选项，提取标题和来源。",
            key=b"runtime-cache-key-0123456789",
        )

        result = session.execute_cached_search(intent, template)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["postcondition_passed"])
        self.assertEqual(result["execution_source"], "cache")
        self.assertIn(("click_ref", {"ref": "option-1", "observation_id": "obs-3"}), backend.calls)

    def test_action_budget_is_bounded(self):
        backend = FakeBackend([snapshot("search", "")])
        session = BrowserExecutionSession(backend, max_action_steps=1)
        session.execute([{"action": "snapshot", "arguments": {}}])
        result = session.execute([{"action": "verify", "arguments": {"required_fields": []}}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "browser_action_budget_exceeded")


class PlaywrightMCPBackendTests(unittest.TestCase):
    def test_maps_semantic_actions_to_pinned_playwright_tools(self):
        bridge = FakeBridge()
        backend = PlaywrightMCPBackend(bridge)

        backend.call("fill_ref", {"ref": "search", "value": "Python"})
        backend.call("click_ref", {"ref": "option-1"})
        backend.call("select_ref", {"ref": "language", "values": ["Python"]})

        self.assertEqual([name for name, _args in bridge.calls], [
            "mcp_playwright_browser_type",
            "mcp_playwright_browser_click",
            "mcp_playwright_browser_select_option",
        ])
        self.assertEqual(bridge.calls[0][1]["target"], "search")
        self.assertEqual(bridge.calls[0][1]["text"], "Python")
        self.assertEqual(bridge.calls[2][1]["values"], ["Python"])


if __name__ == "__main__":
    unittest.main()
