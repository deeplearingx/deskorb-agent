import json
from types import SimpleNamespace

from agent_runtime import AgentRuntime
from responses_tool_protocol import function_call_output


def _runtime_with_browser_checkpoint() -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._task_plan = SimpleNamespace(browser_required=True)
    runtime._browser_session = SimpleNamespace(
        _tab_records=[{"index": 0, "current": True, "title": "Books", "url": "https://books.toscrape.com/"}],
        _current_page_url="https://books.toscrape.com/",
        _action_log=[{"action": "snapshot", "ok": True, "state_changed": False}],
        observation_id="obs-2",
        tab_snapshot_id="tabs-1",
        tab_count=1,
        max_tabs=6,
        action_steps=2,
        max_action_steps=30,
        _scroll_count=0,
        max_scrolls=20,
        interaction_stage="unknown",
        next_allowed_actions=["snapshot", "click_ref"],
        model_allowed_actions=["snapshot", "click_ref", "extract"],
        cache_verified=False,
        _confirmation_count=0,
        evidence_ledger=SimpleNamespace(records=[]),
    )
    return runtime


def test_browser_result_projection_removes_duplicate_snapshot_bodies():
    runtime = _runtime_with_browser_checkpoint()
    page = "PAGE_BODY_" + ("x" * 100_000)
    result = {
        "ok": True,
        "observation_id": "obs-2",
        "content": [{"text": page}],
        "candidates": [{"ref": "e1", "role": "link", "name": "Book"}],
        "observations": [{"action": "snapshot", "ok": True, "content": [{"text": page}]}],
    }

    projected = json.loads(runtime._browser_tool_output_for_model(result))

    assert len(json.dumps(projected, ensure_ascii=False)) < runtime.BROWSER_MODEL_RESULT_CHARS
    assert len(json.dumps(projected["content"], ensure_ascii=False)) < 34_000
    assert "content" not in projected["observations"][0]


def test_browser_result_projection_preserves_find_text_targets():
    runtime = _runtime_with_browser_checkpoint()
    result = {
        "ok": True,
        "query": "Search",
        "matched": True,
        "matched_refs": [{
            "ref": "search-box", "role": "textbox", "name": "Search",
        }],
        "matched_ref_count": 1,
        "observation_id": "obs-2",
    }

    projected = json.loads(runtime._browser_tool_output_for_model(result))

    assert projected["matched_refs"] == [{
        "ref": "search-box", "role": "textbox", "name": "Search",
    }]


def test_browser_transcript_keeps_current_call_pair_but_drops_old_page_output():
    runtime = _runtime_with_browser_checkpoint()
    response = {
        "output": [{
            "type": "function_call",
            "call_id": "call-current",
            "name": "browser_action_batch",
            "arguments": "{\"actions\":[{\"action\":\"snapshot\",\"arguments\":{}}]}",
        }],
    }
    current_output = function_call_output("call-current", '{"ok":true,"content":"CURRENT_PAGE"}')
    old_page = {"role": "user", "content": [{"type": "input_text", "text": "OLD_PAGE_" + ("x" * 120_000)}]}

    compacted = runtime._compact_browser_transcript(
        [old_page],
        "open the public book site",
        response=response,
        outputs=[current_output],
    )
    encoded = json.dumps(compacted, ensure_ascii=False)

    assert len(encoded) < runtime.BROWSER_TRANSCRIPT_MAX_CHARS
    assert "OLD_PAGE_" not in encoded
    assert "call-current" in encoded
    assert "CURRENT_PAGE" in encoded
    assert "obs-2" in encoded
