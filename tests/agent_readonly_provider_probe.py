"""End-to-end smoke test for the independent read-only Agent Runtime.

It only calls the active-window metadata tool.  The report omits the window
title and every model response so no desktop content is printed.
"""
from __future__ import annotations

import json
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL


def main() -> int:
    events: "queue.Queue" = queue.Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL,
                           working_dir=Path(__file__).resolve().parents[1])
    started = time.perf_counter()
    try:
        runtime.run_turn(
            "Call desktop_get_active_window exactly once. Then reply exactly: AGENT_READONLY_OK",
            [],
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}, ensure_ascii=False))
        return 2
    kinds, answer = [], []
    while not events.empty():
        kind, payload = events.get_nowait()
        kinds.append(kind)
        if kind == "delta":
            answer.append(str(payload))
    print(json.dumps({
        "ok": "tool" in kinds and "AGENT_READONLY_OK" in "".join(answer),
        "tool_called": "tool" in kinds,
        "received_answer": bool(answer),
        "elapsed_s": round(time.perf_counter() - started, 3),
    }, ensure_ascii=False))
    return 0 if "tool" in kinds and "AGENT_READONLY_OK" in "".join(answer) else 3


if __name__ == "__main__":
    sys.exit(main())
