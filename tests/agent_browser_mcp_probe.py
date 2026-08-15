"""End-to-end probe for DeskOrb's own Agent + local Playwright MCP bridge.

The target is intentionally a public demo catalogue: no login, cart operation,
form submission, or user-browser profile is involved.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from browser_evidence import safe_http_url
from config import API_BASE_URL, API_MODEL, API_PROXY_URL


DEFAULT_TASK = (
    "使用本地 MCP 浏览器完成测试：打开 https://books.toscrape.com/，从商品列表找到任意一本"
    "价格在 £20 到 £30（含）之间的书。不要登录、加入购物车、提交表单或购买。"
    "最后只报告书名和页面显示的价格。"
)


def _drain(events: Queue) -> list[tuple[str, object]]:
    result = []
    while True:
        try:
            result.append(events.get_nowait())
        except Empty:
            return result


def _approval_token(events: list[tuple[str, object]]) -> str | None:
    for kind, value in events:
        if kind == "approval":
            match = re.search(r"确认\s+([A-F0-9]{6,})", str(value))
            if match:
                return match.group(1)
    return None


def _safe_event_debug(events: list[tuple[str, object]]) -> dict[str, object]:
    labels = [str(value[0])[:100] for kind, value in events
              if kind == "tool" and isinstance(value, (tuple, list)) and value]
    results = []
    for kind, value in events:
        if kind != "tool_result" or not isinstance(value, dict):
            continue
        results.append({
            "tool": str(value.get("tool") or "")[:80],
            "ok": bool(value.get("ok")),
            "failure_kind": str(value.get("failure_kind") or "")[:80] or None,
            "action_types": list(value.get("action_types") or ())[:8],
            "action_steps": int(value.get("action_steps") or 0),
            "verified": bool(value.get("verified")),
        })
    return {
        "tool_labels": dict(Counter(labels)),
        "tool_result_summary": results[-40:],
        "human_handoff_events": sum(kind in {"human_handoff", "human_verification"} for kind, _ in events),
    }


def main() -> int:
    events: Queue = Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL)
    task = os.environ.get("DESKORB_AGENT_PROBE_TASK", DEFAULT_TASK).strip() or DEFAULT_TASK
    action_debug: list[dict[str, object]] = []
    original_local_tool = runtime._run_local_tool

    def traced_local_tool(name, arguments):
        session = runtime._browser_session if name == "browser_action_batch" else None
        actions = arguments.get("actions") if isinstance(arguments, dict) else []
        requested_targets = []
        current_candidates = {}
        if name == "browser_action_batch":
            try:
                from browser_cache import parse_snapshot_candidates
                current_candidates = {
                    str(item.ref): item
                    for item in parse_snapshot_candidates(getattr(session, "_last_snapshot", None))
                } if session is not None else {}
            except Exception:
                current_candidates = {}
            for item in actions if isinstance(actions, list) else ():
                if not isinstance(item, dict) or not isinstance(item.get("arguments"), dict):
                    continue
                ref = str(item["arguments"].get("ref") or "")
                candidate = current_candidates.get(ref)
                if candidate is not None:
                    requested_targets.append({
                        "ref": ref[:80], "role": str(candidate.role)[:40],
                        "name": str(candidate.name)[:160],
                    })
        result = original_local_tool(name, arguments)
        if name == "browser_action_batch":
            action_debug.append({
                "action_types": [str(item.get("action") or "")[:40]
                                 for item in actions if isinstance(item, dict)],
                "argument_keys": [sorted(str(key)[:40] for key in item.keys())
                                   for item in actions if isinstance(item, dict)],
                "requested_refs": [str((item.get("arguments") or {}).get("ref") or "")[:80]
                                    for item in actions if isinstance(item, dict)
                                    and isinstance(item.get("arguments"), dict)
                                    and item.get("arguments", {}).get("ref")],
                "requested_targets": requested_targets[:8],
                "candidate_refs": [str(item.get("ref") or "")[:80]
                                   for item in (result.get("candidates") or [])
                                   if isinstance(item, dict)][:32]
                if isinstance(result, dict) else [],
                "ok": bool(isinstance(result, dict) and result.get("ok")),
                "failure_kind": str(result.get("failure_kind") or "")[:80]
                if isinstance(result, dict) else "",
                "action_steps": int(result.get("action_steps") or 0)
                if isinstance(result, dict) else 0,
                "page_url": safe_http_url(getattr(session, "_current_page_url", ""), strip_query=True)
                if session is not None else "",
                "observation_id": str(getattr(session, "observation_id", "") or "")[:80]
                if session is not None else "",
                "evidence_count": len(getattr(getattr(session, "evidence_ledger", None), "records", ()) or ())
                if session is not None else 0,
            })
        return result

    runtime._run_local_tool = traced_local_tool
    request_metrics: list[dict[str, object]] = []
    original_request = runtime._request

    def traced_request(payload, api_key, *, deadline=None):
        started = time.perf_counter()
        input_items = payload.get("input") if isinstance(payload, dict) else []
        input_chars = len(json.dumps(input_items, ensure_ascii=False, separators=(",", ":")))
        instructions_chars = len(str(payload.get("instructions") or "")) if isinstance(payload, dict) else 0
        tools_count = len(payload.get("tools") or ()) if isinstance(payload, dict) else 0
        try:
            result = original_request(payload, api_key, deadline=deadline)
            request_metrics.append({
                "ok": True,
                "elapsed_s": round(time.perf_counter() - started, 3),
                "input_chars": input_chars,
                "instructions_chars": instructions_chars,
                "tools": tools_count,
                "output_types": sorted({str(item.get("type")) for item in result.get("output") or []
                                         if isinstance(item, dict)}),
            })
            return result
        except Exception as exc:
            request_metrics.append({
                "ok": False,
                "elapsed_s": round(time.perf_counter() - started, 3),
                "input_chars": input_chars,
                "instructions_chars": instructions_chars,
                "tools": tools_count,
                "error_type": type(exc).__name__,
            })
            raise

    runtime._request = traced_request
    try:
        runtime.run_turn(task, [])
        first = _drain(events)
        token = _approval_token(first)
        if token:
            runtime.run_turn(f"确认 {token}", [])
        all_events = [*first, *_drain(events)]
        answer = "\n".join(str(value) for kind, value in all_events if kind == "delta")
        debug = _safe_event_debug(all_events)
        mcp_calls = sum(count for label, count in debug["tool_labels"].items()
                        if "browser" in str(label).casefold())
        result = {
            "ok": bool(token and mcp_calls and re.search(r"£\s?\d+(?:\.\d+)?", answer)),
            "approval_used": bool(token),
            "mcp_tool_calls": mcp_calls,
            **debug,
            "request_metrics": request_metrics,
            "action_debug": action_debug[-40:],
            "answer": answer[-2000:],
            "events": [kind for kind, _value in all_events],
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:1000],
                          **_safe_event_debug(_drain(events)), "request_metrics": request_metrics,
                          "action_debug": action_debug[-40:]},
                         ensure_ascii=False))
        return 3
    finally:
        if runtime.mcp:
            runtime.mcp.close()


if __name__ == "__main__":
    raise SystemExit(main())
