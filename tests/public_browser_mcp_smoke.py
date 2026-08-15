"""Bounded non-model smoke for public navigation used by extended cases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from queue import Queue
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime import AgentRuntime
from browser_evidence import page_url_from_content, safe_http_url
from config import API_BASE_URL, API_MODEL, API_PROXY_URL


ALLOWED_HOSTS = frozenset({
    "books.toscrape.com", "www.wikipedia.org", "wikipedia.org", "www.github.com",
    "github.com", "www.baidu.com", "baidu.com", "www.4399.com", "4399.com",
})


def _approved_url(value: str) -> str:
    url = safe_http_url(value)
    parsed = urlparse(url)
    host = str(parsed.hostname or "").casefold().rstrip(".")
    if host not in ALLOWED_HOSTS:
        raise ValueError("smoke URL host is not in the explicit public allowlist")
    return url


def _summary(value: object) -> dict[str, object]:
    result = value if isinstance(value, dict) else {}
    content = result.get("content")
    return {
        "ok": bool(result.get("ok")),
        "failure_kind": str(result.get("failure_kind") or "")[:80] or None,
        "state_changed": bool(result.get("state_changed")),
        "has_observation": bool(result.get("observation_id")),
        "page_url_observed": bool(page_url_from_content(content)),
        "tab_count": int(result.get("tab_count") or 0),
    }


def run(url: str) -> dict[str, object]:
    target = _approved_url(url)
    runtime = AgentRuntime(Queue(), API_MODEL, API_BASE_URL, API_PROXY_URL)
    try:
        startup = runtime.prepare_visible_browser()
        if not startup.get("ok"):
            return {"ok": False, "failure_kind": startup.get("failure_kind"), "startup": _summary(startup)}
        session = runtime._ensure_browser_session()
        navigation = session.execute([{"action": "navigate", "arguments": {"url": target}}])
        if not navigation.get("ok"):
            return {"ok": False, "failure_kind": navigation.get("failure_kind"),
                    "navigate": _summary(navigation)}
        snapshot = session.execute([{"action": "snapshot", "arguments": {}}])
        tabs = session.execute([{"action": "list_tabs", "arguments": {
            "observation_id": str(snapshot.get("observation_id") or ""),
        }}]) if snapshot.get("ok") else {"ok": False, "failure_kind": "snapshot_failed"}
        return {
            "ok": bool(navigation.get("ok") and snapshot.get("ok") and tabs.get("ok")),
            "target_host": str(urlparse(target).hostname or "").casefold(),
            "navigate": _summary(navigation),
            "snapshot": _summary(snapshot),
            "tabs": _summary(tabs),
        }
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a non-model public Browser MCP smoke.")
    parser.add_argument("--url", action="append", required=True)
    args = parser.parse_args(argv)
    results = [run(item) for item in args.url]
    result = {"ok": all(item.get("ok") for item in results), "results": results}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
