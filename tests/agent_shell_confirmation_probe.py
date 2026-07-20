"""Safe end-to-end test of Agent Runtime's confirmed Shell flow.

The only local command is Write-Output; it does not write files or alter the
desktop.  The report omits response text and confirmation tokens.
"""
from __future__ import annotations

import json
import queue
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL


def drain(events):
    output = []
    while not events.empty():
        output.append(events.get_nowait())
    return output


def main() -> int:
    events: "queue.Queue" = queue.Queue()
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL,
                           working_dir=Path(__file__).resolve().parents[1])
    started = time.perf_counter()
    try:
        runtime.run_turn(
            "Execute Write-Output DESKORB_SHELL_OK using shell_run now. Do not ask for confirmation in prose; invoke the tool.", [],
        )
        first = drain(events)
        approval = next((str(payload) for kind, payload in first if kind == "approval"), "")
        match = re.search(r"确认 ([0-9A-F]{6})", approval)
        if not match:
            print(json.dumps({"ok": False, "stage": "approval", "events": [kind for kind, _ in first]}))
            return 2
        runtime.run_turn("确认 " + match.group(1), [])
        second = drain(events)
        answer = "".join(str(payload) for kind, payload in second if kind == "delta")
        print(json.dumps({"ok": "DESKORB_SHELL_OK" in answer, "approval_requested": True,
                          "received_answer": bool(answer), "elapsed_s": round(time.perf_counter() - started, 3)}))
        return 0 if "DESKORB_SHELL_OK" in answer else 3
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]}))
        return 4


if __name__ == "__main__":
    sys.exit(main())
