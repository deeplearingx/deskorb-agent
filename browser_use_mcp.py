"""DeskOrb semantic adapter for the Browser Use local MCP server.

Browser Use exposes a deliberately small, index-based MCP surface.  DeskOrb
must not leak those indices to its model or let the provider's free-form
extraction result become evidence, so this adapter owns three translations:

* Browser Use JSON state -> the bounded DeskOrb snapshot/ref contract.
* Short-lived DeskOrb refs -> the current Browser Use element index.
* Browser Use HTML observations -> a ref-bound, locally parsed extraction.

The adapter is intentionally backend-only.  Confirmation, origin policy,
action batching, observation generations, and evidence verification remain in
``BrowserExecutionSession``.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from browser_evidence import (
    decode_browser_content,
    is_safe_public_browser_url,
    safe_http_url,
)


class _VisibleHTMLParser(HTMLParser):
    """Extract bounded visible title/paragraph text without executing HTML."""

    _IGNORED = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.paragraphs: list[str] = []
        self._stack: list[str] = []
        self._current: list[str] | None = None
        self._title: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = str(tag or "").casefold()
        self._stack.append(lowered)
        if lowered == "title":
            self._title = []
        elif lowered == "p":
            self._current = []

    def handle_endtag(self, tag: str) -> None:
        lowered = str(tag or "").casefold()
        if lowered == "p" and self._current is not None:
            value = _normalize_text(" ".join(self._current))
            if value:
                self.paragraphs.append(value[:2_000])
            self._current = None
        elif lowered == "title" and self._title is not None:
            value = _normalize_text(" ".join(self._title))
            if value:
                self.title_parts.append(value[:500])
            self._title = None
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index] == lowered:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if any(item in self._IGNORED for item in self._stack):
            return
        text = str(data or "")
        if self._current is not None:
            self._current.append(text)
        if self._title is not None:
            self._title.append(text)


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _content_text(value: Any) -> str:
    """Flatten only MCP text blocks; image data is deliberately ignored."""
    if isinstance(value, str):
        return value[:256_000]
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"][:256_000]
        if isinstance(value.get("content"), (list, tuple)):
            return "\n".join(_content_text(item) for item in value["content"])
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(_content_text(item) for item in value)
    return ""


def _decode_json(value: Any) -> Any:
    decoded = decode_browser_content(value)
    if isinstance(decoded, list):
        text = _content_text(decoded).strip()
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text, strict=False)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return parsed
    if isinstance(decoded, str):
        text = decoded.strip()
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text, strict=False)
            except (TypeError, ValueError):
                return decoded
            if isinstance(parsed, (dict, list)):
                return parsed
    return decoded


def _quote(value: Any, limit: int = 1_200) -> str:
    text = str(value or "")[:limit]
    return json.dumps(text, ensure_ascii=False)


def _role_for_element(element: dict[str, Any]) -> str:
    tag = str(element.get("tag") or element.get("tag_name") or "generic").casefold()
    attributes = element.get("attributes") if isinstance(element.get("attributes"), dict) else {}
    explicit = str(attributes.get("role") or element.get("role") or "").casefold().strip()
    if explicit in {"button", "link", "menuitem", "option", "tab", "textbox", "searchbox",
                    "combobox", "checkbox", "radio", "heading", "main", "article", "region"}:
        return explicit
    if tag == "a":
        return "link"
    if tag == "button":
        return "button"
    if tag in {"textarea", "input"}:
        input_type = str(attributes.get("type") or element.get("type") or "").casefold()
        if input_type == "search" or "search" in str(attributes.get("placeholder") or "").casefold():
            return "searchbox"
        return "textbox"
    if tag in {"select", "option", "optgroup"}:
        return "combobox" if tag == "select" else "option"
    if tag == "summary":
        return "button"
    return "generic"


def _element_name(element: dict[str, Any]) -> str:
    attributes = element.get("attributes") if isinstance(element.get("attributes"), dict) else {}
    for key in ("text", "name", "aria-label", "placeholder", "value", "title"):
        value = element.get(key) if key in element else attributes.get(key)
        value = _normalize_text(value)
        if value:
            return value[:500]
    return ""


class BrowserUseMCPBackend:
    """Translate DeskOrb semantic actions to Browser Use MCP calls.

    ``bridge`` is the existing DeskOrb MCP bridge.  The adapter discovers the
    configured server's exposed names, but never exposes those raw tools to
    the model.  A small direct ``call`` bridge is also accepted for unit tests.
    """

    _TOOLS = {
        "navigate": "browser_navigate",
        "snapshot": "browser_get_state",
        "click_ref": "browser_click",
        "fill_ref": "browser_type",
        "press_key": "browser_click",
        "wait": "browser_get_state",
        "find_text": "browser_get_state",
        "scroll": "browser_scroll",
        "go_back": "browser_go_back",
        "list_tabs": "browser_list_tabs",
        "switch_tab": "browser_switch_tab",
        "open_ref_new_tab": "browser_click",
        "close_tab": "browser_close_tab",
        "extract": "browser_get_html",
    }
    _HTML_TOOL = "browser_get_html"
    _SEARCH_MARKERS = ("search", "find", "query", "搜索", "查找", "检索")

    def __init__(self, bridge: Any, *, server_name: str = "browser-use",
                 timeout_getter: Callable[[], float | None] | None = None):
        self.bridge = bridge
        self.server_name = str(server_name or "browser-use").strip()
        self.timeout_getter = timeout_getter
        self._tool_names: dict[str, str] = {}
        self._ref_map: dict[str, dict[str, Any]] = {}
        self._snapshot = ""
        self._state: dict[str, Any] = {}
        self._generation = 0
        self._tab_map: dict[int, str] = {}
        self._current_tab_id = ""
        self._current_url = ""
        self._current_title = ""

    @property
    def is_isolated(self) -> bool:
        """The runtime still requires the MCP server config to attest isolation."""
        return True

    def call(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if action == "verify":
            return {"ok": False, "error": "verify is evaluated by the semantic browser runtime."}
        if action not in self._TOOLS:
            return {"ok": False, "failure_kind": "unsupported_browser_action",
                    "error": "Unsupported Browser Use semantic action."}
        if action in {"navigate", "open_ref_new_tab"}:
            url = str(arguments.get("url") or "")
            if url and not is_safe_public_browser_url(url):
                return {"ok": False, "failure_kind": "browser_navigation_url_blocked",
                        "error": "The Browser Use adapter accepts only public HTTP(S) URLs."}
        try:
            if action == "snapshot":
                return self._snapshot_action()
            if action == "find_text":
                return self._find_text(arguments)
            if action == "extract":
                return self._extract(arguments)
            if action == "list_tabs":
                return self._list_tabs()
            if action == "click_ref":
                return self._click_ref(arguments, new_tab=False)
            if action == "open_ref_new_tab":
                return self._click_ref(arguments, new_tab=True)
            if action == "fill_ref":
                return self._fill_ref(arguments)
            if action == "press_key":
                return self._press_key(arguments)
            if action == "switch_tab":
                return self._switch_tab(arguments)
            if action == "close_tab":
                return self._close_tab(arguments)
            if action == "wait":
                # Browser Use actions already wait for their own event.  A
                # bounded state request provides a useful readiness check for
                # autocomplete pages without inventing a new wait primitive.
                return self._snapshot_action()
            raw = self._TOOLS[action]
            payload = self._arguments(action, arguments)
            if action == "scroll":
                payload["direction"] = "up" if str(payload.get("direction")).casefold() == "up" else "down"
            return self._call_tool(raw, payload)
        except Exception as exc:
            return {"ok": False, "failure_kind": self._classify_failure(str(exc)),
                    "error": "The Browser Use MCP action failed."}

    def _snapshot_action(self) -> dict[str, Any]:
        result = self._call_tool("browser_get_state", {"include_screenshot": False})
        if not result.get("ok"):
            return result
        state = _decode_json(result.get("content", ""))
        if not isinstance(state, dict):
            return {"ok": False, "failure_kind": "browser_backend_failure",
                    "error": "Browser Use returned an invalid browser state."}
        self._state = state
        self._generation += 1
        self._ref_map = {}
        self._current_url = safe_http_url(state.get("url"))
        self._current_title = _normalize_text(state.get("title"))[:500]
        lines: list[str] = []
        if self._current_url:
            lines.append(f"- Page URL: {self._current_url}")
        if self._current_title:
            lines.append(f"- Page title: {_quote(self._current_title, 500)}")
        lines.append("### Browser Use interactive elements")
        elements = state.get("interactive_elements")
        if not isinstance(elements, list):
            elements = []
        for raw in elements[:256]:
            if not isinstance(raw, dict):
                continue
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError):
                continue
            if index < 0 or index > 10_000:
                continue
            ref = f"bu-ref-{self._generation}-{index}"
            role = _role_for_element(raw)
            name = _element_name(raw)
            metadata = {"index": index, "role": role, "name": name,
                        "href": str(raw.get("href") or "")[:1_000],
                        "tag": str(raw.get("tag") or "")[:40]}
            self._ref_map[ref] = metadata
            line = f"- {role} {_quote(name)} [ref={ref}]"
            lines.append(line)
            href = self._observed_href(metadata.get("href"))
            if href:
                lines.append(f"  - /url: {href}")
        if not self._ref_map:
            lines.append("- generic \"No interactive elements observed\"")
        self._snapshot = "\n".join(lines)[:128_000]
        return {**result, "ok": True, "content": [{"type": "text", "text": self._snapshot}],
                "provider": "browser_use", "state_changed": False}

    def _find_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = _normalize_text(arguments.get("query"))[:160]
        if not query:
            return {"ok": False, "failure_kind": "invalid_browser_find_query",
                    "error": "Text search requires a bounded non-empty query."}
        if not self._snapshot:
            self._snapshot_action()
        folded = query.casefold()
        matches = [item for item in self._ref_map.items()
                   if folded in str(item[1].get("name") or "").casefold()
                   or folded in str(item[1].get("href") or "").casefold()]
        matched = folded in self._snapshot.casefold()
        if not matched:
            # Browser Use's state intentionally contains only interactive DOM
            # nodes.  A bounded HTML text probe lets DeskOrb distinguish
            # “target is ordinary text” from “target is absent”, which in turn
            # drives the site-search recovery branch.
            html_result = self._call_tool(self._HTML_TOOL, {})
            html = _content_text(html_result.get("content", "")) if html_result.get("ok") else ""
            matched = folded in html.casefold()
        return {"ok": True, "query": query, "matched": bool(matched),
                "matches": len(matches) if matches else (1 if matched else 0),
                "content": [{"type": "text", "text": self._snapshot}],
                "provider": "browser_use", "state_changed": False}

    def _click_ref(self, arguments: dict[str, Any], *, new_tab: bool) -> dict[str, Any]:
        ref = str(arguments.get("ref") or "")
        item = self._ref_map.get(ref)
        if not item:
            return {"ok": False, "failure_kind": "browser_unknown_ref",
                    "error": "The Browser Use ref is not bound to the latest state."}
        return self._call_tool("browser_click", {"index": int(item["index"]), "new_tab": bool(new_tab)})

    def _fill_ref(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ref = str(arguments.get("ref") or "")
        item = self._ref_map.get(ref)
        if not item:
            return {"ok": False, "failure_kind": "browser_unknown_ref",
                    "error": "The Browser Use ref is not bound to the latest state."}
        value = arguments.get("value", arguments.get("text", ""))
        return self._call_tool("browser_type", {"index": int(item["index"]), "text": str(value)})

    def _press_key(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Use an observed submit control for Enter when the server has no key tool.

        Browser Use's direct MCP surface currently has no keyboard-press tool.
        We therefore accept Enter only when the latest state contains one
        unambiguous submit/search control.  Other keys fail closed instead of
        pretending that typing the key changed the page.
        """
        key = str(arguments.get("key") or "").strip().casefold()
        if key not in {"enter", "return"}:
            return {"ok": False, "failure_kind": "unsupported_browser_key",
                    "error": "Browser Use MCP does not expose this keyboard key."}
        candidates = [
            (ref, item) for ref, item in self._ref_map.items()
            if str(item.get("role") or "") in {"button", "link"}
            and any(marker in str(item.get("name") or "").casefold()
                    for marker in self._SEARCH_MARKERS + ("submit", "提交"))
        ]
        if len(candidates) != 1:
            return {"ok": False, "failure_kind": "browser_key_submit_control_missing",
                    "error": "Enter requires one unique observed Search/Submit control."}
        _ref, item = candidates[0]
        return self._call_tool("browser_click", {"index": int(item["index"])})

    def _list_tabs(self) -> dict[str, Any]:
        result = self._call_tool("browser_list_tabs", {})
        if not result.get("ok"):
            return result
        decoded = _decode_json(result.get("content", ""))
        raw_tabs = decoded if isinstance(decoded, list) else []
        self._tab_map = {}
        lines: list[str] = []
        for index, raw in enumerate(raw_tabs[:32]):
            if not isinstance(raw, dict):
                continue
            tab_id = str(raw.get("tab_id") or raw.get("id") or "").strip()[:64]
            url = safe_http_url(raw.get("url"))
            if not tab_id or not url:
                continue
            title = _normalize_text(raw.get("title"))[:240]
            self._tab_map[index] = tab_id
            current = tab_id == self._current_tab_id
            marker = "(current) " if current else ""
            lines.append(f"- {index}: {marker}[{title}]({url})")
        self._current_url = self._current_url or ""
        return {**result, "ok": True, "content": [{"type": "text", "text": "\n".join(lines)}],
                "tabs": [{"index": index, "tab_id": tab_id} for index, tab_id in self._tab_map.items()],
                "provider": "browser_use", "state_changed": False}

    def _switch_tab(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            index = int(arguments.get("index"))
        except (TypeError, ValueError):
            return {"ok": False, "failure_kind": "browser_tab_reference_invalid",
                    "error": "The Browser Use tab ref is not bound to the latest tab list."}
        tab_id = self._tab_map.get(index)
        if not tab_id:
            return {"ok": False, "failure_kind": "browser_tab_reference_invalid",
                    "error": "The Browser Use tab ref is not bound to the latest tab list."}
        result = self._call_tool("browser_switch_tab", {"tab_id": tab_id})
        if result.get("ok"):
            self._current_tab_id = tab_id
        return result

    def _close_tab(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            index = int(arguments.get("index"))
        except (TypeError, ValueError):
            return {"ok": False, "failure_kind": "browser_tab_reference_invalid",
                    "error": "The Browser Use tab ref is not bound to the latest tab list."}
        tab_id = self._tab_map.get(index)
        if not tab_id:
            return {"ok": False, "failure_kind": "browser_tab_reference_invalid",
                    "error": "The Browser Use tab ref is not bound to the latest tab list."}
        result = self._call_tool("browser_close_tab", {"tab_id": tab_id})
        if result.get("ok") and self._current_tab_id == tab_id:
            self._current_tab_id = ""
        return result

    def _extract(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ref = str(arguments.get("ref") or "")
        if ref not in self._ref_map:
            return {"ok": False, "failure_kind": "browser_unknown_ref",
                    "error": "The Browser Use extraction ref is not bound to the latest state."}
        requested = [
            str(field).strip().casefold().replace("-", "_").replace(" ", "_")
            for field in arguments.get("fields") or () if str(field).strip()
        ][:16]
        item = self._ref_map[ref]
        fields: dict[str, str] = {}
        href = self._observed_href(item.get("href"))
        if "title" in requested:
            fields["title"] = str(item.get("name") or self._current_title)[:500]
        if "url" in requested:
            fields["url"] = href or self._current_url
        if "source" in requested:
            source_url = href or self._current_url
            host = str(urlsplit(source_url).hostname or "")
            fields["source"] = host.removeprefix("www.")[:500]
        if "excerpt" in requested:
            html_result = self._call_tool(self._HTML_TOOL, {})
            html = _content_text(html_result.get("content", "")) if html_result.get("ok") else ""
            fields["excerpt"] = self._first_paragraph(html)[:2_000]
        # Keep the envelope intentionally small and ref-bound.  No raw HTML
        # or Browser Use model extraction text is passed to the DeskOrb model.
        root_role = str(item.get("role") or "generic")
        root_name = str(item.get("name") or self._current_title)
        lines = []
        if self._current_url:
            lines.append(f"- Page URL: {self._current_url}")
        lines.append("### Snapshot")
        lines.append("```yaml")
        lines.append(f"- {root_role} {_quote(root_name)} [ref={ref}]")
        for field in requested:
            value = str(fields.get(field) or "").strip()
            if value:
                lines.append(f"  - {field}: {_quote(value, 2_000)}")
        lines.append("```")
        return {"ok": True, "content": [{"type": "text", "text": "\n".join(lines)}],
                "provider": "browser_use", "execution_source": "browser_use_html",
                "state_changed": False}

    def _first_paragraph(self, html: str) -> str:
        text = str(html or "")[:120_000]
        if not text:
            return ""
        parser = _VisibleHTMLParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            return ""
        return next((item for item in parser.paragraphs if len(item) >= 20),
                    parser.paragraphs[0] if parser.paragraphs else "")

    def _observed_href(self, value: Any) -> str:
        href = str(value or "").strip()
        if not href:
            return ""
        candidate = safe_http_url(href)
        if candidate:
            return candidate
        if href.startswith(("/", "./", "../")) and self._current_url:
            candidate = safe_http_url(urljoin(self._current_url, href))
            if candidate:
                return candidate
        return ""

    @staticmethod
    def _arguments(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {}
        if action == "navigate":
            return {"url": str(args.get("url") or "")}
        if action == "scroll":
            return {"direction": str(args.get("direction") or "down")}
        if action == "go_back":
            return {}
        return {}

    def _discover(self) -> None:
        if self._tool_names or not callable(getattr(self.bridge, "schemas", None)):
            return
        try:
            schemas = self.bridge.schemas((self.server_name,))
        except TypeError:
            schemas = self.bridge.schemas((self.server_name,))
        except Exception:
            return
        for schema in schemas or ():
            if not isinstance(schema, dict):
                continue
            exposed = str(schema.get("name") or "")
            original = next((raw for raw in set(self._TOOLS.values()) | {self._HTML_TOOL}
                             if exposed.endswith("_" + raw)), None)
            if original:
                self._tool_names[original] = exposed

    def _call_tool(self, original: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._discover()
        exposed = self._tool_names.get(original, original)
        timeout = self.timeout_getter() if callable(self.timeout_getter) else None
        if timeout is not None and float(timeout) <= 0:
            return {"ok": False, "failure_kind": "tool_execution_timeout",
                    "error": "The task deadline was exhausted before the Browser Use action started."}
        try:
            if timeout is None:
                result = self.bridge.call(exposed, arguments)
            else:
                try:
                    result = self.bridge.call(exposed, arguments, timeout_seconds=float(timeout))
                except TypeError:
                    result = self.bridge.call(exposed, arguments)
        except Exception as exc:
            return {"ok": False, "failure_kind": self._classify_failure(str(exc)),
                    "error": "The Browser Use MCP transport failed."}
        if not isinstance(result, dict) or not result.get("ok"):
            failure = self._classify_failure(
                str(result.get("error") or "") if isinstance(result, dict) else ""
            )
            existing_kind = result.get("failure_kind") if isinstance(result, dict) else ""
            return {**(result if isinstance(result, dict) else {}), "ok": False,
                    "failure_kind": str(existing_kind or failure),
                    "error": "The Browser Use MCP tool returned an error."}
        content = result.get("content", result)
        text = _content_text(content).strip()
        if text.casefold().startswith(("error:", "failed:", "failure:")):
            return {"ok": False, "failure_kind": self._classify_failure(text),
                    "error": "The Browser Use MCP tool reported an error."}
        return {**result, "ok": True, "content": content, "provider": "browser_use"}

    @staticmethod
    def _classify_failure(message: str) -> str:
        lowered = str(message or "").casefold()
        if any(marker in lowered for marker in ("timeout", "timed out", "deadline")):
            return "tool_execution_timeout"
        if any(marker in lowered for marker in (
                "closed", "disconnected", "broken pipe", "not running", "transport", "process exited",
        )):
            return "browser_mcp_connection_failed"
        if "not found" in lowered or "unknown tool" in lowered:
            return "browser_unknown_ref"
        return "browser_backend_failure"


__all__ = ["BrowserUseMCPBackend"]
