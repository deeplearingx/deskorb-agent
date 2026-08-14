"""Bounded real-site probe for DeskOrb's semantic Browser Action executor.

This probe deliberately bypasses the model planner.  It validates the local
Playwright MCP startup and the same observation-bound semantic actions that the
model uses, so a provider timeout cannot be misreported as a browser failure.
It is read-only and writes metrics only; page text, URLs, refs and arguments
never appear in stdout or the report.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from queue import Queue
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from browser_cache import parse_snapshot_candidates
from config import API_BASE_URL, API_MODEL, API_PROXY_URL


_ALLOWED_HOSTS = frozenset({"books.toscrape.com"})


def _approved_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme not in {"http", "https"} or host not in _ALLOWED_HOSTS:
        raise ValueError("probe URL must be an approved public read-only host")
    return str(value).strip()


def _step(result: object) -> dict[str, object]:
    value = result if isinstance(result, dict) else {}
    return {
        "ok": bool(value.get("ok")),
        "failure_kind": str(value.get("failure_kind") or "")[:80] or None,
        "state_changed": bool(value.get("state_changed")),
        "has_observation": bool(value.get("observation_id")),
        "action_steps": int(value.get("action_steps") or 0),
    }


def run(url: str) -> dict[str, object]:
    url = _approved_url(url)
    started = time.perf_counter()
    runtime = AgentRuntime(Queue(), API_MODEL, API_BASE_URL, API_PROXY_URL)
    steps: list[dict[str, object]] = []
    actions = ["navigate", "snapshot"]
    try:
        startup = runtime.prepare_visible_browser()
        if not startup.get("ok"):
            return {
                "ok": False,
                "failure_kind": str(startup.get("failure_kind") or "browser_mcp_start_failed")[:80],
                "startup_visible": False,
                "action_sequence": actions,
                "steps": steps,
                "total_latency_ms": round((time.perf_counter() - started) * 1000),
            }
        session = runtime._ensure_browser_session()
        result = session.execute([{"action": "navigate", "arguments": {"url": url}}])
        steps.append(_step(result))
        if not result.get("ok"):
            return {
                "ok": False,
                "failure_kind": str(result.get("failure_kind") or "browser_action_failed")[:80],
                "startup_visible": True,
                "action_sequence": actions,
                "steps": steps,
                "total_latency_ms": round((time.perf_counter() - started) * 1000),
            }

        result = session.execute([{"action": "snapshot", "arguments": {}}])
        steps.append(_step(result))
        if not result.get("ok"):
            return {
                "ok": False,
                "failure_kind": str(result.get("failure_kind") or "browser_action_failed")[:80],
                "startup_visible": True,
                "action_sequence": actions,
                "steps": steps,
                "total_latency_ms": round((time.perf_counter() - started) * 1000),
            }

        # Choose a candidate only from the fresh observation.  The label is
        # never emitted; it is used solely to exercise semantic click binding.
        candidates = parse_snapshot_candidates(result.get("content"))
        target = next((item for item in candidates if item.role in {"link", "button"}), None)
        if target is not None:
            actions.append("click_ref")
            result = session.execute([{
                "action": "click_ref",
                "arguments": {
                    "ref": target.ref,
                    "observation_id": str(result.get("observation_id") or ""),
                },
            }])
            steps.append(_step(result))
            if result.get("ok"):
                actions.append("snapshot")
                result = session.execute([{"action": "snapshot", "arguments": {}}])
                steps.append(_step(result))
        return {
            "ok": bool(all(item.get("ok") for item in steps)),
            "failure_kind": next((str(item["failure_kind"]) for item in steps if item.get("failure_kind")), None),
            "startup_visible": True,
            "action_sequence": actions,
            "steps": steps,
            "total_latency_ms": round((time.perf_counter() - started) * 1000),
        }
    finally:
        runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://books.toscrape.com/", help="approved public read-only URL")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(str(args.url))
    rendered = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
