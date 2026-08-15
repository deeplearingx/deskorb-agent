"""Bounded semantic browser execution on top of the local Playwright MCP.

The model-facing contract is intentionally smaller than Playwright's raw tool
set.  This module owns observation generations, one-action batches, progress
checks, and bounded recovery.  Browser content is returned to the model when
needed, but state fingerprints, refs, and extraction history stay in memory.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import secrets
import time
from urllib.parse import urlsplit
from typing import Any, Callable, Iterable, Protocol

from browser_actions import STATE_CHANGING_ACTIONS, BrowserAction, validate_browser_action_batch
from browser_cache import (
    BrowserTaskIntent,
    LocatorDescriptor,
    build_parameterized_search_template,
    locator_for_candidate,
    parse_snapshot_candidates,
    resolve_locator,
)
from browser_evidence import (
    BrowserEvidenceLedger,
    decode_browser_content,
    extract_list_from_snapshot,
    is_safe_public_browser_url,
    normalize_evidence_fields,
    observed_link_url,
    origin_from_url,
    page_url_from_content,
    parse_tab_list,
    observed_link_urls,
    safe_http_url,
    subtree_lines,
)
from browser_ref_extractor import TrustedBrowserRefExtractor, parse_ref_snapshot


class BrowserBackend(Protocol):
    def call(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class PlaywrightMCPBackend:
    """Translate semantic browser actions to the pinned Playwright MCP tools."""

    _TOOLS = {
        "navigate": "mcp_playwright_browser_navigate",
        "snapshot": "mcp_playwright_browser_snapshot",
        "click_ref": "mcp_playwright_browser_click",
        "fill_ref": "mcp_playwright_browser_type",
        "select_ref": "mcp_playwright_browser_select_option",
        "press_key": "mcp_playwright_browser_press_key",
        "wait": "mcp_playwright_browser_wait_for",
        "switch_tab": "mcp_playwright_browser_tabs",
        "list_tabs": "mcp_playwright_browser_tabs",
        "open_ref_new_tab": "mcp_playwright_browser_tabs",
        "close_tab": "mcp_playwright_browser_tabs",
        "find_text": "mcp_playwright_browser_find",
        "scroll": "mcp_playwright_browser_press_key",
        "go_back": "mcp_playwright_browser_navigate_back",
        "extract": "mcp_playwright_browser_snapshot",
    }

    def __init__(self, bridge: Any, *, timeout_getter: Callable[[], float | None] | None = None):
        self.bridge = bridge
        self.timeout_getter = timeout_getter

    def call(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if action == "verify":
            return {"ok": False, "error": "verify is evaluated by the semantic browser runtime."}
        tool_name = self._TOOLS.get(action)
        if not tool_name:
            return {"ok": False, "error": "Unsupported semantic browser action.",
                    "failure_kind": "unsupported_browser_action"}
        if action in {"navigate", "open_ref_new_tab"}:
            target_url = str(arguments.get("url") or "")
            if not is_safe_public_browser_url(target_url):
                return {
                    "ok": False,
                    "failure_kind": "browser_navigation_url_blocked",
                    "error": "The real browser accepts only public HTTP(S) navigation targets.",
                }
        payload = self._arguments(action, arguments)
        # The bridge strips these fields before the MCP process sees them. They
        # keep the raw tool contract valid while the semantic runtime remains
        # the only model-facing policy boundary.
        if action in STATE_CHANGING_ACTIONS and action not in {"press_key", "scroll"}:
            payload.setdefault("_deskorb_risk_level", "normal")
            payload.setdefault("_deskorb_risk_reason", "bounded browser task action")
        timeout = self.timeout_getter() if callable(self.timeout_getter) else None
        if timeout is not None and float(timeout) <= 0:
            return {"ok": False, "failure_kind": "tool_execution_timeout",
                    "error": "The task deadline was exhausted before the browser action started."}
        if timeout is None:
            return self.bridge.call(tool_name, payload)
        try:
            parameters = inspect.signature(self.bridge.call).parameters
            supports_timeout = (
                "timeout_seconds" in parameters
                or any(item.kind is inspect.Parameter.VAR_KEYWORD
                       for item in parameters.values())
            )
        except (TypeError, ValueError):
            supports_timeout = True
        if supports_timeout:
            return self.bridge.call(tool_name, payload, timeout_seconds=float(timeout))
        # Small in-process bridges used by embedders may retain the legacy
        # two-argument call signature; the runtime still keeps its outer
        # deadline and fail-closed semantics.
        return self.bridge.call(tool_name, payload)

    @staticmethod
    def _arguments(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {}
        if action == "navigate":
            return {"url": str(args.get("url") or "")}
        if action == "snapshot":
            payload: dict[str, Any] = {}
            if args.get("target"):
                payload["target"] = str(args["target"])
            if args.get("depth") is not None:
                payload["depth"] = int(args["depth"])
            return payload
        if action == "extract":
            return {"target": str(args.get("ref") or "")}
        if action == "find_text":
            # Playwright MCP calls this field ``text``; the DeskOrb semantic
            # protocol intentionally keeps the provider-neutral name
            # ``query`` at the model boundary.
            return {"text": str(args.get("query") or "")}
        if action == "click_ref":
            payload = {"target": str(args.get("ref") or "")}
            for key in ("button", "doubleClick", "modifiers", "element"):
                if key in args:
                    payload[key] = args[key]
            return payload
        if action == "fill_ref":
            value = args.get("value", args.get("text", ""))
            payload = {"target": str(args.get("ref") or ""),
                       "text": str(value)}
            for key in ("slowly", "submit", "element"):
                if key in args:
                    payload[key] = args[key]
            return payload
        if action == "select_ref":
            return {"target": str(args.get("ref") or ""),
                    "values": [str(value) for value in args.get("values") or []]}
        if action == "press_key":
            # Playwright MCP's browser_press_key schema targets the focused
            # page and accepts only the key.  The semantic ref is validated by
            # BrowserExecutionSession before this adapter is called.
            return {"key": str(args.get("key") or "")}
        if action == "scroll":
            direction = str(args.get("direction") or "down").casefold()
            return {"key": "PageUp" if direction == "up" else "PageDown"}
        if action == "go_back":
            return {}
        if action == "wait":
            payload = {}
            if args.get("text") is not None:
                payload["text"] = str(args["text"])
            if args.get("text_gone") is not None:
                payload["textGone"] = str(args["text_gone"])
            if args.get("seconds") is not None:
                payload["time"] = float(args["seconds"])
            elif args.get("time") is not None:
                payload["time"] = float(args["time"])
            elif args.get("ms") is not None:
                payload["time"] = max(0.0, float(args["ms"]) / 1000.0)
            return payload
        if action == "switch_tab":
            return {"action": "select", "index": int(args.get("index", 0))}
        if action == "list_tabs":
            return {"action": "list"}
        if action == "open_ref_new_tab":
            return {"action": "new", "url": str(args.get("url") or "")}
        if action == "close_tab":
            return {"action": "close", "index": int(args.get("index", 0))}
        return {}


class BrowserExecutionSession:
    """Execute bounded semantic browser batches with fail-closed recovery."""

    _SNAPSHOT_RECOVERY_FAILURES = frozenset({
        "browser_unknown_ref", "stale_browser_observation",
        "browser_login_target_requires_observed_link",
        "browser_target_unresolved", "browser_research_repository_page_required",
        "browser_research_repository_navigation_blocked",
        "browser_research_record_required_before_issues", "browser_evidence_insufficient",
        "browser_navigation_requires_observed_link", "browser_navigation_origin_not_allowed",
    })
    _SNAPSHOT_RECOVERY_ACTIONS = frozenset({
        "click_ref", "fill_ref", "select_ref", "press_key", "open_ref_new_tab",
        "extract", "extract_list", "navigate",
    })
    # A page can contain a very large accessibility tree.  Keep the full
    # snapshot internally for trusted extraction, but send a smaller bounded
    # projection to the model.  AgentRuntime also removes duplicate snapshot
    # copies from ``observations`` before the next provider request.
    _MAX_RECOVERY_SNAPSHOT_CHARS = 32_000
    _MAX_MODEL_SNAPSHOT_CHARS = 32_000

    _REOBSERVATION_FAILURES = frozenset({
        "browser_mcp_connection_failed", "tool_execution_timeout",
    })
    _SEARCH_FALLBACK_HOSTS = frozenset({
        "google.com", "www.google.com", "bing.com", "www.bing.com", "cn.bing.com",
        "baidu.com", "www.baidu.com", "duckduckgo.com", "www.duckduckgo.com",
        "search.brave.com",
    })
    _GITHUB_NON_REPOSITORY_SEGMENTS = frozenset({
        "features", "topics", "search", "trending", "marketplace", "settings",
        "notifications", "login", "sponsors", "orgs", "users", "collections",
        "explore", "about", "events", "contact", "pricing", "customer-stories",
    })

    def __init__(self, backend: BrowserBackend, *, max_action_steps: int = 20,
                 max_scrolls: int = 20, max_tabs: int = 6,
                 max_navigation_retries: int = 1,
                 max_evidence_failures: int = 2,
                 minimum_results: int = 0,
                 minimum_tab_pairs: int = 0,
                 final_tab_mode: str = "any",
                 allowed_origins: Iterable[str] | None = None,
                 required_evidence_before_issues: Iterable[str] | None = None,
                 strict_navigation_origins: bool | None = None,
                 search_discovery_required: bool = False,
                 repository_research_only: bool = False,
                 handoff_timeout_seconds: int = 120, clock=time.monotonic,
                 on_state_action: Callable[[str, str], None] | None = None,
                 locator_key: bytes | None = None):
        self.backend = backend
        self.max_action_steps = max(1, min(120, int(max_action_steps)))
        self.max_scrolls = max(0, min(100, int(max_scrolls)))
        self.max_tabs = max(1, min(12, int(max_tabs)))
        self.max_navigation_retries = max(0, min(1, int(max_navigation_retries)))
        self.max_evidence_failures = max(2, min(4, int(max_evidence_failures)))
        self.minimum_results = max(0, min(20, int(minimum_results)))
        self.minimum_tab_pairs = max(0, min(10, int(minimum_tab_pairs)))
        self.final_tab_mode = str(final_tab_mode or "any").strip().casefold()
        self.required_evidence_before_issues = tuple(dict.fromkeys(
            str(field).strip().casefold().replace("-", "_").replace(" ", "_")
            for field in (required_evidence_before_issues or ()) if str(field).strip()
        ))
        self.allowed_origins = tuple(sorted({
            str(origin).strip().casefold().rstrip("/")
            for origin in (allowed_origins or ())
            if str(origin).strip() and is_safe_public_browser_url(str(origin).strip())
        }))
        self.strict_navigation_origins = bool(
            isinstance(backend, PlaywrightMCPBackend)
            if strict_navigation_origins is None else strict_navigation_origins
        )
        self.search_discovery_required = bool(search_discovery_required)
        self.repository_research_only = bool(repository_research_only)
        self.handoff_timeout_seconds = max(1, int(handoff_timeout_seconds))
        self.clock = clock
        self.on_state_action = on_state_action
        self.locator_key = bytes(locator_key or b"deskorb-process-locator-key")
        self.reset()

    def reset(self) -> None:
        self._browser_session_id = f"bs-{secrets.token_hex(8)}"
        self._tab_generation = 1
        self._tab_id = "tab-1"
        self._observation_counter = 0
        self._observation_id = ""
        self._state_fingerprint = ""
        self._last_snapshot: Any = None
        self._last_no_progress_signature: str | None = None
        self._no_progress_count = 0
        self._last_state_action_signature: str | None = None
        self._repeated_state_action_count = 0
        self._reobservation_required = False
        self._handoff_required = False
        self._handoff_reason = ""
        self._action_steps = 0
        self._extractions: list[dict[str, Any]] = []
        self._last_extract_signature: str | None = None
        self._last_extract_observation_id = ""
        self._extract_parent_rebind_attempted = False
        self._last_verify_signature: str | None = None
        self._last_verify_observation_id = ""
        self._last_evidence_state_signature: str | None = None
        self._has_evidence_state_signature = False
        self._evidence_failure_count = 0
        self._trusted_extractor = TrustedBrowserRefExtractor()
        self._interaction_stage = "unknown"
        self._stage_transition_count = 0
        self._last_fill_value = ""
        self._action_log: list[dict[str, Any]] = []
        self._learned_locators: dict[str, LocatorDescriptor] = {}
        self._rebound_action_signatures: set[str] = set()
        self._snapshot_recovery_attempts: dict[str, int] = {}
        self._tab_recovery_attempts: dict[str, int] = {}
        self._cache_verified = False
        self._allowed_origin = ""
        self._allowed_origins: set[str] = set(self.allowed_origins)
        self._current_page_url = ""
        self._research_relocation_required = False
        self._scroll_count = 0
        self._navigation_attempts: dict[str, int] = {}
        self._tab_snapshot_counter = 0
        self._tab_snapshot_id = ""
        self._tab_refs: dict[str, dict[str, Any]] = {}
        self._tab_records: list[dict[str, Any]] = []
        self._last_tab_count = 1
        self._evidence_ledger = BrowserEvidenceLedger(max_records=60)
        self._authorized_high_risk: dict[str, Any] | None = None
        self._confirmation_count = 0
        self._login_flow_verified = False

    @property
    def observation_id(self) -> str:
        return self._observation_id

    @property
    def browser_session_id(self) -> str:
        return self._browser_session_id

    @property
    def tab_id(self) -> str:
        return self._tab_id

    @property
    def stage_transition_count(self) -> int:
        return self._stage_transition_count

    @property
    def evidence_ledger(self) -> BrowserEvidenceLedger:
        return self._evidence_ledger

    @property
    def tab_snapshot_id(self) -> str:
        return self._tab_snapshot_id

    @property
    def tab_count(self) -> int:
        return len(self._tab_records)

    @property
    def tab_pair_progress(self) -> dict[str, int]:
        """Return successful pair-opening/closing progress for tab-pressure tasks."""
        openings = sum(
            item.get("action") == "open_ref_new_tab" and item.get("ok")
            for item in self._action_log
        )
        closures = sum(
            item.get("action") == "close_tab" and item.get("ok")
            for item in self._action_log
        )
        return {
            "openings": int(openings),
            "closures": int(closures),
            "completed_pairs": int(min(openings, closures) // 2),
            "required_pairs": int(self.minimum_tab_pairs),
        }

    @property
    def next_allowed_actions(self) -> list[str]:
        if self._cache_verified:
            return []
        legacy_actions = [
            "navigate", "snapshot", "fill_ref", "click_ref", "select_ref",
            "press_key", "wait", "switch_tab", "extract", "verify",
        ]
        if self._handoff_required or self._reobservation_required:
            return ["snapshot"]
        if (self._research_relocation_required
                and not self._is_github_repository_url(self._current_page_url)):
            return ["snapshot", "find_text", "list_tabs", "switch_tab", "open_ref_new_tab",
                    "click_ref", "go_back"]
        if self._interaction_stage == "evidence_ready":
            return ["verify"]
        if self._interaction_stage == "result_ready":
            # A click may have opened a new target=_blank tab while the
            # current tab still exposes its old page.  Keep tab switching
            # available until evidence is verified; the action remains bound
            # to the current observation and is still followed by a snapshot.
            return ["snapshot", "list_tabs", "switch_tab", "extract", "extract_list"]
        if self._interaction_stage in {"waiting_for_options", "ready_to_choose"}:
            blocked = {"fill_ref", "navigate"}
            if self._interaction_stage == "ready_to_choose":
                blocked.add("wait")
            return [item for item in legacy_actions if item not in blocked]
        return legacy_actions

    @property
    def model_allowed_actions(self) -> list[str]:
        """Full action surface used by the model-facing schema."""
        if self._cache_verified:
            return []
        all_actions = [
            "navigate", "snapshot", "fill_ref", "click_ref", "select_ref",
            "press_key", "wait", "find_text", "scroll", "go_back",
            "list_tabs", "switch_tab", "open_ref_new_tab", "close_tab",
            "extract", "extract_list", "verify",
        ]
        if self._handoff_required or self._reobservation_required:
            return ["snapshot"]
        if (self._research_relocation_required
                and not self._is_github_repository_url(self._current_page_url)):
            return ["snapshot", "find_text", "list_tabs", "switch_tab", "open_ref_new_tab",
                    "click_ref", "go_back"]
        if self._interaction_stage in {"waiting_for_options", "ready_to_choose"}:
            blocked = {"fill_ref", "navigate"}
            if self._interaction_stage == "ready_to_choose":
                blocked.add("wait")
            return [item for item in all_actions if item not in blocked]
        return all_actions

    @property
    def action_steps(self) -> int:
        return self._action_steps

    def execute(self, value: Any) -> dict[str, Any]:
        if self._handoff_required:
            return self._failure("browser_handoff_required",
                                 "Manual browser handoff must be completed before continuing.",
                                 handoff_required=True,
                                 handoff_timeout_seconds=self.handoff_timeout_seconds)
        # A tab switch is still bound to the current observation, but the
        # runtime may fill that opaque binding when a model omits it.  This
        # keeps the contract fail-closed without making a harmless tab action
        # loop on a schema omission.
        if isinstance(value, list) and self._observation_id:
            prepared: list[Any] = []
            for item in value:
                if (isinstance(item, dict)
                        and str(item.get("action") or "").strip().lower()
                        in {"switch_tab", "list_tabs", "find_text", "scroll", "go_back",
                            "close_tab", "extract_list", "open_ref_new_tab"}):
                    nested = item.get("arguments")
                    if isinstance(nested, dict) and not str(nested.get("observation_id") or "").strip():
                        prepared.append({
                            **item,
                            "arguments": {**nested, "observation_id": self._observation_id},
                        })
                        continue
                    # Responses-compatible gateways sometimes flatten the
                    # arguments object into the action item.  Keep the same
                    # harmless tab-switch binding as the nested form, while
                    # leaving malformed non-object ``arguments`` untouched so
                    # the validator can reject it explicitly.
                    if nested is None and not str(item.get("observation_id") or "").strip():
                        prepared.append({**item, "observation_id": self._observation_id})
                        continue
                prepared.append(item)
            value = prepared
        actions, error = validate_browser_action_batch(value)
        if error:
            return self._failure("invalid_browser_action_batch", error,
                                 next_allowed_action=self._recovery_hint(error))
        if self._action_steps + len(actions) > self.max_action_steps:
            return self._failure("browser_action_budget_exceeded",
                                 "The browser action budget has been exhausted.")

        initial_observation = self._observation_id
        observations: list[dict[str, Any]] = []
        final: dict[str, Any] = {"ok": True}
        snapshot_seen_in_batch = False
        for action in actions:
            if self._action_steps >= self.max_action_steps:
                return self._failure("browser_action_budget_exceeded",
                                     "The browser action budget has been exhausted.",
                                     observations=observations)
            if (self._research_relocation_required
                    and not self._is_github_repository_url(self._current_page_url)
                    and action.action in {"extract", "extract_list", "verify"}):
                final = self._failure(
                    "browser_research_repository_page_required",
                    "Relocate to an observed GitHub /owner/repository page before collecting research evidence.",
                    requires_reobservation=False,
                )
                self._action_steps += 1
                self._action_log.append({
                    "action": action.action,
                    "ok": False,
                    "state_changed": False,
                })
                observations.append({
                    "action": action.action,
                    "ok": False,
                    "state_changed": False,
                    "failure_kind": final.get("failure_kind"),
                    "observation_id": self._observation_id,
                })
                break
            if not self._stage_allows_action(action.action):
                final = self._failure(
                    "browser_action_not_allowed_for_stage",
                    "The requested browser action is not allowed in the current interaction stage. "
                    "Follow next_allowed_actions from the latest semantic result.",
                    requested_action=action.action,
                )
                self._action_steps += 1
                self._action_log.append({
                    "action": action.action,
                    "ok": False,
                    "state_changed": False,
                })
                observations.append({
                    "action": action.action,
                    "ok": False,
                    "state_changed": False,
                    "failure_kind": final.get("failure_kind"),
                    "observation_id": self._observation_id,
                })
                break
            # Observation-only actions cannot see the result of an earlier
            # snapshot in the same provider batch.  Rebind only the actions
            # that do not carry a page ref or perform a state mutation; this
            # keeps ``snapshot + list_tabs/find_text/extract_list`` safe while
            # stale element refs and state actions remain fail-closed.
            if (snapshot_seen_in_batch
                    and action.action in {"list_tabs", "find_text", "extract_list"}):
                supplied = str(action.arguments.get("observation_id") or "")
                if not supplied or supplied == initial_observation:
                    action = BrowserAction(action.action, {
                        **action.arguments, "observation_id": self._observation_id,
                    })
            if action.action == "snapshot":
                final = self._observe(action.arguments)
            elif action.action == "find_text":
                final = self._find_text(action.arguments)
            elif action.action == "list_tabs":
                final = self._list_tabs(action.arguments)
            elif action.action == "extract":
                final = self._extract(action.arguments)
            elif action.action == "extract_list":
                final = self._extract_list(action.arguments)
            elif action.action == "verify":
                final = self._verify(action.arguments)
            else:
                final = self._state_action(action, initial_observation)
            if not final.get("ok") and self._should_attach_snapshot_recovery(action, final):
                final = self._attach_snapshot_recovery(action, final)
            if not final.get("ok") and self._should_attach_tab_recovery(action, final):
                final = self._attach_tab_recovery(action, final)
            self._action_steps += 1
            self._action_log.append({
                "action": action.action,
                "ok": bool(final.get("ok")),
                "state_changed": bool(final.get("state_changed")),
            })
            observations.append({
                "action": action.action,
                "ok": bool(final.get("ok")),
                "state_changed": bool(final.get("state_changed")),
                "failure_kind": final.get("failure_kind"),
                "verification": final.get("verification"),
                "observation_id": final.get("observation_id", self._observation_id),
                **({"ref": action.arguments.get("ref")} if action.action == "extract" else {}),
                **({"ref": action.arguments.get("ref")} if action.action == "open_ref_new_tab" else {}),
                **({"extraction": final["extraction"]} if "extraction" in final else {}),
                **({"extraction_list": final["extraction_list"]} if "extraction_list" in final else {}),
                **({"tabs": final["tabs"]} if "tabs" in final else {}),
                **({"content": final["content"]} if "content" in final else {}),
            })
            if action.action == "snapshot":
                snapshot_seen_in_batch = bool(final.get("ok"))
            if not final.get("ok"):
                break
            # A snapshot followed by a ref action in one batch is valid only
            # when the supplied ref was bound to the observation that existed
            # when this batch started. The action method handles the generation
            # update without exposing generated IDs to the model mid-batch.
        result = dict(final)
        result["observations"] = observations
        result["batch_action_steps"] = len(actions)
        result["action_steps"] = self._action_steps
        result["observation_id"] = self._observation_id
        result["interaction_stage"] = self._interaction_stage
        result["browser_session_id"] = self._browser_session_id
        result["tab_id"] = self._tab_id
        result["next_allowed_actions"] = self.next_allowed_actions
        result["model_allowed_actions"] = self.model_allowed_actions
        result["stage_transition_count"] = self._stage_transition_count
        result["confirmation_count"] = self._confirmation_count
        result["login_flow_verified"] = self._login_flow_verified
        result["tab_pair_progress"] = self.tab_pair_progress
        return result

    def _find_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        supplied = str(arguments.get("observation_id") or "")
        if not self._observation_id or supplied != self._observation_id:
            return self._failure("stale_browser_observation",
                                 "Fresh browser observation is required before text search.")
        query = str(arguments.get("query") or "").strip()
        if not query or len(query) > 160:
            return self._failure("invalid_browser_find_query",
                                 "Text search requires a bounded non-empty plain-text query.")
        try:
            result = self.backend.call("find_text", {"query": query})
        except Exception as exc:
            result = {"ok": False, "failure_kind": self._classify_backend_failure_kind(str(exc)),
                      "error": "The browser text-search tool failed."}
        if not isinstance(result, dict):
            result = {"ok": False, "failure_kind": "browser_backend_failure",
                      "error": "The browser text-search tool returned an invalid result."}
        if result.get("ok"):
            if "matched" in result:
                matched = bool(result.get("matched"))
            elif "matches" in result:
                matched = bool(result.get("matches"))
            else:
                # The pinned MCP returns a text-only result.  Keep the
                # semantic action truthful when the adapter does not provide
                # a structured match count.
                rendered = json.dumps(result.get("content", ""), ensure_ascii=False)
                matched = query.casefold() in rendered.casefold()
            query_folded = query.casefold()
            matched_refs = []
            for candidate in parse_snapshot_candidates(self._last_snapshot):
                candidate_text = " ".join((candidate.name, candidate.role, *candidate.parent_roles)).casefold()
                if query_folded in candidate_text:
                    matched_ref = {
                        "ref": candidate.ref,
                        "role": candidate.role,
                        "name": candidate.name[:160],
                    }
                    # Equal-named controls can have different semantics.  The
                    # OpenAI home page, for example, exposes both a decorative
                    # button and a real platform login link.  Surface only an
                    # observed public href so the model can choose between
                    # current targets without inventing a URL.
                    observed_href = observed_link_url(
                        self._last_snapshot, candidate.ref, base_url=self._current_page_url,
                    )
                    if observed_href and is_safe_public_browser_url(observed_href):
                        matched_ref["href"] = observed_href[:500]
                    matched_refs.append(matched_ref)
                if len(matched_refs) >= 16:
                    break
            # Login labels commonly occur on both a non-navigating button and
            # the actual observed authentication link.  Prefer the actionable
            # public link for this security-sensitive semantic query while
            # retaining every bounded match for callers that need to inspect
            # ambiguity.
            if any(marker in query_folded for marker in ("login", "log in", "sign in", "登录", "登入")):
                matched_refs.sort(key=lambda item: (0 if item.get("href") else 1,
                                                    str(item.get("role") or ""),
                                                    str(item.get("ref") or "")))
                observed_login_refs = [item for item in matched_refs if item.get("href")]
                if observed_login_refs:
                    # Decorative same-labelled controls are not useful model
                    # targets once an observed public authentication link is
                    # available. Keep the runtime's fail-closed click check,
                    # but make the safe choice deterministic for the model.
                    matched_refs = observed_login_refs
            elif self.search_discovery_required:
                # Search snapshots often put contributor profiles, site-root
                # navigation, and the real project result next to each other.
                # Keep every observed match, but make a real GitHub
                # /owner/repository link the deterministic first choice for
                # research queries.  The URL is still copied only from the
                # current trusted snapshot.
                matched_refs.sort(key=lambda item: (
                    0 if self._is_github_repository_url(item.get("href")) else
                    1 if item.get("href") else 2,
                    str(item.get("ref") or ""),
                ))
                if self.repository_research_only:
                    repository_refs = [
                        item for item in matched_refs
                        if self._is_github_repository_url(item.get("href"))
                    ]
                    if repository_refs:
                        matched_refs = repository_refs
            response = {**result, "ok": True, "query": query,
                        "observation_id": self._observation_id,
                        "matched": bool(matched or matched_refs),
                        "matched_refs": matched_refs,
                        "matched_ref_count": len(matched_refs),
                        "content_trust": "untrusted_page_data", "state_changed": False}
            if "content" in response:
                response["content"] = self._bounded_snapshot_content(
                    response.get("content"), self._MAX_MODEL_SNAPSHOT_CHARS,
                )
            if any(marker in query_folded for marker in ("login", "log in", "sign in", "登录", "登入")):
                preferred = next((item.get("ref") for item in matched_refs if item.get("href")), "")
                if preferred:
                    response["preferred_ref"] = preferred
            elif self.search_discovery_required:
                preferred = next(
                    (item.get("ref") for item in matched_refs
                     if self._is_github_repository_url(item.get("href"))),
                    "",
                )
                if preferred:
                    response["preferred_repository_ref"] = preferred
            return response
        failure_kind = self._classify_backend_failure_kind(result)
        if failure_kind in self._REOBSERVATION_FAILURES:
            return self._backend_failure(result)
        # The pinned MCP exposes browser_find, but a local or older adapter may
        # not.  A plain substring fallback over the already trusted snapshot is
        # still bounded and does not widen navigation or permissions.
        snapshot_text = str(self._last_snapshot or "")
        matched = query.casefold() in snapshot_text.casefold()
        return {
            "ok": True,
            "query": query,
            "matched": matched,
            "matches": 1 if matched else 0,
            "observation_id": self._observation_id,
            "content": self._bounded_snapshot_content(self._last_snapshot, self._MAX_MODEL_SNAPSHOT_CHARS)
            if matched else [],
            "content_trust": "untrusted_page_data",
            "state_changed": False,
            "fallback": "snapshot_substring_search",
        }

    def _list_tabs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        supplied = str(arguments.get("observation_id") or "")
        if not self._observation_id or supplied != self._observation_id:
            return self._failure("stale_browser_observation",
                                 "Fresh browser observation is required before listing tabs.")
        try:
            result = self.backend.call("list_tabs", {})
        except Exception as exc:
            return self._backend_failure({
                "ok": False,
                "failure_kind": self._classify_backend_failure_kind(str(exc)),
                "error": "The browser Tab listing failed.",
            })
        if not isinstance(result, dict) or not result.get("ok"):
            return self._backend_failure(result if isinstance(result, dict) else {
                "ok": False, "failure_kind": "browser_backend_failure",
                "error": "The browser Tab listing returned an invalid result.",
            })
        parsed = parse_tab_list(result.get("content", result))
        self._last_tab_count = len(parsed)
        self._tab_snapshot_counter += 1
        self._tab_snapshot_id = f"tabs-{self._tab_snapshot_counter}"
        self._tab_refs = {}
        self._tab_records = []
        for item in parsed[:self.max_tabs]:
            index = int(item.get("index") or 0)
            material = f"{self._browser_session_id}|{self._tab_snapshot_id}|{index}|{item.get('url','')}"
            token = hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:18]
            tab_ref = f"tabref-{self._tab_snapshot_counter}-{token}"
            record = {"tab_ref": tab_ref, "current": bool(item.get("current")),
                      "title": str(item.get("title") or "")[:240],
                      "url": str(item.get("url") or "")[:1000], "index": index}
            self._tab_refs[tab_ref] = record
            self._tab_records.append(record)
            tab_origin = origin_from_url(record.get("url"))
            if tab_origin:
                self._allowed_origins.add(tab_origin)
        if len(parsed) > self.max_tabs:
            return self._failure("browser_tab_limit_exceeded",
                                 "The browser already has more tabs than the task budget allows.",
                                 tab_snapshot_id=self._tab_snapshot_id,
                                 tabs=self._safe_tabs(), tab_count=len(parsed),
                                 max_tabs=self.max_tabs)
        return {**result, "ok": True, "tab_snapshot_id": self._tab_snapshot_id,
                "tabs": self._safe_tabs(), "tab_count": len(self._tab_records),
                "observation_id": self._observation_id, "state_changed": False,
                "content_trust": "untrusted_page_data"}

    def _safe_tabs(self) -> list[dict[str, Any]]:
        return [{key: value for key, value in item.items() if key != "index"}
                for item in self._tab_records]

    def _extract_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        supplied = str(arguments.get("observation_id") or "")
        if not self._observation_id or supplied != self._observation_id:
            return self._failure("stale_browser_observation",
                                 "Fresh browser observation is required before list extraction.")
        fields = list(arguments.get("fields") or [])
        unique_by = list(arguments.get("unique_by") or [])
        # Evidence records need a stable identity even when the model omits
        # it from the requested projection.  A URL is added only when the
        # caller asked to deduplicate by URL or is extracting issue titles;
        # the value still has to come from an observed link in the snapshot.
        normalized_fields = {str(field).strip().casefold().replace("-", "_")
                             for field in fields}
        normalized_unique = {str(field).strip().casefold().replace("-", "_")
                             for field in unique_by}
        try:
            page_path = urlsplit(self._current_page_url).path.casefold()
        except ValueError:
            page_path = ""
        if "issue_title" in normalized_fields and "/issues" not in page_path:
            return self._failure(
                "browser_issue_list_requires_issues_page",
                "Issue list extraction is only valid on the current repository Issues page.",
                observation_id=self._observation_id, state_changed=False,
            )
        if ("url" in normalized_unique or "issue_title" in normalized_fields) \
                and "url" not in normalized_fields:
            fields.append("url")
        repository_fields = {
            "stars", "language", "updated_at", "installation", "mcp", "memory",
            "multi_agent", "tool_calling",
        }
        current_host = str(urlsplit(self._current_page_url).hostname or "").casefold().rstrip(".")
        if (self.search_discovery_required
                and (current_host in self._SEARCH_FALLBACK_HOSTS
                     or current_host in {"github.com", "www.github.com"})
                and ((current_host in {"github.com", "www.github.com"}
                      and not self._is_github_repository_url(self._current_page_url))
                     or current_host in self._SEARCH_FALLBACK_HOSTS)
                and normalized_fields.intersection(repository_fields)):
            self._research_relocation_required = True
            return self._evidence_failure(
                "browser_research_repository_page_required",
                "Repository evidence must be collected from an observed repository page, not a search result or site root.",
                requires_reobservation=True,
            )
        try:
            limit = max(1, min(20, int(arguments.get("limit", 20))))
        except (TypeError, ValueError):
            return self._failure("invalid_browser_extract_list", "List extraction limit must be an integer.")
        extraction = extract_list_from_snapshot(
            self._last_snapshot,
            fields,
            limit=limit,
            scope_ref=str(arguments.get("scope_ref") or ""),
            unique_by=unique_by,
            page_url=self._current_page_url,
            observation_id=self._observation_id,
        )
        items = extraction.get("items") if isinstance(extraction, dict) else []
        if not isinstance(items, list) or not items:
            return self._evidence_failure(
                "browser_evidence_insufficient",
                "The current observation contains no bounded repeated records for extraction.",
                extraction_list=extraction, observation_id=self._observation_id,
                state_changed=False,
            )
        requested_field_names = [
            str(field).strip().casefold().replace("-", "_").replace(" ", "_")
            for field in fields if str(field).strip()
        ]
        incomplete_fields: set[str] = set()
        for item in items:
            item_fields = item.get("fields") if isinstance(item, dict) else {}
            normalized_item_fields = {
                str(key).strip().casefold().replace("-", "_").replace(" ", "_"): value
                for key, value in (item_fields.items() if isinstance(item_fields, dict) else ())
            }
            incomplete_fields.update(
                field for field in requested_field_names
                if not str(normalized_item_fields.get(field) or "").strip()
            )
        if incomplete_fields:
            return self._evidence_failure(
                "browser_evidence_insufficient",
                "At least one extracted list record is missing requested fields; choose a more specific observed list root.",
                extraction_list=extraction,
                verification={"passed": False, "missing_fields": sorted(incomplete_fields)},
                observation_id=self._observation_id,
                state_changed=False,
            )
        self._set_interaction_stage("evidence_ready")
        self._last_evidence_state_signature = self._last_state_action_signature
        self._has_evidence_state_signature = True
        self._extractions.append({
            "kind": "list",
            "items": items,
            "fields": list(extraction.get("fields") or []),
            "count": len(items),
            "trusted_ref": True,
            "observation_id": self._observation_id,
        })
        for item in items:
            item_fields = item.get("fields") if isinstance(item, dict) else {}
            self._evidence_ledger.add(
                kind="list_item", tab_id=self._tab_id,
                observation_id=self._observation_id,
                source_url=self._current_page_url,
                fields=item_fields if isinstance(item_fields, dict) else {},
                supporting_text=str(item.get("supporting_text") or "") if isinstance(item, dict) else "",
            )
        if "issue_title" in normalized_fields:
            issue_titles = [
                str((item.get("fields") or {}).get("issue_title") or "").strip()
                for item in items if isinstance(item, dict)
            ]
            issue_titles = [value for value in issue_titles if value]
            if issue_titles:
                self._evidence_ledger.merge_repository_fields(
                    source_url=self._current_page_url,
                    fields={"issue_title": " | ".join(dict.fromkeys(issue_titles))[:500]},
                    supporting_text="; ".join(dict.fromkeys(issue_titles))[:240],
                )
        return {
            "ok": True,
            "extraction_list": extraction,
            "verification": {"passed": True, "kind": "browser_list_extraction",
                              "matched_items": len(items), "required_fields": len(fields)},
            "observation_id": self._observation_id,
            "content_trust": "untrusted_page_data",
            "state_changed": False,
        }

    def _observe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.backend.call("snapshot", arguments)
        except Exception as exc:
            failure_kind = self._classify_backend_failure_kind(str(exc))
            failure = self._backend_failure({
                "ok": False,
                "failure_kind": failure_kind,
                "error": self._backend_failure_message(failure_kind, phase="observation"),
            })
            failure.setdefault("requires_reobservation", True)
            return failure
        if not isinstance(result, dict):
            return self._failure(
                "browser_backend_failure",
                "The local browser backend returned an invalid observation.",
                requires_reobservation=True,
            )
        if not result.get("ok"):
            return self._backend_failure(result)
        content = decode_browser_content(result.get("content", result))
        previous_stage = self._interaction_stage
        self._observation_counter += 1
        self._observation_id = f"obs-{self._observation_counter}"
        self._state_fingerprint = self._fingerprint(content)
        self._last_snapshot = content
        observed_url = page_url_from_content(content)
        if observed_url:
            observed_origin = origin_from_url(observed_url)
            if (self.strict_navigation_origins and self._allowed_origins and observed_origin
                    and observed_origin not in self._allowed_origins):
                self._reobservation_required = True
                return self._failure(
                    "browser_navigation_origin_changed",
                    "The browser ended on an origin that was not allowed by the task contract.",
                    requires_reobservation=True,
                )
            self._current_page_url = observed_url
            if observed_origin:
                self._allowed_origins.add(observed_origin)
                self._allowed_origin = observed_origin
            if self._is_github_repository_url(self._current_page_url):
                self._research_relocation_required = False
        if previous_stage == "evidence_ready":
            self._set_interaction_stage(
                "result_ready" if self._has_extractable_result(content) else "unknown"
            )
        elif previous_stage == "waiting_for_options" and self._has_options(content):
            self._set_interaction_stage("ready_to_choose")
        self._reobservation_required = False
        # A new observation invalidates all ref-bound evidence and any
        # observation-only retry signature.  State actions already do this
        # through _observe(); explicit snapshots must obey the same rule.
        self._extractions.clear()
        self._cache_verified = False
        self._last_extract_signature = None
        self._last_extract_observation_id = ""
        self._extract_parent_rebind_attempted = False
        self._last_verify_signature = None
        self._last_verify_observation_id = ""
        self._trusted_extractor.observe({"observation_id": self._observation_id})
        return {**result, "ok": True, "observation_id": self._observation_id,
                "state_changed": True, "content_trust": "untrusted_page_data",
                "page_url": self._current_page_url,
                "content": self._bounded_snapshot_content(
                    content, self._MAX_MODEL_SNAPSHOT_CHARS,
                ),
                "candidates": self._recovery_candidates()}

    def _stage_allows_action(self, action: str) -> bool:
        """Keep model-facing stages strict while preserving typed recovery errors."""
        if self._reobservation_required:
            # Let the action handler return the more specific stale/reobserve
            # failure instead of masking it with a generic stage error.
            return True
        if self._cache_verified:
            return action in {"snapshot", "list_tabs", "extract", "verify"}
        if self._interaction_stage == "ready_to_choose":
            # fill_ref is intentionally absent from next_allowed_actions, but
            # reaches its dedicated input-stage lock for clearer diagnostics.
            return action not in {"navigate", "wait"}
        if self._interaction_stage == "evidence_ready":
            # Evidence is a checkpoint, not a terminal stage.  Long research
            # tasks must be able to preserve the record and continue to a
            # second page or Tab; verify remains available as a final gate.
            return True
        if self._interaction_stage == "result_ready":
            # verify without a fresh extract must still reach _verify so stale
            # evidence is reported as such; the schema recommends extract.
            return True
        return True

    @classmethod
    def _is_github_repository_url(cls, value: Any) -> bool:
        parsed = urlsplit(str(value or ""))
        host = str(parsed.hostname or "").casefold().rstrip(".")
        if host not in {"github.com", "www.github.com"}:
            return False
        parts = [part.casefold() for part in str(parsed.path or "").split("/") if part]
        return len(parts) >= 2 and parts[0] not in cls._GITHUB_NON_REPOSITORY_SEGMENTS

    def _repository_record_verified(self) -> bool:
        required = set(self.required_evidence_before_issues)
        if not required:
            return True
        return any(
            required.issubset({
                str(key).strip().casefold().replace("-", "_").replace(" ", "_")
                for key, value in (record.fields or {}).items()
                if str(value or "").strip()
            })
            for record in self._evidence_ledger.records
            if str(record.kind).casefold() not in {"list_item", "issue"}
        )

    @classmethod
    def _is_issues_url(cls, value: Any) -> bool:
        parsed = urlsplit(str(value or ""))
        host = str(parsed.hostname or "").casefold().rstrip(".")
        parts = {part.casefold() for part in str(parsed.path or "").split("/") if part}
        return host in {"github.com", "www.github.com"} and "issues" in parts

    def _state_action(self, action: BrowserAction, initial_observation: str) -> dict[str, Any]:
        if self._handoff_required:
            return self._handoff_failure()
        navigation_key = ""
        navigation_attempt = 0
        if action.action == "navigate":
            url = str(action.arguments.get("url") or "")
            navigation_key = url.strip()
            navigation_attempt = self._navigation_attempts.get(navigation_key, 0)
            if navigation_attempt > self.max_navigation_retries:
                return self._failure(
                    "browser_navigation_retry_exhausted",
                    "This navigation target already used its bounded retry budget; use a search fallback or report the block.",
                    retries=self.max_navigation_retries,
                )
            self._navigation_attempts[navigation_key] = navigation_attempt + 1
            parsed = urlsplit(url)
            origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            observed_target = safe_http_url(url) in set(observed_link_urls(
                self._last_snapshot, base_url=self._current_page_url,
            ))
            current_host = str(urlsplit(self._current_page_url).hostname or "").casefold().rstrip(".")
            target_host = str(parsed.hostname or "").casefold().rstrip(".")
            target_path_parts = [part for part in str(parsed.path or "").split("/") if part]
            if (self.repository_research_only
                    and self._is_github_repository_url(self._current_page_url)
                    and target_host in {"github.com", "www.github.com"}
                    and not self._is_github_repository_url(url)):
                return self._failure(
                    "browser_research_repository_navigation_blocked",
                    "Stay on the observed repository and its same-repository pages while collecting evidence; "
                    "do not navigate to a GitHub profile, site root, or generic navigation page.",
                    requires_reobservation=True,
                )
            if (target_host in {"github.com", "www.github.com"}
                    and "issues" in {part.casefold() for part in target_path_parts}
                    and self.required_evidence_before_issues):
                if not self._repository_record_verified():
                    return self._failure(
                        "browser_research_record_required_before_issues",
                        "Verify the current repository's main evidence record before opening its Issues page.",
                    )
            if (self.search_discovery_required
                    and current_host in self._SEARCH_FALLBACK_HOSTS
                    and target_host not in self._SEARCH_FALLBACK_HOSTS
                    and (not observed_target or (
                        target_host in {"github.com", "www.github.com"}
                        and not self._is_github_repository_url(url)
                    ))):
                return self._failure(
                    "browser_navigation_requires_observed_link",
                    "This research task requires opening an observed result link; do not navigate "
                    "directly to an unobserved repository or a GitHub site root.",
                )
            origin_is_explicit = origin in self._allowed_origins or observed_target
            if self.strict_navigation_origins and not self._allowed_origin and not origin_is_explicit:
                host = str(parsed.hostname or "").casefold().rstrip(".")
                if host not in self._SEARCH_FALLBACK_HOSTS:
                    return self._failure(
                        "browser_navigation_origin_not_allowed",
                        "The first navigation target must be user-specified or a configured search engine.",
                    )
            if self._allowed_origin and origin != self._allowed_origin and not origin_is_explicit:
                host = str(parsed.hostname or "").casefold().rstrip(".")
                fallback_allowed = bool(
                    host in self._SEARCH_FALLBACK_HOSTS
                    and self._navigation_attempts
                )
                if not fallback_allowed:
                    return self._failure(
                        "browser_navigation_origin_not_allowed",
                        "Navigation to a new origin requires an explicit user target; page links cannot broaden scope.",
                    )
            self._allowed_origin = origin
            self._allowed_origins.add(origin)
        supplied = str(action.arguments.get("observation_id") or "")
        # Navigation is the only state action that can begin a browser task
        # without an existing page observation. Ref actions must always be
        # bound to the current generation; this is what prevents a model from
        # reusing a ref after a re-render or tab switch.
        observation_required = action.action != "navigate"
        ref_bound = action.action in {"click_ref", "fill_ref", "select_ref", "press_key", "open_ref_new_tab"}
        if observation_required and (not self._observation_id or supplied != self._observation_id):
            # When a batch starts with snapshot, allow the action to use the
            # pre-batch generation; the snapshot is the fresh observation it
            # was meant to bind to. No stale generation from an older batch is
            # accepted.
            if not ref_bound and not supplied and self._observation_id:
                action = BrowserAction(action.action, {
                    **action.arguments, "observation_id": self._observation_id,
                })
            elif not (initial_observation and supplied == initial_observation
                    and initial_observation != self._observation_id):
                return self._failure("stale_browser_observation",
                                     "Fresh browser observation is required before this action.")
            else:
                action = BrowserAction(action.action, {
                    **action.arguments, "observation_id": self._observation_id,
                })
        if self._reobservation_required:
            return self._failure("browser_reobservation_required",
                                 "Fresh browser observation is required before retrying this action.",
                                 requires_reobservation=True)

        # If the model tries to change the query while autocomplete is already
        # waiting for options, stop before locator lookup so a re-rendered ref
        # cannot turn a repeated search into a new input action.
        if action.action == "fill_ref" and self._interaction_stage in {
            "waiting_for_options", "ready_to_choose",
        }:
            requested_value = str(action.arguments.get("value", action.arguments.get("text", "")))
            if requested_value != self._last_fill_value:
                return self._failure(
                    "browser_input_stage_locked",
                    "The search input has already been filled. Observe or choose the current option; "
                    "do not fill the same search field again.",
                    interaction_stage=self._interaction_stage,
                    requires_reobservation=self._interaction_stage == "waiting_for_options",
                )

        # Risk is checked before ref binding so an obviously sensitive target
        # (for example login-password) is blocked even when the page has
        # already re-rendered that ref away.
        if action.action in {"click_ref", "fill_ref", "select_ref", "press_key"}:
            risk_failure = self._risk_failure_for_action(action)
            if risk_failure:
                return self._failure(*risk_failure)

        if action.action == "scroll" and self._scroll_count >= self.max_scrolls:
            return self._failure("browser_scroll_budget_exceeded",
                                 "The bounded browser scroll budget has been exhausted.",
                                 max_scrolls=self.max_scrolls)

        original_signature = self._action_signature(action)
        action, locator_rebound, ref_error = self._bind_current_ref(action, original_signature)
        if ref_error:
            return self._failure("browser_unknown_ref", ref_error,
                                 requires_reobservation=True)

        login_target_error = self._login_target_link_error(action)
        if login_target_error:
            return self._failure(
                login_target_error[0], login_target_error[1],
                preferred_ref=login_target_error[2], requires_reobservation=True,
            )

        # Rebinding changes the semantic target. Recompute the signature and
        # risk from the new observed control before duplicate-action checks.
        backend_arguments = dict(action.arguments)
        tab_index: int | None = None
        if action.action in {"switch_tab", "close_tab"}:
            if action.action == "close_tab" and self._tab_snapshot_id and len(self._tab_records) <= 1:
                return self._failure("browser_last_tab_cannot_close",
                                     "The final browser Tab cannot be closed.")
            tab_index, tab_error = self._resolve_tab_index(action.arguments)
            if tab_error:
                return self._failure("browser_tab_reference_invalid", tab_error,
                                     requires_reobservation=True)
            backend_arguments["index"] = tab_index
        clicked_url = ""
        if action.action == "open_ref_new_tab":
            if not self._tab_snapshot_id:
                return self._failure("browser_tab_observation_required",
                                     "List tabs before opening a new observed link in a separate tab.",
                                     requires_reobservation=False)
            link_url = observed_link_url(self._last_snapshot, str(action.arguments.get("ref") or ""),
                                         base_url=self._current_page_url)
            if not link_url:
                return self._failure("browser_observed_link_missing",
                                     "The selected ref is not an observed HTTP(S) link.",
                                     requires_reobservation=True)
            if self._is_issues_url(link_url) and not self._repository_record_verified():
                return self._failure(
                    "browser_research_record_required_before_issues",
                    "Verify the current repository's main evidence record before opening its Issues page.",
                    requires_reobservation=True,
                )
            if len(self._tab_records) >= self.max_tabs:
                return self._failure("browser_tab_limit_exceeded",
                                     "The browser Tab limit would be exceeded by opening this link.",
                                     max_tabs=self.max_tabs)
            if self.strict_navigation_origins and not is_safe_public_browser_url(link_url):
                return self._failure(
                    "browser_navigation_url_blocked",
                    "The observed link is not a public HTTP(S) target permitted for the real browser.",
                )
            backend_arguments["url"] = link_url
            link_origin = origin_from_url(link_url)
            if link_origin:
                self._allowed_origins.add(link_origin)
            if (self.repository_research_only
                    and self._is_github_repository_url(self._current_page_url)):
                linked_host = str(urlsplit(link_url).hostname or "").casefold().rstrip(".")
                if linked_host in {"github.com", "www.github.com"} \
                        and not self._is_github_repository_url(link_url):
                    return self._failure(
                        "browser_research_repository_navigation_blocked",
                        "Stay on the observed repository and its same-repository pages while collecting evidence; "
                        "do not open a GitHub profile, site root, or generic navigation page.",
                        requires_reobservation=True,
                    )
        elif action.action == "click_ref":
            # A trusted observed link is allowed to cross origin once; an
            # arbitrary JavaScript redirect is not.  The post-action snapshot
            # still checks the final origin against this bounded set.
            clicked_url = observed_link_url(
                self._last_snapshot,
                str(action.arguments.get("ref") or ""),
                base_url=self._current_page_url,
            )
            if self._is_issues_url(clicked_url) and not self._repository_record_verified():
                return self._failure(
                    "browser_research_record_required_before_issues",
                    "Verify the current repository's main evidence record before opening its Issues page.",
                    requires_reobservation=True,
                )
            if self.strict_navigation_origins and clicked_url and not is_safe_public_browser_url(clicked_url):
                return self._failure(
                    "browser_navigation_url_blocked",
                    "The observed link is not a public HTTP(S) target permitted for the real browser.",
                )
            clicked_origin = origin_from_url(clicked_url)
            if clicked_origin:
                self._allowed_origins.add(clicked_origin)
            if (self.search_discovery_required
                    and str(urlsplit(self._current_page_url).hostname or "").casefold().rstrip(".")
                    in self._SEARCH_FALLBACK_HOSTS
                    and clicked_url):
                clicked_target = urlsplit(clicked_url)
                clicked_host = str(clicked_target.hostname or "").casefold().rstrip(".")
                clicked_path_parts = [part for part in str(clicked_target.path or "").split("/") if part]
                if (clicked_host in {"github.com", "www.github.com"}
                        and not self._is_github_repository_url(clicked_url)):
                    return self._failure(
                        "browser_navigation_requires_observed_link",
                        "This research task requires opening an observed repository result, not the GitHub site root.",
                    )
            if (self.repository_research_only
                    and self._is_github_repository_url(self._current_page_url)
                    and clicked_url):
                clicked_host = str(urlsplit(clicked_url).hostname or "").casefold().rstrip(".")
                if (clicked_host in {"github.com", "www.github.com"}
                        and not self._is_github_repository_url(clicked_url)):
                    return self._failure(
                        "browser_research_repository_navigation_blocked",
                        "Stay on the observed repository and its same-repository pages while collecting evidence; "
                        "do not click a GitHub profile, site root, or generic navigation page.",
                        requires_reobservation=True,
                    )
        signature = self._action_signature(BrowserAction(action.action, backend_arguments))
        if action.action in {"click_ref", "fill_ref", "select_ref", "press_key"}:
            risk_failure = self._risk_failure_for_action(action)
            if risk_failure:
                return self._failure(*risk_failure)
            if self._is_high_risk_action(action):
                # A high-risk confirmation is single-use.  Consume it before
                # the MCP call so a transport failure cannot make a later
                # replay silently inherit the user's approval.
                self._authorized_high_risk = None
        if self._last_state_action_signature == signature:
            self._repeated_state_action_count += 1
            if self._repeated_state_action_count >= 2:
                return self._handoff_failure()
            return self._failure(
                "browser_repeated_state_action",
                "This browser state action was already completed. Use the latest observation "
                "to choose the next semantic target instead of repeating it.",
                requires_reobservation=True,
            )
        if self._last_no_progress_signature == signature and not self._reobservation_required:
            return self._handoff_failure()

        # A re-render may replace the input ref, but the task is still in the
        # choose-options stage and must not accept another search input. Keep
        # this after duplicate detection so an identical replay is classified
        # as a replay, while a new value is classified as an input-stage lock.
        if action.action == "fill_ref" and self._interaction_stage in {
            "waiting_for_options", "ready_to_choose",
        }:
            return self._failure(
                "browser_input_stage_locked",
                "The search input has already been filled. Observe or choose the current option; "
                "do not fill the same search field again.",
                interaction_stage=self._interaction_stage,
                requires_reobservation=self._interaction_stage == "waiting_for_options",
            )

        # Capture the semantic role before the action.  Autocomplete widgets
        # commonly re-render and replace the input ref immediately after the
        # fill call, so checking only the post-action snapshot can miss the
        # stage lock and permit a repeated search.
        autocomplete_input = self._is_autocomplete_input(action)
        before = self._state_fingerprint
        self._remember_locator(action)
        self._emit_state_activity("begin", action.action)
        result: dict[str, Any] = {"ok": False, "failure_kind": "browser_backend_failure"}
        try:
            result = self.backend.call(action.action, backend_arguments)
        except Exception as exc:
            failure_kind = self._classify_backend_failure_kind(str(exc))
            result = {
                "ok": False,
                "failure_kind": failure_kind,
                "error": self._backend_failure_message(failure_kind, phase="action"),
            }
        finally:
            self._emit_state_activity("end", action.action)
        if not isinstance(result, dict):
            return self._backend_failure({
                "ok": False,
                "failure_kind": "browser_backend_failure",
                "error": "The local browser backend returned an invalid action result.",
            })
        if not result.get("ok"):
            if action.action == "navigate" and navigation_key:
                if navigation_attempt >= self.max_navigation_retries:
                    result = {
                        **result,
                        "failure_kind": "browser_navigation_retry_exhausted",
                        "error": "The navigation target failed after the bounded retry budget.",
                        "retries": self.max_navigation_retries,
                    }
            return self._backend_failure(result)
        if action.action == "navigate" and navigation_key:
            self._navigation_attempts.pop(navigation_key, None)
        if action.action in {"switch_tab", "open_ref_new_tab", "close_tab"}:
            self._tab_generation += 1
            self._tab_id = f"tab-{self._tab_generation}"
            # Locator learning is scoped to a tab.  A same-label control in a
            # newly selected tab is a different semantic target, so allowing
            # the previous tab's cache to rebind it would bypass the fresh
            # observation boundary.
            self._learned_locators.clear()
            self._rebound_action_signatures.clear()
            if action.action == "switch_tab" and tab_index is not None:
                # Switching the active page does not change Tab topology or
                # backend indices. Keep the latest tab refs valid; only the
                # page observation/ref generation is replaced below.
                for record in self._tab_records:
                    record["current"] = int(record.get("index") or -1) == tab_index
            else:
                self._invalidate_tab_snapshot()
            if action.action == "open_ref_new_tab":
                self._last_tab_count += 1
            elif action.action == "close_tab":
                self._last_tab_count = max(1, self._last_tab_count - 1)
        if action.action == "scroll":
            self._scroll_count += 1
        after = self._observe({})
        if not after.get("ok"):
            return {**after, "requires_reobservation": True}
        if self.search_discovery_required and action.action in {
                "navigate", "click_ref", "open_ref_new_tab"}:
            final_url = urlsplit(self._current_page_url)
            final_host = str(final_url.hostname or "").casefold().rstrip(".")
            if (final_host in {"github.com", "www.github.com"}
                    and not self._is_github_repository_url(self._current_page_url)):
                return self._failure(
                    "browser_navigation_requires_observed_link",
                    "The browser ended on the GitHub site root. Re-observe and open a specific observed repository result.",
                    requires_reobservation=True,
                )

        # Dynamic autocomplete controls frequently render their listbox after
        # the input call returns.  Wait inside the trusted runtime so the model
        # cannot respond by typing into the same search field again.  The wait
        # is bounded and is not counted as another model semantic action.
        if autocomplete_input:
            self._last_fill_value = str(action.arguments.get("value", action.arguments.get("text", "")))
            self._set_interaction_stage("waiting_for_options")
            for _ in range(8):
                if self._has_options(after.get("content")):
                    self._set_interaction_stage("ready_to_choose")
                    break
                try:
                    waited = self.backend.call("wait", {"seconds": 0.15})
                except Exception:
                    break
                if not isinstance(waited, dict) or not waited.get("ok"):
                    break
                refreshed = self._observe({})
                if not refreshed.get("ok"):
                    break
                after = refreshed
                if self._has_options(after.get("content")):
                    self._set_interaction_stage("ready_to_choose")
                    break
        changed = bool(after.get("observation_id") and self._state_fingerprint != before)
        if changed:
            self._last_no_progress_signature = None
            self._no_progress_count = 0
            self._last_state_action_signature = signature
            self._repeated_state_action_count = 0
            self._reobservation_required = False
            self._snapshot_recovery_attempts.clear()
            self._tab_recovery_attempts.clear()
            self._extractions.clear()
            self._evidence_failure_count = 0
            if action.action == "click_ref" and self._has_extractable_result(after.get("content")):
                self._set_interaction_stage("result_ready")
            if action.action == "click_ref":
                current = urlsplit(self._current_page_url)
                host = str(current.hostname or "").casefold()
                path = str(current.path or "").casefold()
                if host in {
                        "auth.openai.com", "auth0.openai.com", "platform.openai.com",
                        "chatgpt.com", "www.chatgpt.com",
                } and any(marker in path for marker in ("login", "authorize", "auth")):
                    self._login_flow_verified = True
                # A real login anchor commonly uses ``target=_blank``.  In
                # that case Playwright MCP may leave the original landing
                # page active even though the click successfully created the
                # observed login Tab.  The href was bound to the current
                # snapshot and the click is already behind the high-risk
                # confirmation gate, so it is safe to use that trusted href
                # as completion evidence when the resulting browser state
                # changed.
                if clicked_url:
                    clicked = urlsplit(clicked_url)
                    clicked_host = str(clicked.hostname or "").casefold()
                    clicked_path = str(clicked.path or "").casefold()
                    if clicked_host in {
                            "auth.openai.com", "auth0.openai.com",
                            "platform.openai.com", "chatgpt.com",
                            "www.chatgpt.com",
                    } and any(marker in clicked_path for marker in (
                            "login", "authorize", "auth")):
                        self._login_flow_verified = True
            tab_refresh: dict[str, Any] = {}
            if action.action in {"open_ref_new_tab", "close_tab"}:
                # Topology changes invalidate every previous tab_ref. Refresh
                # the opaque listing inside the same semantic action result so
                # the model can continue with fresh refs without spending an
                # additional round on a bare list_tabs call.
                listed = self._list_tabs({"observation_id": self._observation_id})
                if listed.get("ok"):
                    tab_refresh = {
                        "tab_snapshot_id": listed.get("tab_snapshot_id"),
                        "tabs": listed.get("tabs") or [],
                        "tab_count": listed.get("tab_count", self._last_tab_count),
                        "tab_listing_refreshed": True,
                    }
                else:
                    tab_refresh = {
                        "tab_listing_refreshed": False,
                        "tab_listing_failure_kind": str(
                            listed.get("failure_kind") or "browser_tab_list_failed"
                        ),
                    }
            return {**result, "ok": True, "state_changed": True,
                    "observation_id": self._observation_id,
                    "content": after.get("content"), "content_trust": "untrusted_page_data",
                    "candidates": after.get("candidates") or [],
                    "locator_rebound": locator_rebound,
                    "tabs_invalidated": action.action in {"open_ref_new_tab", "close_tab"},
                    "interaction_stage": self._interaction_stage,
                    "login_flow_verified": self._login_flow_verified,
                    **tab_refresh}

        self._no_progress_count += 1
        if self._no_progress_count >= 2:
            return self._handoff_failure()
        self._last_no_progress_signature = signature
        self._last_state_action_signature = None
        self._repeated_state_action_count = 0
        self._reobservation_required = False
        return {"ok": False, "state_changed": False,
                "failure_kind": "browser_no_progress",
                "error": "The browser action completed without an observable page change.",
                "requires_reobservation": True,
                "observation_id": self._observation_id,
                "content": after.get("content"),
                "content_trust": "untrusted_page_data",
                "locator_rebound": locator_rebound,
                "interaction_stage": self._interaction_stage}

    def _resolve_tab_index(self, arguments: dict[str, Any]) -> tuple[int | None, str | None]:
        tab_ref = str(arguments.get("tab_ref") or "").strip()
        tab_snapshot_id = str(arguments.get("tab_snapshot_id") or "").strip()
        if tab_ref or tab_snapshot_id:
            if not tab_ref or tab_snapshot_id != self._tab_snapshot_id:
                return None, "A fresh tab_snapshot_id and tab_ref pair is required."
            record = self._tab_refs.get(tab_ref)
            if not record:
                return None, "The tab_ref is stale or was not present in the latest tab listing."
            index = record.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                return None, "The observed tab reference has no valid backend index."
            return index, None
        index = arguments.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return None, "A non-negative legacy tab index is required when opaque tab refs are absent."
        return index, None

    def _invalidate_tab_snapshot(self) -> None:
        self._tab_snapshot_id = ""
        self._tab_refs.clear()
        self._tab_records.clear()

    def _bind_current_ref(self, action: BrowserAction, signature: str) -> tuple[BrowserAction, bool, str | None]:
        """Validate a ref against the latest snapshot and rebind it once if learned."""
        if action.action not in {"click_ref", "fill_ref", "select_ref", "press_key", "extract", "open_ref_new_tab"}:
            return action, False, None
        ref = str(action.arguments.get("ref") or "")
        candidates = parse_snapshot_candidates(self._last_snapshot)
        if any(candidate.ref == ref for candidate in candidates):
            return action, False, None
        if signature in self._rebound_action_signatures:
            return action, False, "The semantic ref is not present in the latest observation after one automatic rebind."
        descriptor = self._learned_locators.get(action.action) or self._learned_locators.get("click_ref")
        if descriptor is None:
            return action, False, "The semantic ref was not present in the latest observation; observe again."
        variables = {}
        if action.action == "fill_ref":
            variables = {"query": str(action.arguments.get("value", action.arguments.get("text", "")))}
        elif action.action in {"click_ref", "open_ref_new_tab"}:
            variables = {"target_label": str(action.arguments.get("target_label") or "")}
        rebound = resolve_locator(descriptor, candidates, variables, key=self.locator_key)
        if not rebound:
            return action, False, "The old semantic ref could not be uniquely rebound from the latest observation."
        self._rebound_action_signatures.add(signature)
        return BrowserAction(action.action, {**action.arguments, "ref": rebound}), True, None

    @staticmethod
    def _has_options(content: Any) -> bool:
        return any(candidate.role in {"option", "listbox"}
                   for candidate in parse_snapshot_candidates(content))

    @staticmethod
    def _has_extractable_result(content: Any) -> bool:
        return any(candidate.role in {"article", "region", "main", "section", "group", "generic"}
                   for candidate in parse_snapshot_candidates(content))

    def _is_autocomplete_input(self, action: BrowserAction) -> bool:
        if action.action != "fill_ref":
            return False
        ref = str(action.arguments.get("ref") or "")
        candidate = next((item for item in parse_snapshot_candidates(self._last_snapshot)
                          if item.ref == ref), None)
        return bool(candidate and candidate.role in {"combobox", "searchbox"})

    def _emit_state_activity(self, phase: str, action: str) -> None:
        if self.on_state_action is None:
            return
        try:
            self.on_state_action(phase, action)
        except Exception:
            # The activity indicator is advisory and cannot change browser
            # execution semantics.
            return

    def _extract(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ref = str(arguments.get("ref") or "")
        supplied = str(arguments.get("observation_id") or "")
        if not self._observation_id or supplied != self._observation_id:
            return self._failure("stale_browser_observation",
                                 "Fresh browser observation is required before extraction.")
        if self._reobservation_required:
            return self._failure(
                "browser_reobservation_required",
                "Fresh browser observation is required before retrying this evidence action.",
                requires_reobservation=True,
            )
        requested_fields = {
            str(field).strip().casefold().replace("-", "_").replace(" ", "_")
            for field in (arguments.get("fields") or ())
        }
        current_host = str(urlsplit(self._current_page_url).hostname or "").casefold().rstrip(".")
        repository_fields = {
            "stars", "language", "updated_at", "installation", "mcp", "memory",
            "multi_agent", "tool_calling",
        }
        if (self.search_discovery_required
                and (current_host in self._SEARCH_FALLBACK_HOSTS
                     or current_host in {"github.com", "www.github.com"})
                and ((current_host in {"github.com", "www.github.com"}
                     and not self._is_github_repository_url(self._current_page_url))
                     or current_host in self._SEARCH_FALLBACK_HOSTS)
                and requested_fields.intersection(repository_fields)):
            self._research_relocation_required = True
            return self._evidence_failure(
                "browser_research_repository_page_required",
                "Repository evidence must be collected from an observed repository page, not a search result or site root.",
                requires_reobservation=True,
            )
        signature = self._evidence_signature("extract", arguments)
        if (self._last_extract_signature == signature
                and self._last_extract_observation_id == self._observation_id):
            return self._failure(
                "browser_evidence_loop",
                "This ref-bound extraction was already attempted for the current observation. "
                "Choose a different ref or obtain a fresh observation before retrying.",
                requires_reobservation=True,
            )
        if (self._has_evidence_state_signature
                and self._last_evidence_state_signature == self._last_state_action_signature):
            return self._failure(
                "browser_evidence_requires_state_change",
                "Structured evidence is already available for this page state. "
                "Move to the next required page or state action before extracting again.",
                requires_reobservation=False,
            )
        self._last_extract_signature = signature
        self._last_extract_observation_id = self._observation_id
        self._remember_locator(BrowserAction("extract", arguments))
        if isinstance(self.backend, PlaywrightMCPBackend):
            local_refs = [ref]
            parent_ref = self._semantic_parent_ref(ref)
            if parent_ref and parent_ref not in local_refs:
                local_refs.append(parent_ref)
            for local_ref in local_refs:
                local_content = self._snapshot_subtree_content(local_ref)
                if local_content is None:
                    continue
                _parsed, local_extraction, local_verification = self._attest_extraction(
                    local_content, local_ref, arguments.get("fields") or [],
                )
                if local_verification["passed"]:
                    local_extraction["observation_id"] = self._observation_id
                    self._evidence_failure_count = 0
                    self._last_evidence_state_signature = self._last_state_action_signature
                    self._has_evidence_state_signature = True
                    self._set_interaction_stage("evidence_ready")
                    self._extractions.append(local_extraction)
                    self._evidence_ledger.add(
                        kind="ref_extract", tab_id=self._tab_id,
                        observation_id=self._observation_id,
                        source_url=self._current_page_url,
                        fields=local_extraction.get("fields")
                        if isinstance(local_extraction, dict) else {},
                        supporting_text=str(local_extraction.get("reason") or "")
                        if isinstance(local_extraction, dict) else "",
                    )
                    return {
                        "ok": True,
                        "extraction": local_extraction,
                        "verification": local_verification,
                        "observation_id": self._observation_id,
                        "content_trust": "untrusted_page_data",
                        "execution_source": "snapshot_ref_subtree",
                        "locator_rebound": local_ref != ref,
                        "state_changed": False,
                    }
            page_fallback = self._github_page_snapshot_extraction(ref, list(arguments.get("fields") or []))
            if page_fallback is not None:
                return page_fallback
        try:
            result = self.backend.call("extract", arguments)
        except Exception:
            result = {"ok": False, "failure_kind": "browser_backend_failure",
                      "error": "The local browser backend failed while extracting evidence."}
        if not isinstance(result, dict):
            result = {"ok": False, "failure_kind": "browser_backend_failure",
                      "error": "The local browser backend returned an invalid extraction result."}
        if not result.get("ok"):
            return self._evidence_failure(
                str(result.get("failure_kind") or "browser_tool_failure"),
                "The requested page ref could not be extracted. Obtain a fresh observation and relocate once.",
            )
        fields = arguments.get("fields") or []
        parsed, extraction, verification = self._attest_extraction(
            result.get("content", result), ref, fields,
        )
        if not verification["passed"] and not self._extract_parent_rebind_attempted:
            parent_ref = self._semantic_parent_ref(ref)
            if parent_ref and parent_ref != ref:
                self._extract_parent_rebind_attempted = True
                rebound_arguments = {
                    "ref": parent_ref,
                    "fields": list(fields),
                    "observation_id": self._observation_id,
                }
                try:
                    rebound_result = self.backend.call("extract", rebound_arguments)
                except Exception:
                    rebound_result = {"ok": False}
                if isinstance(rebound_result, dict) and rebound_result.get("ok"):
                    rebound_parsed, rebound_extraction, rebound_verification = self._attest_extraction(
                        rebound_result.get("content", rebound_result), parent_ref, fields,
                    )
                    if rebound_verification["passed"]:
                        self._remember_locator(BrowserAction("extract", rebound_arguments))
                        self._evidence_failure_count = 0
                        self._last_evidence_state_signature = self._last_state_action_signature
                        self._has_evidence_state_signature = True
                        self._set_interaction_stage("evidence_ready")
                        self._extractions.append(rebound_extraction)
                        self._evidence_ledger.add(
                            kind="ref_extract", tab_id=self._tab_id,
                            observation_id=self._observation_id,
                            source_url=self._current_page_url,
                            fields=rebound_extraction.get("fields") if isinstance(rebound_extraction, dict) else {},
                            supporting_text=str(rebound_extraction.get("reason") or "")
                            if isinstance(rebound_extraction, dict) else "",
                        )
                    return {
                            **rebound_result,
                            "ok": True,
                            "extraction": rebound_extraction,
                            "verification": rebound_verification,
                            "observation_id": self._observation_id,
                            "content_trust": "untrusted_page_data",
                            "state_changed": False,
                            "locator_rebound": True,
                        }
        page_fallback = self._github_page_snapshot_extraction(ref, list(fields))
        if page_fallback is not None:
            return page_fallback
        native = {
            "source": "deskorb_ref_subtree",
            "ref": ref,
            "fields": extraction.get("fields") if isinstance(extraction, dict) else {},
            "matched_fields": extraction.get("matched_fields") if isinstance(extraction, dict) else 0,
            "observed_chars": extraction.get("observed_chars") if isinstance(extraction, dict) else 0,
            "trusted_ref": bool(isinstance(extraction, dict) and extraction.get("trusted_ref")),
            "observation_id": self._observation_id,
        }
        if not verification["passed"]:
            return self._evidence_failure(
                "browser_evidence_insufficient",
                "The selected page ref did not provide all requested structured fields. "
                "Obtain a fresh observation and relocate once; do not repeat the same ref.",
                extraction=extraction,
                verification=verification,
                observation_id=self._observation_id,
                state_changed=False,
            )
        extraction["observation_id"] = self._observation_id
        self._evidence_failure_count = 0
        self._last_evidence_state_signature = self._last_state_action_signature
        self._has_evidence_state_signature = True
        self._set_interaction_stage("evidence_ready")
        self._extractions.append(extraction)
        self._evidence_ledger.add(
            kind="ref_extract", tab_id=self._tab_id,
            observation_id=self._observation_id,
            source_url=self._current_page_url,
            fields=extraction.get("fields") if isinstance(extraction, dict) else {},
            supporting_text=str(extraction.get("reason") or "") if isinstance(extraction, dict) else "",
        )
        return {**result, "ok": True, "extraction": extraction,
                "verification": verification, "observation_id": self._observation_id,
                "content_trust": "untrusted_page_data",
                "state_changed": False}

    def _snapshot_subtree_content(self, ref: str) -> list[dict[str, str]] | None:
        """Build a bounded local ref view from the latest trusted snapshot.

        Playwright MCP already returned the current accessibility snapshot with
        an observation token.  For the real adapter, parsing the observed
        subtree locally avoids a second large MCP extraction round-trip (which
        can stall on long README pages) while retaining the same ref and
        observation binding.
        """
        lines = subtree_lines(self._last_snapshot, ref)
        if not lines:
            return None
        page_url = safe_http_url(self._current_page_url, strip_query=True)
        text = "### Page\n"
        if page_url:
            text += f"- Page URL: {page_url}\n"
        text += "### Snapshot\n```yaml\n" + "\n".join(lines[:512]) + "\n```"
        return [{"type": "text", "text": text[:128_000]}]

    def _attest_extraction(self, content: Any, ref: str,
                           fields: list[Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Parse and attest one ref subtree without widening page-data trust."""
        parsed = parse_ref_snapshot(content, ref, fields)
        native = {
            "source": "deskorb_ref_subtree",
            "ref": ref,
            "fields": parsed.get("fields") if isinstance(parsed, dict) else {},
            "matched_fields": parsed.get("matched_fields") if isinstance(parsed, dict) else 0,
            "observed_chars": parsed.get("observed_chars") if isinstance(parsed, dict) else 0,
            "trusted_ref": bool(isinstance(parsed, dict) and parsed.get("trusted_ref")),
            "observation_id": self._observation_id,
        }
        attested = self._trusted_extractor.extract(
            {"extraction": native},
            {"ref": ref, "fields": fields},
            source_tool="browser_extract_ref",
        ).safe_dict()
        extraction = {**parsed, **attested, "observation_id": self._observation_id}
        if isinstance(extraction.get("fields"), dict):
            extraction["fields"] = normalize_evidence_fields(extraction["fields"])
            capability_fields = {"mcp", "memory", "multi_agent", "tool_calling"}
            for field in fields:
                canonical = str(field).strip().casefold().replace("-", "_").replace(" ", "_")
                if canonical in capability_fields and not str(extraction["fields"].get(canonical) or "").strip():
                    extraction["fields"][canonical] = "unknown"
            if any(str(field).strip().casefold().replace("-", "_").replace(" ", "_")
                   in capability_fields for field in fields):
                extraction["matched_fields"] = max(
                    int(extraction.get("matched_fields") or 0),
                    sum(bool(str(extraction["fields"].get(str(field).strip().casefold().replace("-", "_").replace(" ", "_")) or "").strip())
                        for field in fields),
                )
        verification = {
            "passed": bool(attested.get("trusted_ref") and
                            extraction.get("matched_fields") == len(fields)),
            "kind": "browser_structured_verification",
            "matched_fields": int(extraction.get("matched_fields") or 0),
            "required_fields": len(fields),
            "missing_fields": [
                str(field) for field in fields
                if not str((extraction.get("fields") or {}).get(str(field)) or "").strip()
            ],
        }
        return parsed, extraction, verification

    def _github_page_snapshot_extraction(self, ref: str, fields: list[Any]) -> dict[str, Any] | None:
        """Build bounded GitHub evidence from the current observed page.

        GitHub renders repository metadata, README content, and capability
        mentions in separate accessibility subtrees.  Requiring every field
        to live under one ref makes real repository research fail even when
        the current public page contains the evidence.  This fallback stays
        fail-closed: it only reads the current observed GitHub repository
        snapshot, derives no network data, and emits ``unknown`` only for the
        four capability fields when the bounded page scan found no claim.
        """
        current_url = safe_http_url(self._current_page_url, strip_query=True)
        parsed_url = urlsplit(current_url)
        host = str(parsed_url.hostname or "").casefold().rstrip(".")
        path_parts = [part for part in str(parsed_url.path or "").split("/") if part]
        if host not in {"github.com", "www.github.com"} or len(path_parts) < 2:
            return None
        if path_parts[0].casefold() in {
            "features", "topics", "settings", "notifications", "login", "marketplace",
        }:
            return None
        requested = [str(field).strip().casefold().replace("-", "_").replace(" ", "_")
                     for field in fields if str(field).strip()]
        if not requested:
            return None

        def flatten(value: Any) -> str:
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                if isinstance(value.get("text"), str):
                    return value["text"]
                if isinstance(value.get("content"), (list, tuple)):
                    return "\n".join(flatten(item) for item in value["content"])
                return ""
            if isinstance(value, (list, tuple)):
                return "\n".join(flatten(item) for item in value)
            return ""

        page_text = flatten(self._last_snapshot)[:256_000]
        values: dict[str, Any] = {}
        if "title" in requested:
            values["title"] = path_parts[1].replace("-", "-")[:500]
        if "url" in requested:
            values["url"] = current_url
        if "source" in requested:
            values["source"] = host.removeprefix("www.")

        star_match = re.search(
            r"(?<![\w.])(\d+(?:[.,]\d+)?\s*[kmb]?)\s+"
            r"(?:stars?\b|users\s+starred\s+this\s+repository\b)",
            page_text, re.IGNORECASE,
        )
        if "stars" in requested and star_match:
            values["stars"] = star_match.group(1)

        language_match = re.search(
            r"(?:programming\s+)?languages?\b\s*[:：]\s*"
            r"([A-Za-z][A-Za-z0-9+#.\- ]{0,40})",
            page_text, re.IGNORECASE,
        )
        if "language" in requested and language_match:
            values["language"] = language_match.group(1).strip().split("\n", 1)[0][:80]
        elif "language" in requested:
            # GitHub can render only the Languages heading in an accessibility
            # snapshot while loading the percentage breakdown lazily.  The
            # absence is not evidence of a language; retain an explicit
            # unknown value so the record remains honest and verifiable.
            values["language"] = "unknown"

        updated_match = re.search(
            r"(?:latest\s+commit|last\s+updated|updated\s+at|last\s+commit|updated)\b"
            r"[^\r\n]{0,240}?(?:on\s+)?((?:[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})|"
            r"(?:\d{4}-\d{2}-\d{2})|(?:\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago))",
            page_text, re.IGNORECASE,
        )
        if "updated_at" in requested and updated_match:
            values["updated_at"] = updated_match.group(1)

        install_match = re.search(
            r"\b(?:pip|uv|poetry|conda|npm|pnpm|yarn|cargo|go)\s+"
            r"(?:install|add|get)\b[^\r\n`]{0,180}",
            page_text, re.IGNORECASE,
        )
        if "installation" in requested and install_match:
            values["installation"] = install_match.group(0).strip().strip("\"'")[:500]

        capability_patterns = {
            "mcp": r"\bmcp\b",
            "memory": r"\bmemory\b",
            "multi_agent": r"\bmulti[ -]?agent\b",
            "tool_calling": r"\btool[ -]?calling\b",
        }
        for field, term in capability_patterns.items():
            if field not in requested:
                continue
            negative = re.search(
                rf"(?:does\s+not\s+support|doesn['’]t\s+support|unsupported|without\s+support)"
                rf".{{0,48}}{term}|{term}.{{0,48}}(?:not\s+supported|unsupported)",
                page_text, re.IGNORECASE | re.DOTALL,
            )
            positive = re.search(
                rf"(?:supports?|supporting|supported|enable[ds]?) .{{0,48}}{term}|"
                rf"{term}.{{0,48}}(?:supports?|supported|available|enabled)",
                page_text, re.IGNORECASE | re.DOTALL,
            )
            if negative:
                values[field] = "no"
            elif positive:
                values[field] = "yes"
            else:
                # The page was explicitly scanned for this requested
                # capability.  Unknown is honest; it is never a negative.
                values[field] = "unknown"

        normalized = normalize_evidence_fields(values)
        if any(not str(normalized.get(field) or "").strip() for field in requested):
            return None
        extraction = {
            "source": "deskorb_github_page_snapshot",
            "ref": str(ref or "")[:80],
            "fields": {field: normalized[field] for field in requested},
            "matched_fields": len(requested),
            "observed_chars": min(len(page_text), 100_000),
            "trusted_ref": True,
            "page_scope": True,
            "reason": "bounded_current_github_page_snapshot",
            "observation_id": self._observation_id,
        }
        verification = {
            "passed": True,
            "kind": "browser_github_page_extraction",
            "matched_fields": len(requested),
            "required_fields": len(requested),
        }
        self._evidence_failure_count = 0
        self._last_evidence_state_signature = self._last_state_action_signature
        self._has_evidence_state_signature = True
        self._set_interaction_stage("evidence_ready")
        self._extractions.append(extraction)
        self._evidence_ledger.add(
            kind="github_page_extract", tab_id=self._tab_id,
            observation_id=self._observation_id, source_url=current_url,
            fields=extraction["fields"], supporting_text=extraction["reason"],
        )
        return {
            "ok": True,
            "extraction": extraction,
            "verification": verification,
            "observation_id": self._observation_id,
            "content_trust": "untrusted_page_data",
            "execution_source": "github_page_snapshot",
            "state_changed": False,
        }

    def _semantic_parent_ref(self, ref: str) -> str | None:
        """Return at most one semantic parent ref from the latest observation."""
        candidates = parse_snapshot_candidates(self._last_snapshot)
        target_index = next((index for index, candidate in enumerate(candidates)
                             if candidate.ref == ref), None)
        if target_index is None:
            return None
        target = candidates[target_index]
        preferred_roles = list(target.parent_roles[-1:])
        allowed_roles = {"article", "region", "main", "section", "group", "generic"}
        if preferred_roles:
            allowed_roles &= {role.casefold() for role in preferred_roles}
        for candidate in reversed(candidates[:target_index]):
            if candidate.ref != ref and candidate.role.casefold() in allowed_roles:
                return candidate.ref
        return None

    def _final_tab_state_verified(self) -> bool:
        """Verify a tab-only terminal contract without requiring fake evidence."""
        tabs = [item for item in self._tab_records if isinstance(item, dict)]
        hosts = [str(urlsplit(str(item.get("url") or "")).hostname or "").casefold().rstrip(".")
                 for item in tabs]
        if self.final_tab_mode == "search_only":
            passed = (
                len(tabs) == 1
                and len(hosts) == 1
                and hosts[0] in {
                    "baidu.com", "www.baidu.com", "bing.com", "www.bing.com", "cn.bing.com",
                    "google.com", "www.google.com", "duckduckgo.com", "www.duckduckgo.com",
                    "search.brave.com", "search.yahoo.com", "yandex.com", "www.yandex.com",
                }
            )
        elif self.final_tab_mode == "github_repositories":
            passed = (
                len(tabs) == 3
                and len(hosts) == 3
                and all(self._is_github_repository_url(item.get("url")) for item in tabs)
            )
        else:
            passed = False
        if self.minimum_tab_pairs:
            passed = passed and (
                self.tab_pair_progress["completed_pairs"] >= self.minimum_tab_pairs
            )
        return bool(passed)

    def _verify(self, arguments: dict[str, Any]) -> dict[str, Any]:
        latest = self._extractions[-1] if self._extractions else {}
        signature = self._evidence_signature("verify", {
            **arguments, "extraction_index": len(self._extractions),
        })
        if (self._last_verify_signature == signature
                and self._last_verify_observation_id == self._observation_id):
            return {
                "ok": False,
                "failure_kind": "browser_evidence_loop",
                "error": "This verification was already attempted for the current evidence.",
                "requires_reobservation": True,
                "observation_id": self._observation_id,
                "state_changed": False,
            }
        self._last_verify_signature = signature
        self._last_verify_observation_id = self._observation_id
        fields = latest.get("fields") if isinstance(latest, dict) else {}
        fields = fields if isinstance(fields, dict) else {}
        if (not latest or str(latest.get("observation_id") or "") != self._observation_id):
            if not latest and self.final_tab_mode != "any":
                passed = self._final_tab_state_verified()
                verification = {
                    "passed": passed,
                    "kind": "final_tab_state",
                    "matched_fields": 0,
                    "required_fields": 0,
                    "matched_items": 0,
                    "minimum_results": self.minimum_results,
                    "tab_count": self._last_tab_count,
                    "evidence_count": len(self._evidence_ledger.records),
                    "confirmation_count": self._confirmation_count,
                    "tab_pair_progress": self.tab_pair_progress,
                }
                self._cache_verified = passed
                if passed:
                    self._set_interaction_stage("verified")
                return {
                    "ok": True,
                    "verified": passed,
                    "verification": verification,
                    "postcondition_kind": "final_tab_state",
                    "postcondition_passed": passed,
                    "extraction": {},
                    "evidence_ledger": self._evidence_ledger.safe_dict(),
                    "observation_id": self._observation_id,
                    "state_changed": False,
                }
            return {
                "ok": True,
                "verified": False,
                "verification": {
                    "passed": False,
                    "kind": "browser_structured_verification",
                    "matched_fields": 0,
                    "required_fields": len(arguments.get("required_fields") or []),
                    "failure_kind": "browser_stale_evidence",
                },
                "failure_kind": "browser_stale_evidence",
                "observation_id": self._observation_id,
                "state_changed": False,
            }
        required = arguments.get("required_fields") or []
        items = latest.get("items") if isinstance(latest, dict) else None
        is_list = isinstance(items, list)
        if is_list:
            passed = bool(latest.get("trusted_ref")) and bool(items)
            minimum_items = max(self.minimum_results, max(0, int(arguments.get("min_items") or 0)))
            passed = passed and len(items) >= minimum_items
            for item in items:
                item_fields = item.get("fields") if isinstance(item, dict) else {}
                item_fields = item_fields if isinstance(item_fields, dict) else {}
                passed = passed and all(str(item_fields.get(name) or "").strip() for name in required)
            unique_by = arguments.get("unique_by") or []
            if unique_by:
                keys = []
                for item in items:
                    item_fields = item.get("fields") if isinstance(item, dict) else {}
                    keys.append("|".join(str(item_fields.get(name) or "").casefold().strip()
                                         for name in unique_by))
                passed = passed and len(keys) == len(set(keys)) and all(keys)
        else:
            passed = bool(latest.get("trusted_ref")) and all(str(fields.get(name) or "").strip()
                                                             for name in required)
        if self.minimum_results:
            passed = passed and len(self._evidence_ledger.records) >= self.minimum_results
        pair_progress = self.tab_pair_progress
        if self.minimum_tab_pairs:
            passed = passed and pair_progress["completed_pairs"] >= self.minimum_tab_pairs
        contains = arguments.get("contains")
        values = [contains] if isinstance(contains, str) else list(contains or [])
        if is_list:
            rendered = " ".join(
                str(value)
                for item in items
                if isinstance(item, dict)
                for value in ((item.get("fields") or {}).values()
                              if isinstance(item.get("fields"), dict) else ())
            )
        else:
            rendered = " ".join(str(value) for value in fields.values())
        passed = passed and all(value in rendered for value in values)
        expected = arguments.get("expected")
        if isinstance(expected, dict):
            passed = passed and all(
                str(fields.get(str(key)) or "").strip() == str(value).strip()
                for key, value in expected.items()
            )
        price = self._number(fields.get("price"))
        if arguments.get("price_min") is not None:
            passed = passed and price is not None and price >= float(arguments["price_min"])
        if arguments.get("price_max") is not None:
            passed = passed and price is not None and price <= float(arguments["price_max"])
        required_evidence = arguments.get("required_evidence") or []
        if required_evidence:
            evidence_fields = {
                str(key)
                for record in self._evidence_ledger.records
                for key, value in record.fields.items()
                if str(value).strip()
            }
            passed = passed and all(str(field) in evidence_fields for field in required_evidence)
        source_priority = [str(item).casefold().strip() for item in arguments.get("source_priority") or ()
                           if str(item).strip()]
        if source_priority:
            priority_fields = [str(item) for item in (required_evidence or required)]
            for field in priority_fields:
                candidates = [record for record in self._evidence_ledger.records
                              if str(record.fields.get(field) or "").strip()]
                ranks = [index for index, source in enumerate(source_priority)
                         if any(source in str(record.source_url).casefold() for record in candidates)]
                passed = passed and bool(ranks)
        required_origins = arguments.get("required_origins") or []
        if required_origins:
            observed_origins = {
                origin_from_url(record.source_url)
                for record in self._evidence_ledger.records
                if record.source_url
            }
            passed = passed and all(
                any(str(required_origin).casefold() in observed for observed in observed_origins)
                for required_origin in required_origins
            )
        if arguments.get("max_tabs") is not None:
            try:
                current_tab_count = self._last_tab_count
                passed = passed and current_tab_count <= int(arguments["max_tabs"])
            except (TypeError, ValueError):
                passed = False
        if arguments.get("required_confirmations") is not None:
            try:
                passed = passed and self._confirmation_count >= int(arguments["required_confirmations"])
            except (TypeError, ValueError):
                passed = False
        final_tabs = arguments.get("final_tabs") or []
        if final_tabs:
            current_urls = [str(item.get("url") or "").casefold()
                            for item in self._tab_records if isinstance(item, dict)]
            passed = passed and all(
                any(str(expected).casefold() in url for url in current_urls)
                for expected in final_tabs
            )
        forbidden_actions = {str(item).casefold() for item in arguments.get("forbidden_actions") or []}
        if forbidden_actions:
            executed_actions = {str(item.get("action") or "").casefold() for item in self._action_log}
            passed = passed and not (forbidden_actions & executed_actions)
        postcondition = str(arguments.get("postcondition") or "structured_fields").strip().lower()
        if postcondition == "tab_changed":
            passed = passed and self._tab_generation > 1
        if postcondition == "origin":
            passed = passed and bool(self._current_page_url)
        verification_kind = postcondition if postcondition in {
            "structured_fields", "element_present", "element_absent", "selection",
            "result_count", "tab_changed", "origin",
        } else "browser_structured_verification"
        matched_fields = (sum(bool(value) for value in fields.values()) if not is_list
                          else sum(
                              bool(value)
                              for item in items
                              if isinstance(item, dict)
                              for value in ((item.get("fields") or {}).values()
                                            if isinstance(item.get("fields"), dict) else ())
                          ))
        verification = {"passed": bool(passed), "kind": verification_kind,
                        "matched_fields": matched_fields,
                        "required_fields": len(required),
                        "matched_items": len(items) if is_list else 0,
                        "minimum_results": self.minimum_results,
                        "tab_count": self._last_tab_count,
                        "evidence_count": len(self._evidence_ledger.records),
                        "confirmation_count": self._confirmation_count}
        verification["tab_pair_progress"] = pair_progress
        self._cache_verified = bool(passed)
        if passed:
            self._set_interaction_stage("verified")
        return {"ok": True, "verified": bool(passed), "verification": verification,
                "postcondition_kind": verification_kind,
                "postcondition_passed": bool(passed),
                "extraction": dict(latest),
                "evidence_ledger": self._evidence_ledger.safe_dict(),
                "observation_id": self._observation_id, "state_changed": False}

    def _evidence_failure(self, failure_kind: str, error: str, **extra: Any) -> dict[str, Any]:
        """Allow one fresh-observation relocation, then hand off the task."""
        self._evidence_failure_count += 1
        if self._evidence_failure_count >= self.max_evidence_failures:
            return self._handoff_failure()
        self._reobservation_required = True
        return {
            "ok": False,
            "failure_kind": failure_kind,
            "error": error,
            "requires_reobservation": True,
            "observation_id": self._observation_id,
            **extra,
        }

    def _handoff_failure(self) -> dict[str, Any]:
        self._preserve_known_navigation_origins()
        self._handoff_required = True
        self._handoff_reason = "browser_no_progress"
        self._observation_id = ""
        self._state_fingerprint = ""
        self._last_snapshot = None
        self._extractions.clear()
        self._last_extract_signature = None
        self._last_extract_observation_id = ""
        self._extract_parent_rebind_attempted = False
        self._last_verify_signature = None
        self._last_verify_observation_id = ""
        self._evidence_failure_count = 0
        self._interaction_stage = "unknown"
        self._last_fill_value = ""
        self._learned_locators.clear()
        self._rebound_action_signatures.clear()
        self._allowed_origin = ""
        self._current_page_url = ""
        self._navigation_attempts.clear()
        self._research_relocation_required = False
        self._authorized_high_risk = None
        self._invalidate_tab_snapshot()
        self._evidence_ledger.clear()
        self._action_log.clear()
        self._cache_verified = False
        self._trusted_extractor.invalidate()
        return {"ok": False, "failure_kind": self._handoff_reason,
                "error": "Browser interaction made no progress; manual handoff is required.",
                "handoff_required": True,
                "handoff_timeout_seconds": self.handoff_timeout_seconds,
                "observation_id": "",
                "browser_session_id": self._browser_session_id,
                "tab_id": self._tab_id,
                "interaction_stage": self._interaction_stage,
                "next_allowed_actions": self.next_allowed_actions,
                "stage_transition_count": self._stage_transition_count}

    def resume_after_handoff(self) -> None:
        self._handoff_required = False
        self._handoff_reason = ""
        self._last_no_progress_signature = None
        self._no_progress_count = 0
        self._last_state_action_signature = None
        self._repeated_state_action_count = 0
        self._reobservation_required = True
        self._observation_id = ""
        self._state_fingerprint = ""
        self._last_snapshot = None
        self._extractions.clear()
        self._last_extract_signature = None
        self._last_extract_observation_id = ""
        self._extract_parent_rebind_attempted = False
        self._last_verify_signature = None
        self._last_verify_observation_id = ""
        self._evidence_failure_count = 0
        self._interaction_stage = "unknown"
        self._last_fill_value = ""
        self._learned_locators.clear()
        self._rebound_action_signatures.clear()
        self._allowed_origin = ""
        self._current_page_url = ""
        self._navigation_attempts.clear()
        self._research_relocation_required = False
        self._authorized_high_risk = None
        self._invalidate_tab_snapshot()
        self._evidence_ledger.clear()
        self._action_log.clear()
        self._cache_verified = False
        self._trusted_extractor.invalidate()

    def _preserve_known_navigation_origins(self) -> None:
        """Keep only configured or already observed origins across handoff."""
        origins = set(self._allowed_origins)
        origins.update(self.allowed_origins)
        for value in (self._allowed_origin, self._current_page_url):
            origin = origin_from_url(value)
            if origin:
                origins.add(origin)
        for record in self._tab_records:
            origin = origin_from_url(record.get("url")) if isinstance(record, dict) else ""
            if origin:
                origins.add(origin)
        self._allowed_origins = origins

    def _should_attach_snapshot_recovery(self, action: BrowserAction,
                                         result: dict[str, Any]) -> bool:
        """Return whether one automatic fresh snapshot can help the model recover."""
        if action.action not in self._SNAPSHOT_RECOVERY_ACTIONS:
            return False
        if not isinstance(result, dict) or result.get("handoff_required"):
            return False
        failure_kind = str(result.get("failure_kind") or "").strip()
        if failure_kind in self._SNAPSHOT_RECOVERY_FAILURES:
            return True
        # Older MCP adapters do not classify a missing locator consistently.
        # Only treat bounded target-resolution messages as recoverable; a
        # generic backend/transport failure must go through MCP recovery.
        if failure_kind != "browser_backend_failure":
            return False
        detail = " ".join(str(result.get(key) or "") for key in ("error", "message")).casefold()
        return any(marker in detail for marker in (
            "target not found", "element not found", "locator", "no such element",
            "not attached", "not visible", "could not resolve", "cannot resolve",
        ))

    @staticmethod
    def _bounded_snapshot_content(content: Any, limit: int) -> Any:
        """Keep recovery observations useful without returning unbounded page data."""
        remaining = max(1024, int(limit))
        if isinstance(content, str):
            return content if len(content) <= remaining else content[:remaining] + "\n[snapshot truncated]"
        if isinstance(content, list):
            bounded: list[Any] = []
            for item in content:
                if remaining <= 0:
                    break
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    text = str(item["text"])
                    if len(text) > remaining:
                        bounded.append({**item, "text": text[:remaining] + "\n[ snapshot truncated ]"})
                        break
                    bounded.append(item)
                    remaining -= len(text)
                else:
                    encoded = repr(item)
                    if len(encoded) <= remaining:
                        bounded.append(item)
                        remaining -= len(encoded)
            return bounded
        if isinstance(content, dict):
            # Playwright MCP normally returns a list of text blocks. Keep the
            # fallback shape stable for lightweight adapters while applying a
            # bounded representation to unexpected payloads.
            return BrowserExecutionSession._bounded_snapshot_content(
                json.dumps(content, ensure_ascii=False, default=str), remaining,
            )
        return str(content)[:remaining]

    def _recovery_candidates(self) -> list[dict[str, Any]]:
        """Project current snapshot controls into bounded model-facing choices."""
        candidates: list[dict[str, Any]] = []
        for candidate in parse_snapshot_candidates(self._last_snapshot)[:32]:
            item: dict[str, Any] = {
                "ref": str(candidate.ref)[:80],
                "role": str(candidate.role)[:40],
                "name": str(candidate.name)[:160],
            }
            href = observed_link_url(
                self._last_snapshot, candidate.ref, base_url=self._current_page_url,
            )
            if href and is_safe_public_browser_url(href):
                item["href"] = href[:500]
            candidates.append(item)
        return candidates

    def _attach_snapshot_recovery(self, action: BrowserAction,
                                  failure: dict[str, Any]) -> dict[str, Any]:
        """Attach one fresh observation after a ref/target resolution failure."""
        ref = str(action.arguments.get("ref") or "").strip()
        page_key = safe_http_url(self._current_page_url, strip_query=True)
        recovery_key = "|".join((action.action, ref, page_key))
        attempts = int(self._snapshot_recovery_attempts.get(recovery_key, 0))
        if attempts >= 1:
            return failure
        self._snapshot_recovery_attempts[recovery_key] = attempts + 1

        observed = self._observe({})
        if not observed.get("ok"):
            return {
                **failure,
                "state_changed": False,
                "recovery": {
                    "mode": "fresh_snapshot",
                    "status": "failed",
                    "failure_kind": str(observed.get("failure_kind") or "browser_snapshot_failed"),
                    "attempt": attempts + 1,
                },
                "snapshot_recovery_attempts": attempts + 1,
            }

        snapshot = self._bounded_snapshot_content(
            observed.get("content"), self._MAX_RECOVERY_SNAPSHOT_CHARS,
        )
        return {
            **failure,
            "ok": False,
            "state_changed": False,
            "requires_reobservation": False,
            "observation_id": self._observation_id,
            "content": snapshot,
            "content_trust": "untrusted_page_data",
            "page_url": safe_http_url(self._current_page_url, strip_query=True),
            "candidates": self._recovery_candidates(),
            "recovery": {
                "mode": "fresh_snapshot",
                "status": "ready",
                "attempt": attempts + 1,
                "observation_id": self._observation_id,
                "page_url": safe_http_url(self._current_page_url, strip_query=True),
                "matched_candidates": self._recovery_candidates(),
            },
            "snapshot_recovery_attempts": attempts + 1,
            "next_allowed_actions": self.next_allowed_actions,
        }

    @staticmethod
    def _should_attach_tab_recovery(action: BrowserAction,
                                    result: dict[str, Any]) -> bool:
        """Return whether a stale/missing Tab reference can be refreshed safely."""
        if action.action not in {"open_ref_new_tab", "switch_tab", "close_tab"}:
            return False
        if not isinstance(result, dict) or result.get("handoff_required"):
            return False
        return str(result.get("failure_kind") or "") in {
            "browser_tab_observation_required", "browser_tab_reference_invalid",
        }

    def _attach_tab_recovery(self, action: BrowserAction,
                             failure: dict[str, Any]) -> dict[str, Any]:
        """Attach one fresh Tab listing without replaying a topology mutation."""
        tab_ref = str(action.arguments.get("tab_ref") or "").strip()
        tab_snapshot_id = str(action.arguments.get("tab_snapshot_id") or "").strip()
        recovery_key = "|".join((action.action, tab_ref, tab_snapshot_id))
        attempts = int(self._tab_recovery_attempts.get(recovery_key, 0))
        if attempts >= 1 or not self._observation_id:
            return failure
        self._tab_recovery_attempts[recovery_key] = attempts + 1
        listed = self._list_tabs({"observation_id": self._observation_id})
        if not listed.get("ok"):
            return {
                **failure,
                "state_changed": False,
                "recovery": {
                    "mode": "fresh_tab_list",
                    "status": "failed",
                    "failure_kind": str(listed.get("failure_kind") or "browser_tab_list_failed"),
                    "attempt": attempts + 1,
                },
                "tab_recovery_attempts": attempts + 1,
            }
        return {
            **failure,
            "ok": False,
            "state_changed": False,
            "requires_reobservation": False,
            "observation_id": self._observation_id,
            "tab_snapshot_id": listed.get("tab_snapshot_id"),
            "tabs": listed.get("tabs") or [],
            "tab_count": listed.get("tab_count", self._last_tab_count),
            "recovery": {
                "mode": "fresh_tab_list",
                "status": "ready",
                "attempt": attempts + 1,
                "observation_id": self._observation_id,
                "tab_snapshot_id": listed.get("tab_snapshot_id"),
                "tabs": listed.get("tabs") or [],
            },
            "tab_recovery_attempts": attempts + 1,
            "next_allowed_actions": self.next_allowed_actions,
        }

    def _failure(self, failure_kind: str, error: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "failure_kind": failure_kind, "error": error,
                "observation_id": self._observation_id,
                "browser_session_id": self._browser_session_id,
                "tab_id": self._tab_id,
                "interaction_stage": self._interaction_stage,
                "next_allowed_actions": self.next_allowed_actions,
                "stage_transition_count": self._stage_transition_count,
                **extra}

    @property
    def interaction_stage(self) -> str:
        return self._interaction_stage

    def _set_interaction_stage(self, stage: str) -> None:
        normalized = str(stage or "unknown")[:40]
        if normalized != self._interaction_stage:
            self._stage_transition_count += 1
            self._interaction_stage = normalized

    @property
    def cache_verified(self) -> bool:
        return self._cache_verified

    def batch_requires_confirmation(self, actions: Any) -> bool:
        """Inspect current observed targets before the runtime approval gate."""
        if not isinstance(actions, list):
            return False
        for item in actions:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "")
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            if action not in {"click_ref", "fill_ref", "select_ref", "press_key"}:
                continue
            if bool(args.get("submit")) or bool(args.get("doubleClick")):
                return True
            ref = str(args.get("ref") or "")
            candidate = next((value for value in parse_snapshot_candidates(self._last_snapshot)
                              if value.ref == ref), None)
            if candidate is not None:
                text = " ".join((candidate.role, candidate.name, *candidate.parent_roles,
                                  *candidate.state_tokens)).lower()
                if any(marker in text for marker in (
                    "send", "submit", "purchase", "buy", "checkout", "delete", "upload", "login", "password",
                    "发送", "提交", "购买", "结算", "删除", "上传", "登录", "密码")):
                    return True
        return False

    def authorize_high_risk_once(self, descriptor: dict[str, Any] | None) -> None:
        """Install one runtime-owned approval for the already-bound target.

        The descriptor is created before the user prompt and is checked again
        after a fresh observation.  This method never broadens the action
        policy: permanently forbidden actions are still rejected by
        ``_risk_failure_for_action``.
        """
        self._authorized_high_risk = dict(descriptor) if isinstance(descriptor, dict) else None
        if self._authorized_high_risk is not None:
            self._confirmation_count += 1

    def confirmation_descriptor(self, actions: Any) -> dict[str, Any]:
        """Return a non-sensitive binding for a pending high-risk browser batch."""
        targets: list[dict[str, str]] = []
        for item in actions if isinstance(actions, list) else ():
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip().lower()
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            ref = str(args.get("ref") or "").strip()
            if not ref:
                continue
            candidate = next((value for value in parse_snapshot_candidates(self._last_snapshot)
                              if value.ref == ref), None)
            if candidate is None:
                targets.append({"action": action, "ref": ref[:80], "role": "", "name_hash": ""})
                continue
            name_hash = hashlib.sha256(candidate.name.encode("utf-8", "replace")).hexdigest()[:20]
            match_count = sum(
                1 for value in parse_snapshot_candidates(self._last_snapshot)
                if value.role == candidate.role and value.name == candidate.name
            )
            targets.append({"action": action, "ref": ref[:80], "role": candidate.role[:40],
                            "name_hash": name_hash, "match_count": str(match_count)})
        material = {
            "origin": origin_from_url(self._current_page_url),
            "observation_id": self._observation_id,
            "targets": targets,
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {"origin": material["origin"], "observation_id": self._observation_id,
                "targets": targets,
                "action_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}

    def confirmation_matches(self, actions: Any, descriptor: dict[str, Any] | None) -> bool:
        if not isinstance(descriptor, dict):
            return True
        current = self.confirmation_descriptor(actions)
        return bool(
            current.get("action_hash") == descriptor.get("action_hash")
            and current.get("origin") == descriptor.get("origin")
            and current.get("targets") == descriptor.get("targets")
            and current.get("observation_id") == descriptor.get("observation_id")
            and all(str(item.get("match_count") or "") == "1"
                    for item in current.get("targets") or () if isinstance(item, dict))
        )

    def confirmation_target_matches(self, actions: Any,
                                    descriptor: dict[str, Any] | None) -> bool:
        """Compare a pending target after a required fresh observation.

        The observation generation is intentionally ignored for this one
        transition.  The caller must immediately create a new descriptor from
        the fresh observation before installing the one-use authorization.
        """
        if not isinstance(descriptor, dict):
            return True
        current = self.confirmation_descriptor(actions)
        return bool(
            current.get("origin") == descriptor.get("origin")
            and current.get("targets") == descriptor.get("targets")
            and all(str(item.get("match_count") or "") == "1"
                    for item in current.get("targets") or () if isinstance(item, dict))
        )

    def cacheable_workflow(self) -> bool:
        actions = {item.get("action") for item in self._action_log if item.get("ok")}
        return self._cache_verified and {"fill_ref", "click_ref", "extract", "verify"}.issubset(actions)

    def cached_workflow_template(self, fields: tuple[str, ...] = ("title", "source")) -> dict[str, Any]:
        template = build_parameterized_search_template(fields=fields)
        for step in template.get("steps", []):
            if not isinstance(step, dict):
                continue
            action = str(step.get("action") or "")
            descriptor = self._learned_locators.get(action)
            if descriptor:
                step["locator"] = descriptor.safe_dict()
        return template

    def resolve_semantic_locator(self, descriptor: LocatorDescriptor,
                                 variables: dict[str, str] | None = None) -> str | None:
        return resolve_locator(
            descriptor,
            parse_snapshot_candidates(self._last_snapshot),
            variables,
            key=self.locator_key,
        )

    def execute_cached_search(self, intent: BrowserTaskIntent,
                              template: dict[str, Any], *, cache_status: str = "hit") -> dict[str, Any]:
        """Replay the bounded read-only search chain without a model request."""
        if str(template.get("kind") or "") != "read_only_search":
            return self._cache_failure("browser_cache_template_invalid", cache_status)
        self.reset()

        def run(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = self.execute([{"action": action, "arguments": arguments}])
            if not result.get("ok"):
                return {
                    **result,
                    "execution_source": "cache",
                    "cache_status": cache_status,
                    "model_fallback": True,
                    "postcondition_passed": False,
                }
            return result

        navigated = run("navigate", {"url": intent.start_url})
        if not navigated.get("ok"):
            return navigated
        observed = run("snapshot", {})
        if not observed.get("ok"):
            return observed
        fill_ref = self._cached_ref(template, "fill_ref", {"query": intent.query})
        if not fill_ref:
            return self._cache_failure("browser_cache_locator_ambiguous", cache_status)
        filled = run("fill_ref", {"ref": fill_ref, "value": intent.query,
                                   "observation_id": self.observation_id})
        if not filled.get("ok"):
            return filled
        click_ref = self._cached_ref(template, "click_ref", {"target_label": intent.target_label})
        if not click_ref:
            return self._cache_failure("browser_cache_locator_ambiguous", cache_status)
        clicked = run("click_ref", {"ref": click_ref, "observation_id": self.observation_id})
        if not clicked.get("ok"):
            return clicked
        extract_ref = self._cached_ref(template, "extract", {})
        if not extract_ref:
            return self._cache_failure("browser_cache_result_not_found", cache_status)
        extracted = run("extract", {"ref": extract_ref, "fields": list(intent.fields),
                                     "observation_id": self.observation_id})
        if not extracted.get("ok"):
            return extracted
        verified = run("verify", {"required_fields": list(intent.fields)})
        if not verified.get("ok") or not verified.get("verification", {}).get("passed"):
            return self._cache_failure("browser_cache_postcondition_failed", cache_status, **verified)
        return {
            **verified,
            "ok": True,
            "verified": True,
            "execution_source": "cache",
            "cache_status": cache_status,
            "model_fallback": False,
            "model_planning_requests": 0,
            "deterministic_steps": self._action_steps,
            "postcondition_passed": True,
            "extraction": extracted.get("extraction"),
        }

    def _cached_ref(self, template: dict[str, Any], action: str,
                    variables: dict[str, str]) -> str | None:
        locator_data: dict[str, Any] = {}
        for step in template.get("steps", []):
            if isinstance(step, dict) and step.get("action") == action:
                if isinstance(step.get("locator"), dict):
                    locator_data = step["locator"]
                break
        role = str(locator_data.get("role") or ("combobox" if action == "fill_ref" else "option"))
        descriptor = LocatorDescriptor(
            role=role,
            # The accessible name/value of a combobox can include the current
            # query. Parameterized replay must rely on its role and current
            # observation, not on the previous query's digest.
            name_digest=("" if action == "fill_ref"
                         else str(locator_data.get("name_digest") or "")),
            placeholder=str(locator_data.get("placeholder") or ""),
            parent_roles=tuple(str(item) for item in locator_data.get("parent_roles") or []),
            state_tokens=tuple(str(item) for item in locator_data.get("state_tokens") or []),
            relative_position=(int(locator_data["relative_position"])
                              if locator_data.get("relative_position") is not None else None),
        )
        ref = self.resolve_semantic_locator(
            descriptor,
            {} if action == "fill_ref" else variables,
        )
        if ref:
            return ref
        if action == "extract":
            for fallback_role in ("article", "region", "main", "section", "group"):
                ref = self.resolve_semantic_locator(LocatorDescriptor(role=fallback_role), {})
                if ref:
                    return ref
        return None

    @staticmethod
    def _cache_failure(failure_kind: str, cache_status: str, **extra: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "failure_kind": failure_kind,
            "execution_source": "cache",
            "cache_status": cache_status,
            "model_fallback": True,
            "model_planning_requests": 0,
            "postcondition_passed": False,
            **extra,
        }

    def _remember_locator(self, action: BrowserAction) -> None:
        if action.action not in {"fill_ref", "click_ref", "open_ref_new_tab", "extract"}:
            return
        ref = str(action.arguments.get("ref") or "")
        if not ref:
            return
        candidate = next((item for item in parse_snapshot_candidates(self._last_snapshot)
                          if item.ref == ref), None)
        if candidate is None:
            return
        placeholder = {
            "fill_ref": "$query", "click_ref": "$target_label",
            "open_ref_new_tab": "$target_label", "extract": "$result_root",
        }[action.action]
        descriptor = locator_for_candidate(
            candidate, key=self.locator_key, placeholder=placeholder
        )
        self._learned_locators[action.action] = descriptor
        if action.action == "open_ref_new_tab":
            self._learned_locators.setdefault("click_ref", descriptor)

    @staticmethod
    def _recovery_hint(error: str) -> str:
        text = str(error or "").lower()
        if "state-changing" in text or "final action" in text:
            return "send_one_state_action_in_a_separate_final_batch"
        if "observation_id" in text:
            return "use_the_latest_observation_id_from_a_fresh_snapshot"
        if "extract" in text:
            return "extract_with_ref_observation_id_and_nonempty_fields"
        if "verify" in text:
            return "verify_with_required_fields_or_contains_after_successful_extract"
        if "wait" in text:
            return "wait_with_seconds_or_ms"
        return "read_the_bounded_error_and_retry_once_with_the_semantic_schema"

    @staticmethod
    def _classify_backend_failure_kind(value: Any) -> str:
        """Map local MCP failures to stable, observation-safe categories."""
        if isinstance(value, dict):
            explicit = str(value.get("failure_kind") or "").strip()
            text = " ".join(str(value.get(key) or "") for key in ("error", "message")).lower()
        else:
            explicit = ""
            text = str(value or "").lower()
        if explicit == "tool_execution_timeout" or any(marker in text for marker in (
                "timed out", "timeout", "deadline was exhausted", "bounded budget")):
            return "tool_execution_timeout"
        if explicit == "browser_mcp_connection_failed" or any(marker in text for marker in (
                "target closed", "browser closed", "page closed", "context closed",
                "connection closed", "disconnected", "broken pipe", "transport",
                "process exited", "server exited", "exited while", "not running",
                "stopped while")):
            return "browser_mcp_connection_failed"
        return explicit or "browser_backend_failure"

    @staticmethod
    def _backend_failure_message(failure_kind: str, *, phase: str) -> str:
        if failure_kind == "tool_execution_timeout":
            return f"The browser {phase} exceeded its bounded execution budget."
        if failure_kind == "browser_mcp_connection_failed":
            return "The local browser MCP connection closed; obtain a fresh browser observation before retrying."
        if phase == "observation":
            return "The local browser backend failed while obtaining a fresh observation."
        return "The local browser backend failed while executing the action."

    def _backend_failure(self, result: dict[str, Any]) -> dict[str, Any]:
        failure_kind = self._classify_backend_failure_kind(result)
        failed = {**result, "ok": False, "failure_kind": failure_kind}
        if failure_kind in self._REOBSERVATION_FAILURES:
            self._reobservation_required = True
            failed["requires_reobservation"] = True
        return failed

    @staticmethod
    def _fingerprint(content: Any) -> str:
        try:
            encoded = json.dumps(content, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            encoded = repr(content).encode("utf-8", errors="replace")
        return hashlib.sha256(encoded).hexdigest()[:24]

    @staticmethod
    def _action_signature(action: BrowserAction) -> str:
        args = {key: value for key, value in action.arguments.items()
                if key != "observation_id"}
        try:
            encoded = json.dumps({"action": action.action, "arguments": args},
                                 ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
        except (TypeError, ValueError):
            encoded = repr((action.action, args))
        return encoded

    @staticmethod
    def _evidence_signature(kind: str, arguments: dict[str, Any]) -> str:
        """Create an in-memory signature for an observation-only evidence call."""
        try:
            return json.dumps({"kind": kind, "arguments": arguments},
                              ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
        except (TypeError, ValueError):
            return repr((kind, arguments))

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None:
            return None
        text = str(value).replace(",", "")
        import re
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None

    def _risk_failure_for_action(self, action: BrowserAction) -> tuple[str, str] | None:
        if not self._is_high_risk_action(action):
            return None
        if self._is_forbidden_high_risk_action(action):
            # Preserve the normal approval boundary for the product/UI.  If a
            # user nevertheless approves this target, the second check below
            # returns the stronger permanent-deny result and the MCP call is
            # still never made.
            if not self._authorized_high_risk_matches(action):
                return (
                    "browser_high_risk_confirmation_required",
                    "This browser action requires confirmation; confirmation cannot authorize this permanently forbidden action.",
                )
            return (
                "browser_forbidden_action",
                "This browser action is permanently forbidden: confirmation cannot authorize submission, purchase, upload, deletion, or credential input.",
            )
        if not self._authorized_high_risk_matches(action):
            return (
                "browser_high_risk_confirmation_required",
                "This browser action requires a fresh, target-bound user confirmation.",
            )
        return None

    def _authorized_high_risk_matches(self, action: BrowserAction) -> bool:
        descriptor = self._authorized_high_risk
        if not isinstance(descriptor, dict):
            return False
        return self.confirmation_matches([{
            "action": action.action,
            "arguments": action.arguments,
        }], descriptor)

    def _candidate_risk_text(self, action: BrowserAction) -> str:
        args = action.arguments
        candidate = next((item for item in parse_snapshot_candidates(self._last_snapshot)
                          if item.ref == str(args.get("ref") or "")), None)
        candidate_text = ""
        if candidate is not None:
            candidate_text = " ".join((candidate.role, candidate.name,
                                        *candidate.parent_roles, *candidate.state_tokens))
        return " ".join((candidate_text, *(str(args.get(key) or "")
                                           for key in ("ref", "value", "element", "button", "label")))).casefold()

    def _login_target_link_error(self, action: BrowserAction) -> tuple[str, str, str] | None:
        """Reject a decorative login button when a real observed link exists.

        Public landing pages often render an inert button and a same-labelled
        anchor.  Treating the first one as the login target causes an approved
        action to appear successful while leaving the browser on the same
        page.  The model must choose the unique observed public link instead;
        this check is deliberately scoped to login-labelled click targets.
        """
        if action.action != "click_ref":
            return None
        ref = str(action.arguments.get("ref") or "")
        candidates = parse_snapshot_candidates(self._last_snapshot)
        target = next((candidate for candidate in candidates if candidate.ref == ref), None)
        if target is None or target.role != "button":
            return None
        target_name = " ".join(target.name.casefold().split())
        if not any(marker in target_name for marker in ("login", "log in", "sign in", "登录", "登入")):
            return None
        for candidate in candidates:
            if candidate.ref == ref or candidate.role != "link":
                continue
            candidate_name = " ".join(candidate.name.casefold().split())
            if not candidate_name or not any(
                    marker in candidate_name
                    for marker in ("login", "log in", "sign in", "登录", "登入")):
                continue
            href = observed_link_url(
                self._last_snapshot, candidate.ref, base_url=self._current_page_url,
            )
            if href and is_safe_public_browser_url(href):
                return (
                    "browser_login_target_requires_observed_link",
                    "A same-labelled observed public login link exists; use that link ref instead of the decorative button.",
                    candidate.ref,
                )
        return None

    def _is_forbidden_high_risk_action(self, action: BrowserAction) -> bool:
        text = self._candidate_risk_text(action)
        forbidden = (
            "send", "submit", "publish", "purchase", "buy", "checkout", "delete", "upload",
            "download", "credential", "password", "username", "email", "发送", "提交", "发布",
            "购买", "结算", "删除", "上传", "下载", "凭据", "密码", "用户名", "邮箱",
        )
        if any(marker in text for marker in forbidden):
            return True
        return bool(action.action == "fill_ref" and
                    (action.arguments.get("submit") or action.arguments.get("doubleClick")))

    def _is_high_risk_action(self, action: BrowserAction) -> bool:
        args = action.arguments
        text = self._candidate_risk_text(action)
        if bool(args.get("submit")) or bool(args.get("doubleClick")):
            return True
        return any(marker in text for marker in (
            "send", "submit", "publish", "purchase", "buy", "checkout", "delete", "upload", "login", "password",
            "credential", "发送", "提交", "发布", "购买", "结算", "删除", "上传", "登录", "密码", "凭据",
        ))


__all__ = ["BrowserExecutionSession", "PlaywrightMCPBackend"]
