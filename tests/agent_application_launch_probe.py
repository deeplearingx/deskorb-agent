"""Provider probe that stops at approval and never launches an application."""
from __future__ import annotations

import json
import queue
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL


def main() -> int:
    events: "queue.Queue" = queue.Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL,
                           working_dir=Path(__file__).resolve().parents[1])
    try:
        runtime.run_turn("打开 QQ。请直接执行，不要让我手动点击。", [])
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}, ensure_ascii=False))
        return 2
    kinds = []
    approval = ""
    application = ""
    while not events.empty():
        kind, payload = events.get_nowait()
        kinds.append(kind)
        if kind == "approval":
            approval = str(payload)
        if kind == "tool" and isinstance(payload, tuple) and isinstance(payload[1], dict):
            application = str(payload[1].get("application") or "")
    ok = "tool" in kinds and "approval" in kinds and application == "qq" and "Authorize task:" in approval
    print(json.dumps({"ok": ok, "tool_called": "tool" in kinds,
                      "approval_requested": "approval" in kinds,
                      "correct_application": application == "qq"}, ensure_ascii=False))
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
