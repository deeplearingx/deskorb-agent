import json

from browser_ref_extractor import parse_ref_snapshot
from browser_use_mcp import BrowserUseMCPBackend


class FakeBrowserUseBridge:
    def __init__(self):
        self.calls = []

    def call(self, name, arguments, timeout_seconds=None):
        self.calls.append((name, dict(arguments), timeout_seconds))
        if name == "browser_get_state":
            state = {
                "url": "https://example.com/",
                "title": "Example",
                "interactive_elements": [
                    {"index": 2, "tag": "a", "text": "Target page", "href": "/target"},
                    {"index": 4, "tag": "input", "placeholder": "Search"},
                    {"index": 5, "tag": "button", "text": "Search"},
                ],
            }
            return {"ok": True, "content": [{"type": "text", "text": json.dumps(state)}]}
        if name == "browser_get_html":
            return {
                "ok": True,
                "content": [{
                    "type": "text",
                    "text": "<html><title>Example</title><body><p>First paragraph with useful evidence.</p></body></html>",
                }],
            }
        if name == "browser_list_tabs":
            return {
                "ok": True,
                "content": [{
                    "type": "text",
                    "text": json.dumps([
                        {"tab_id": "aaaa", "url": "https://example.com/", "title": "Example"},
                        {"tab_id": "bbbb", "url": "https://example.org/", "title": "Other"},
                    ]),
                }],
            }
        return {"ok": True, "content": [{"type": "text", "text": "ok"}]}


def test_browser_use_state_converts_indices_to_short_lived_refs_and_clicks_current_index():
    bridge = FakeBrowserUseBridge()
    backend = BrowserUseMCPBackend(bridge)

    observed = backend.call("snapshot", {})
    assert observed["ok"]
    assert "Page URL: https://example.com/" in observed["content"][0]["text"]
    assert "[ref=bu-ref-1-2]" in observed["content"][0]["text"]

    clicked = backend.call("click_ref", {"ref": "bu-ref-1-2"})
    assert clicked["ok"]
    assert bridge.calls[-1][0] == "browser_click"
    assert bridge.calls[-1][1] == {"index": 2, "new_tab": False}

    stale = backend.call("click_ref", {"ref": "bu-ref-0-2"})
    assert stale["ok"] is False
    assert stale["failure_kind"] == "browser_unknown_ref"


def test_browser_use_extract_reads_title_url_and_first_html_paragraph_as_ref_bound_content():
    bridge = FakeBrowserUseBridge()
    backend = BrowserUseMCPBackend(bridge)
    backend.call("snapshot", {})

    result = backend.call("extract", {
        "ref": "bu-ref-1-2",
        "fields": ["title", "url", "excerpt"],
    })
    assert result["ok"]
    parsed = parse_ref_snapshot(result["content"], "bu-ref-1-2", ["title", "url", "excerpt"])
    assert parsed["trusted_ref"] is True
    assert parsed["fields"]["title"] == "Target page"
    assert parsed["fields"]["url"] == "https://example.com/target"
    assert parsed["fields"]["excerpt"].startswith("First paragraph")
    assert result["execution_source"] == "browser_use_html"


def test_browser_use_tab_list_translates_short_tab_ids_to_semantic_indices():
    bridge = FakeBrowserUseBridge()
    backend = BrowserUseMCPBackend(bridge)

    listed = backend.call("list_tabs", {})
    assert listed["ok"]
    assert "- 0: [Example](https://example.com/)" in listed["content"][0]["text"]

    switched = backend.call("switch_tab", {"index": 1})
    assert switched["ok"]
    assert bridge.calls[-1][0] == "browser_switch_tab"
    assert bridge.calls[-1][1] == {"tab_id": "bbbb"}


def test_browser_use_enter_requires_one_observed_submit_control():
    bridge = FakeBrowserUseBridge()
    backend = BrowserUseMCPBackend(bridge)
    backend.call("snapshot", {})

    pressed = backend.call("press_key", {"ref": "bu-ref-1-4", "key": "Enter"})
    assert pressed["ok"]
    assert bridge.calls[-1][0] == "browser_click"
    assert bridge.calls[-1][1] == {"index": 5}
