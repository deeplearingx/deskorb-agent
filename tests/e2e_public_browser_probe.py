"""Run explicitly opted-in, bounded acceptance checks against public websites.

This script is intentionally separate from the repeatable loopback matrix.  It
uses the configured provider and the isolated Playwright profile, so it is a
manual acceptance check rather than a PR gate. Its JSON output is metrics-only.
Each runtime receives an in-memory task journal, so page text, URLs, prompts,
answers, tool payloads, cookies, and approval tokens are neither written to
stdout/report nor persisted in a runtime task database.
"""
from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL, API_TIMEOUT, MODEL_PROVIDER
from public_browser_scenarios import PublicBrowserScenario, SCENARIOS, selected_scenarios
from task_runtime import InMemoryTaskJournal


_PUBLIC_BROWSER_ACTIONS = frozenset({"navigate", "snapshot", "click_ref", "switch_tab", "extract", "verify", "wait"})
_OBSERVATION_ACTIONS = frozenset({"snapshot", "extract", "verify"})
_PAGE_URL_LINE = re.compile(r"(?im)^\s*-\s+Page URL:\s*(https?://[^\s<>\"']+)")
_SENSITIVE_KEYS = frozenset({
    "answer", "final_answer", "prompt", "input", "messages", "content", "page_text", "url",
    "urls", "title", "titles", "price", "prices", "source", "sources", "tool_calls",
    "tool_payloads", "arguments", "observations", "screenshot", "screenshots", "cookie", "cookies",
    "api_key", "token", "error", "traceback", "profile", "base_url",
})


def _drain(events: Queue) -> list[tuple[str, object]]:
    values = []
    while True:
        try:
            values.append(events.get_nowait())
        except Empty:
            return values


def _approval_token(events: list[tuple[str, object]]) -> str | None:
    for kind, value in events:
        if kind != "approval":
            continue
        match = re.search(r"确认\s+([A-F0-9]{6,})", str(value))
        if match:
            return match.group(1)
    return None


def _domain_allowed(url: object, allowed_domains: tuple[str, ...]) -> bool:
    try:
        host = (urlparse(str(url)).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return False
    return any(host == domain or host.endswith("." + domain) for domain in allowed_domains)


def _browser_result_text(value: object) -> str:
    """Read adapter-owned text containers without retaining page content."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_browser_result_text(value[key]) for key in ("content", "observations", "text", "data")
                           if key in value)
    if isinstance(value, (list, tuple)):
        return "\n".join(_browser_result_text(item) for item in value)
    return ""


def _observed_page_urls(result: object) -> list[str]:
    """Extract only the adapter's explicit Page URL metadata from a result."""
    text = _browser_result_text(result)
    urls: list[str] = []
    for match in _PAGE_URL_LINE.finditer(text):
        url = match.group(1).rstrip(".,);]}")
        if url not in urls:
            urls.append(url)
    return urls


def _refresh_browser_url(runtime: AgentRuntime, result: object) -> list[str]:
    """Track the last observed adapter URL for the next bounded click check."""
    urls = _observed_page_urls(result)
    if urls:
        runtime._browser_current_url = urls[-1]
    return urls


def _current_browser_url(runtime: AgentRuntime | None) -> str:
    return str(getattr(runtime, "_browser_current_url", "") or "").strip()


def _action_from_tool(name: object, arguments: object | None = None) -> str | None:
    lowered = str(name or "").lower()
    if isinstance(arguments, dict) and isinstance(arguments.get("name"), str):
        lowered += " " + str(arguments["name"]).lower()
    if "snapshot" in lowered or "screenshot" in lowered:
        return "snapshot"
    if "extract" in lowered:
        return "extract"
    if "verify" in lowered:
        return "verify"
    if "wait" in lowered:
        return "wait"
    if "tab" in lowered:
        return "switch_tab"
    if "navigate" in lowered:
        return "navigate"
    if "click" in lowered:
        return "click_ref"
    if any(part in lowered for part in ("fill", "type", "press", "key")):
        return "fill_ref"
    if "download" in lowered:
        return "download"
    if "upload" in lowered:
        return "upload"
    if "submit" in lowered:
        return "submit"
    return None


def _is_playwright_browser_tool(runtime: AgentRuntime | None, name: str) -> bool:
    """Recognize only calls routed to the local Playwright browser adapter."""
    if name == "browser_action_batch":
        return True
    mcp = getattr(runtime, "mcp", None)
    if mcp and mcp.owns(name):
        return getattr(mcp, "server_name", lambda _name: None)(name) == "playwright"
    return name.startswith("mcp_playwright_")


def _read_only_navigation_allowed(url: object, allowed_domains: tuple[str, ...]) -> bool:
    """Keep navigations on approved public read routes, not account or transaction pages."""
    if not _domain_allowed(url, allowed_domains):
        return False
    parsed = urlparse(str(url))
    if parsed.scheme.casefold() not in {"http", "https"} or parsed.username or parsed.password:
        return False
    route = (parsed.path + "?" + parsed.query).casefold()
    blocked_markers = ("login", "signin", "account", "auth", "captcha", "verify", "security",
                       "cart", "checkout", "order", "payment", "message", "favorite", "upload", "download")
    return not any(marker in route for marker in blocked_markers)


def _public_browser_policy_failure(runtime: AgentRuntime | None, scenario: PublicBrowserScenario,
                                   name: str, arguments: dict[str, Any]) -> str | None:
    """Reject an unsafe public-browser operation before the runtime executes it."""
    if not _is_playwright_browser_tool(runtime, name):
        return None
    if name == "browser_action_batch":
        actions = arguments.get("actions")
        if not isinstance(actions, list):
            return "forbidden_browser_action"
        candidate_url = _current_browser_url(runtime)
        for item in actions:
            if not isinstance(item, dict):
                return "forbidden_browser_action"
            action = str(item.get("action") or "").lower()
            if action not in _PUBLIC_BROWSER_ACTIONS:
                return "forbidden_browser_action"
            if action == "navigate" and not _read_only_navigation_allowed(
                    (item.get("arguments") or {}).get("url"), scenario.allowed_domains):
                return "unapproved_navigation"
            if action == "navigate":
                candidate_url = str((item.get("arguments") or {}).get("url") or "").strip()
            elif action == "switch_tab":
                # The newly selected tab is unknown until a fresh snapshot.
                candidate_url = ""
            elif action == "click_ref":
                if not _read_only_navigation_allowed(candidate_url, scenario.allowed_domains):
                    return "unapproved_navigation"
                if (scenario.click_requires_evidence and
                        int(getattr(runtime, "_public_candidate_count", 0) or 0) < scenario.minimum_results):
                    return "evidence_required_before_click"
        return None
    action = _action_from_tool(name, arguments)
    if action not in _PUBLIC_BROWSER_ACTIONS:
        return "forbidden_browser_action"
    if action == "navigate" and not _read_only_navigation_allowed(arguments.get("url"), scenario.allowed_domains):
        return "unapproved_navigation"
    if action == "click_ref":
        if not _read_only_navigation_allowed(_current_browser_url(runtime), scenario.allowed_domains):
            return "unapproved_navigation"
        if (scenario.click_requires_evidence and
                int(getattr(runtime, "_public_candidate_count", 0) or 0) < scenario.minimum_results):
            return "evidence_required_before_click"
    return None


def _install_read_only_guard(runtime: AgentRuntime, scenario: PublicBrowserScenario,
                             calls: list[tuple[str, dict[str, Any]]],
                             extractions: list[dict[str, str]],
                             policy_failures: list[str]) -> Any:
    """Guard the production dispatcher and record only bounded in-memory metrics."""
    original_dispatch = runtime._run_local_tool

    def guarded(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not _is_playwright_browser_tool(runtime, name):
            result = {"ok": False, "error": "Public browser acceptance allows only bounded Playwright operations.",
                    "failure_kind": "forbidden_public_tool"}
            calls.append((str(name), dict(arguments) if isinstance(arguments, dict) else {}))
            policy_failures.append("forbidden_public_tool")
            return result
        failure_kind = _public_browser_policy_failure(runtime, scenario, name, arguments)
        if failure_kind:
            result = {"ok": False, "error": "Public browser acceptance policy rejected this operation.",
                    "failure_kind": failure_kind}
            calls.append((str(name), dict(arguments) if isinstance(arguments, dict) else {}))
            policy_failures.append(failure_kind)
            return result
        calls.append((str(name), dict(arguments) if isinstance(arguments, dict) else {}))
        result = original_dispatch(name, arguments)
        if isinstance(result, dict) and result.get("failure_kind"):
            policy_failures.append(str(result["failure_kind"]))
        attempts, untrusted = _collect_batch_evidence(result, extractions)
        runtime._public_extraction_attempts = int(getattr(runtime, "_public_extraction_attempts", 0)) + attempts
        runtime._public_untrusted_extractions = int(getattr(runtime, "_public_untrusted_extractions", 0)) + untrusted
        runtime._public_candidate_count = _candidate_metrics(scenario, extractions)[0]
        observed_urls = _refresh_browser_url(runtime, result)
        if observed_urls and any(
                not _read_only_navigation_allowed(url, scenario.allowed_domains)
                for url in observed_urls):
            return {**(result if isinstance(result, dict) else {}), "ok": False,
                    "error": "A public-browser click navigated outside the approved scenario domains.",
                    "failure_kind": "unapproved_navigation"}
        return result

    runtime._run_local_tool = guarded
    return original_dispatch


def _public_result(run: dict[str, Any]) -> dict[str, Any]:
    """Whitelist public metrics so a future caller cannot leak trace data."""
    allowed = {
        "case_id", "attempt", "outcome", "failure_kind", "total_latency_ms", "first_response_ms",
        "tool_rounds", "approval_used", "approval_count", "needs_task_confirmation", "task_confirmation_once",
        "handoff_present", "handoff_resumed", "handoff_passed", "fresh_observation_after_handoff",
        "completed", "verified", "safety_passed", "evidence_passed", "requires_evidence",
        "candidate_count", "evidence_field_set_count", "official_source_count",
        "extraction_attempts", "untrusted_extraction_count", "browser_action_kinds",
        "browser_action_sequence", "action_steps",
    }
    result = {key: value for key, value in run.items() if key in allowed}
    forbidden = set(result).intersection(_SENSITIVE_KEYS)
    if forbidden:
        raise ValueError("Public browser report attempted to include sensitive metrics")
    return result


def _run_turn(runtime: AgentRuntime, text: str, timeout_seconds: int) -> tuple[bool, str | None]:
    """Interrupt a hung provider/MCP turn without printing its exception text."""
    errors: list[BaseException] = []

    def target() -> None:
        try:
            runtime.run_turn(text, [])
        except BaseException as exc:  # The metric records only a safe category.
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(max(1, int(timeout_seconds)))
    if thread.is_alive():
        try:
            runtime.interrupt()
        except Exception:
            pass
        thread.join(5)
        return False, "timeout"
    return (not errors), (None if not errors else "runtime_error")


def _turn_failure_kind(error_kind: str | None, calls: list[tuple[str, dict[str, Any]]],
                       policy_failures: list[str] | None = None) -> str | None:
    """Expose whether a timeout happened before or after any browser tool ran."""
    for kind in ("forbidden_public_tool", "forbidden_browser_action", "unapproved_navigation",
                 "evidence_required_before_click"):
        if kind in set(policy_failures or ()):
            return kind
    if error_kind != "timeout":
        return error_kind
    return "provider_timeout_before_tools" if not calls else "provider_timeout_after_tools"


def _read_handoff_continue(timeout_seconds: int) -> str | None:
    """Wait for a manual continuation without exposing page or challenge details."""
    values: queue.Queue[str] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            values.put(str(input("Manual verification is waiting. Complete it in the isolated browser, then type '我已完成验证，继续': ")))
        except (EOFError, KeyboardInterrupt):
            values.put("")

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        return values.get(timeout=max(1, int(timeout_seconds)))
    except queue.Empty:
        return None


def _collect_batch_evidence(result: object, extractions: list[dict[str, str]]) -> tuple[int, int]:
    if not isinstance(result, dict):
        return 0, 0
    attempts = 0
    untrusted = 0
    for observation in result.get("observations") or ():
        if not isinstance(observation, dict) or observation.get("action") != "extract":
            continue
        attempts += 1
        extraction = observation.get("extraction")
        fields = extraction.get("fields") if isinstance(extraction, dict) else None
        ref = str(observation.get("ref") or "").strip()
        if isinstance(fields, dict) and ref and extraction.get("trusted_ref") is True:
            extractions.append({**{str(key): str(value) for key, value in fields.items()}, "__ref": ref})
        else:
            untrusted += 1
    return attempts, untrusted


def _candidate_metrics(scenario: PublicBrowserScenario, extractions: list[dict[str, str]]) -> tuple[int, int, int, bool]:
    """Validate card-bound evidence without retaining card contents in the report."""
    valid: list[dict[str, str]] = []
    seen_refs: set[str] = set()
    price_in_range = False
    for fields in extractions:
        ref = str(fields.get("__ref") or "").strip()
        if not ref or ref in seen_refs:
            continue
        seen_refs.add(ref)
        if not all(str(fields.get(field) or "").strip() for field in scenario.required_fields):
            continue
        if any(required.casefold() not in str(fields.get("title") or "").casefold()
               for required in scenario.required_contains):
            continue
        if scenario.evidence_source_domains and not _domain_allowed(
                fields.get("url"), scenario.evidence_source_domains):
            continue
        valid.append(fields)
        if scenario.price_range:
            price_text = str(fields.get("price") or "")
            number = re.search(r"(?:¥|￥|价格\s*[:：]?)\s*(\d+(?:\.\d+)?)|(?<!\d)(\d+(?:\.\d+)?)\s*元", price_text)
            value = next((float(part) for part in number.groups() if part) if number else None, None)
            price_in_range = price_in_range or bool(
                value is not None and scenario.price_range[0] <= value <= scenario.price_range[1])
    canonical_urls = {str(fields.get("url") or "").strip().lower() for fields in valid if fields.get("url")}
    official = sum(_domain_allowed(fields.get("url"), scenario.required_source_domains) for fields in valid)
    passed = len(canonical_urls) >= scenario.minimum_results
    if scenario.price_range:
        passed = passed and price_in_range
    if scenario.required_source_domains:
        passed = passed and official >= 1
    return len(canonical_urls), len(extractions), official, passed


def run_case(scenario: PublicBrowserScenario, *, working_dir: Path, attempt: int = 1,
             timeout_seconds: int = 120, interactive_handoff: bool = True,
             human_resume_timeout_seconds: int = 300) -> dict[str, Any]:
    """Run one live scenario and return a privacy-safe normalized record."""
    started = time.monotonic()
    events: Queue = Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL,
                           working_dir=working_dir, model_provider=MODEL_PROVIDER,
                           task_journal=InMemoryTaskJournal())
    # The semantic batch already covers the bounded public action set.  Avoid
    # injecting the full direct Playwright schema into every provider round.
    runtime._semantic_browser_only = True
    calls: list[tuple[str, dict[str, Any]]] = []
    extractions: list[dict[str, str]] = []
    extraction_attempts = 0
    untrusted_extractions = 0
    policy_failures: list[str] = []
    original_dispatch = _install_read_only_guard(runtime, scenario, calls, extractions, policy_failures)
    runtime._public_candidate_count = 0
    runtime._public_extraction_attempts = 0
    runtime._public_untrusted_extractions = 0

    def record_probe_metrics(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        nonlocal extraction_attempts, untrusted_extractions
        if isinstance(result, dict) and result.get("failure_kind"):
            policy_failures.append(str(result["failure_kind"]))
        attempts, untrusted = _collect_batch_evidence(result, extractions)
        extraction_attempts += attempts
        untrusted_extractions += untrusted
        runtime._public_candidate_count = _candidate_metrics(scenario, extractions)[0]
        runtime._public_extraction_attempts = extraction_attempts
        runtime._public_untrusted_extractions = untrusted_extractions
        calls_value = (str(name), dict(arguments) if isinstance(arguments, dict) else {})
        if not calls or calls[-1] != calls_value:
            calls.append(calls_value)

    # The production probe wraps _run_local_tool. Fixture runtimes use this
    # callback to emulate the production tool_result bookkeeping without
    # requiring the removed legacy dispatcher methods.
    runtime._probe_record = record_probe_metrics

    def sync_metrics() -> None:
        nonlocal extraction_attempts, untrusted_extractions
        extraction_attempts = int(getattr(runtime, "_public_extraction_attempts", 0))
        untrusted_extractions = int(getattr(runtime, "_public_untrusted_extractions", 0))
    observed: list[tuple[str, object]] = []
    handoff_index: int | None = None
    handoff_resumed = False
    failure_kind: str | None = None
    try:
        mcp = getattr(runtime, "mcp", None)
        if not mcp or not bool(getattr(mcp, "is_browser_isolated", lambda: False)()):
            failure_kind = "browser_not_isolated"
            return _public_result(_result(scenario, attempt, started, failure_kind=failure_kind, calls=calls))
        task_prompt = scenario.prompt + (
            " 每次 browser_action_batch 只能包含一个语义动作；navigate、snapshot、wait、extract、verify"
            " 必须分别调用，不能把 snapshot 放在 navigate 同一批次，也不能在同一批次连续提取多个 ref。"
            " 先单独 navigate，下一轮单独 snapshot；若结果尚未出现，下一轮单独 wait，随后再单独 snapshot。"
            " 只从最新 snapshot 中选择实际存在且位于结果条目根节点的 ref；extract 失败时不要重复同一 ref，"
            " 应改用新的结果条目根 ref 或返回明确失败。无需点击即可完成证据收集时不要点击结果链接。"
        )
        ok, error_kind = _run_turn(runtime, task_prompt, timeout_seconds)
        observed.extend(_drain(events))
        sync_metrics()
        if not ok:
            failure_kind = _turn_failure_kind(error_kind, calls, policy_failures)
            return _public_result(_result(scenario, attempt, started, failure_kind=failure_kind, calls=calls))
        token = _approval_token(observed)
        if not token:
            return _public_result(_result(scenario, attempt, started, observed,
                                          failure_kind="approval_missing", calls=calls))
        ok, error_kind = _run_turn(runtime, "确认 " + token, timeout_seconds)
        observed.extend(_drain(events))
        sync_metrics()
        if not ok:
            return _public_result(_result(scenario, attempt, started, observed,
                                          failure_kind=_turn_failure_kind(error_kind, calls, policy_failures), calls=calls,
                                          extraction_attempts=extraction_attempts,
                                          untrusted_extractions=untrusted_extractions))
        handoff_index = len(calls)
        handoff_present = any(kind in {"human_verification", "human_handoff"} for kind, _ in observed)
        if handoff_present:
            if not interactive_handoff:
                return _public_result(_result(scenario, attempt, started, observed,
                                               failure_kind="human_verification_required", calls=calls))
            continuation = _read_handoff_continue(human_resume_timeout_seconds)
            allowed_continuations = {
                runtime.HUMAN_VERIFICATION_CONTINUE,
                "我已完成验证，继续",
                "我已完成选择，继续",
            }
            if continuation not in allowed_continuations:
                return _public_result(_result(scenario, attempt, started, observed,
                                               failure_kind="human_verification_not_resumed", calls=calls))
            handoff_resumed = True
            # The operator can change the browser page while it is handed off.
            # Discard all pre-handoff card evidence; only extraction observed
            # after the continuation's fresh observation may prove acceptance.
            extractions.clear()
            extraction_attempts = 0
            untrusted_extractions = 0
            runtime._public_candidate_count = 0
            handoff_index = len(calls)
            event_count_before_resume = len(observed)
            ok, error_kind = _run_turn(runtime, continuation, timeout_seconds)
            observed.extend(_drain(events))
            sync_metrics()
            if not ok:
                return _public_result(_result(scenario, attempt, started, observed,
                                               handoff_resumed=True,
                                               failure_kind=_turn_failure_kind(error_kind, calls, policy_failures), calls=calls))
            if any(kind in {"human_verification", "human_handoff"}
                   for kind, _ in observed[event_count_before_resume:]):
                return _public_result(_result(scenario, attempt, started, observed, handoff_resumed=True,
                                               failure_kind="human_verification_persisted", calls=calls))
        return _public_result(_evaluate(scenario, attempt, started, observed, calls, extractions,
                                        handoff_index, handoff_resumed, policy_failures,
                                        extraction_attempts, untrusted_extractions))
    except Exception:
        return _public_result(_result(scenario, attempt, started, observed,
                                      failure_kind="probe_error", calls=calls,
                                      extraction_attempts=extraction_attempts,
                                      untrusted_extractions=untrusted_extractions))
    finally:
        runtime._run_local_tool = original_dispatch
        if getattr(runtime, "mcp", None):
            try:
                runtime.mcp.close()
            except Exception:
                pass


def _call_action_kinds(calls: list[tuple[str, dict[str, Any]]] | None) -> list[str]:
    kinds: list[str] = []
    for name, arguments in calls or ():
        if name == "browser_action_batch":
            actions = arguments.get("actions") or ()
            for item in actions:
                action = str(item.get("action") or "") if isinstance(item, dict) else ""
                if action and action not in kinds:
                    kinds.append(action)
            continue
        action = _action_from_tool(name, arguments)
        if action and action not in kinds:
            kinds.append(action)
    return kinds


def _call_action_sequence(calls: list[tuple[str, dict[str, Any]]] | None) -> list[str]:
    """Return every bounded semantic browser action in execution order."""
    sequence: list[str] = []
    for name, arguments in calls or ():
        if name == "browser_action_batch":
            for item in arguments.get("actions") or ():
                if isinstance(item, dict):
                    action = str(item.get("action") or "")
                    if action:
                        sequence.append(action)
            continue
        action = _action_from_tool(name, arguments)
        if action:
            sequence.append(action)
    return sequence


def _result(scenario: PublicBrowserScenario, attempt: int, started: float,
            observed: list[tuple[str, object]] | None = None, *, handoff_resumed: bool = False,
            failure_kind: str | None = None,
            calls: list[tuple[str, dict[str, Any]]] | None = None,
            extraction_attempts: int = 0, untrusted_extractions: int = 0) -> dict[str, Any]:
    events = observed or []
    handoff_present = any(kind in {"human_verification", "human_handoff"} for kind, _ in events)
    approvals = sum(kind == "approval" for kind, _ in events)
    return {
        "case_id": scenario.case_id, "attempt": attempt,
        "outcome": "blocked" if failure_kind and failure_kind.startswith("human_verification") else "failed",
        "failure_kind": failure_kind, "total_latency_ms": round((time.monotonic() - started) * 1000),
        "first_response_ms": None, "tool_rounds": len(calls or ()), "approval_used": approvals == 1,
        "approval_count": approvals, "needs_task_confirmation": True,
        "task_confirmation_once": approvals == 1, "handoff_present": handoff_present,
        "handoff_resumed": handoff_resumed, "handoff_passed": False,
        "fresh_observation_after_handoff": False, "completed": False, "verified": False,
        "safety_passed": (not failure_kind or failure_kind.startswith("human_verification") or
                           failure_kind in {"provider_timeout_before_tools", "evidence_required_before_click"}),
        "evidence_passed": False, "requires_evidence": True, "candidate_count": 0,
        "evidence_field_set_count": 0, "official_source_count": 0,
        "extraction_attempts": extraction_attempts,
        "untrusted_extraction_count": untrusted_extractions,
        "browser_action_kinds": _call_action_kinds(calls),
        "browser_action_sequence": _call_action_sequence(calls),
        "action_steps": len(_call_action_sequence(calls)),
    }


def _evaluate(scenario: PublicBrowserScenario, attempt: int, started: float,
              observed: list[tuple[str, object]], calls: list[tuple[str, dict[str, Any]]],
              extractions: list[dict[str, str]], handoff_index: int | None,
              handoff_resumed: bool, policy_failures: list[str] | None = None,
              extraction_attempts: int = 0, untrusted_extractions: int = 0) -> dict[str, Any]:
    result = _result(scenario, attempt, started, observed, handoff_resumed=handoff_resumed, calls=calls,
                     extraction_attempts=extraction_attempts,
                     untrusted_extractions=untrusted_extractions)
    action_kinds: list[str] = []
    failure_set = set(policy_failures or ())
    forbidden = "forbidden_browser_action" in failure_set
    invalid_domain = "unapproved_navigation" in failure_set
    forbidden_tool = "forbidden_public_tool" in failure_set
    evidence_gate = "evidence_required_before_click" in failure_set
    for name, arguments in calls:
        if not _is_playwright_browser_tool(None, name):
            forbidden_tool = True
            continue
        if name == "browser_action_batch":
            for item in arguments.get("actions") or ():
                if not isinstance(item, dict):
                    forbidden = True
                    continue
                action = str(item.get("action") or "")
                if action not in action_kinds:
                    action_kinds.append(action)
                if action not in _PUBLIC_BROWSER_ACTIONS:
                    forbidden = True
                if action == "navigate":
                    invalid_domain = invalid_domain or not _read_only_navigation_allowed(
                        (item.get("arguments") or {}).get("url"), scenario.allowed_domains)
        else:
            action = _action_from_tool(name, arguments)
            if action and action not in action_kinds:
                action_kinds.append(action)
            if action and action not in _PUBLIC_BROWSER_ACTIONS:
                forbidden = True
            if action == "navigate":
                invalid_domain = invalid_domain or not _read_only_navigation_allowed(arguments.get("url"), scenario.allowed_domains)
    for kind, value in observed:
        if kind != "tool" or not isinstance(value, tuple):
            continue
        action = _action_from_tool(value[0], value[1] if len(value) > 1 else None)
        if action and action not in action_kinds:
            action_kinds.append(action)
        if action and action not in _PUBLIC_BROWSER_ACTIONS:
            forbidden = True
    final_progress = next((value for kind, value in reversed(observed)
                           if kind == "task_progress" and isinstance(value, dict) and value.get("terminal")), {})
    completed = final_progress.get("terminal") == "completed"
    verified = bool(final_progress.get("verified"))
    candidate_count, evidence_sets, official_count, candidate_passed = _candidate_metrics(scenario, extractions)
    post_handoff = action_kinds
    if handoff_index is not None and handoff_resumed:
        post_handoff = []
        for name, arguments in calls[handoff_index:]:
            if name == "browser_action_batch":
                post_handoff.extend(str(item.get("action") or "") for item in arguments.get("actions") or () if isinstance(item, dict))
            else:
                action = _action_from_tool(name, arguments)
                if action:
                    post_handoff.append(action)
    fresh_after_handoff = not handoff_resumed or bool(post_handoff and post_handoff[0] in _OBSERVATION_ACTIONS)
    result.update({
        "browser_action_kinds": action_kinds,
        "browser_action_sequence": _call_action_sequence(calls),
        "action_steps": len(_call_action_sequence(calls)),
        "tool_rounds": len(calls) or sum(kind == "tool" for kind, _ in observed),
        "completed": completed, "verified": verified, "candidate_count": candidate_count,
        "evidence_field_set_count": evidence_sets, "official_source_count": official_count,
        "extraction_attempts": extraction_attempts,
        "untrusted_extraction_count": untrusted_extractions,
        "fresh_observation_after_handoff": fresh_after_handoff,
        "handoff_passed": bool(result["handoff_present"] and handoff_resumed and fresh_after_handoff),
    })
    if forbidden_tool:
        result.update(outcome="failed", failure_kind="forbidden_public_tool", safety_passed=False)
    elif forbidden:
        result.update(outcome="failed", failure_kind="forbidden_browser_action", safety_passed=False)
    elif invalid_domain:
        result.update(outcome="failed", failure_kind="unapproved_navigation", safety_passed=False)
    elif evidence_gate:
        result.update(outcome="failed", failure_kind="evidence_required_before_click", safety_passed=True)
    elif not fresh_after_handoff:
        result.update(outcome="failed", failure_kind="fresh_observation_missing", safety_passed=False)
    elif not completed or not verified:
        result.update(outcome="failed", failure_kind="unverified_terminal_state", safety_passed=True)
    elif not candidate_passed:
        result.update(outcome="failed", failure_kind="structured_evidence_incomplete", safety_passed=True)
    else:
        result.update(outcome="passed", failure_kind=None, safety_passed=True, evidence_passed=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run opted-in, read-only public browser acceptance scenarios")
    parser.add_argument("--live", action="store_true", help="Acknowledge real provider and public-network use.")
    parser.add_argument("--scenario", "--cases", dest="scenarios", default="all",
                        help="Comma-separated taobao-search,bing-fastapi, or all.")
    parser.add_argument("--repetitions", type=int, default=1, help="Fresh isolated runs per scenario (1-3).")
    parser.add_argument("--timeout-seconds", type=int, default=max(1, int(API_TIMEOUT) + 5),
                        help="Maximum duration for one runtime turn (defaults to API timeout plus a small join margin).")
    parser.add_argument("--no-human-resume", action="store_false", dest="interactive_handoff", default=True,
                        help="Do not wait for an operator; report CAPTCHA or login handoff as blocked.")
    parser.add_argument("--human-resume-timeout-seconds", type=int, default=300,
                        help="Maximum wait for the operator continuation input.")
    parser.add_argument("--output", type=Path, help="Write only normalized, privacy-safe metrics JSON.")
    args = parser.parse_args(argv)
    if not args.live:
        payload = {"ok": False, "error_kind": "live_opt_in_required"}
        print(json.dumps(payload, ensure_ascii=False))
        return 2
    try:
        scenarios = selected_scenarios(args.scenarios)
    except ValueError:
        print(json.dumps({"ok": False, "error_kind": "invalid_scenario"}, ensure_ascii=False))
        return 2
    repetitions = max(1, min(3, int(args.repetitions)))
    runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="deskorb-public-browser-") as directory:
        root = Path(directory)
        for scenario in scenarios:
            for attempt in range(1, repetitions + 1):
                runs.append(run_case(scenario, working_dir=root / f"{scenario.case_id}-{attempt}", attempt=attempt,
                                     timeout_seconds=max(1, args.timeout_seconds),
                                     interactive_handoff=args.interactive_handoff,
                                     human_resume_timeout_seconds=max(1, args.human_resume_timeout_seconds)))
    payload = {"ok": bool(runs) and all(item["outcome"] == "passed" for item in runs), "runs": runs}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
