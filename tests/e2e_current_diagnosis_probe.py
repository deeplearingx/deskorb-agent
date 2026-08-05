"""Consent-gated, read-only diagnosis of the currently running DeskOrb environment."""
from __future__ import annotations

import json
import argparse
import os
import sys
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, API_PROXY_URL, MODEL_PROVIDER


CONSENT_TOKEN = "I_AUTHORIZE_CURRENT_DESKTOP_DIAGNOSTIC"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consent-gated read-only current desktop diagnosis")
    parser.add_argument("--confirm-current-desktop", action="store_true",
                        help="explicitly authorize sending active-window metadata and the local DeskOrb log")
    args = parser.parse_args(argv)
    env_consent = os.environ.get("DESKORB_AGENT_LIVE_DIAGNOSTIC_CONFIRM", "").strip()
    if not args.confirm_current_desktop and env_consent != CONSENT_TOKEN:
        print(json.dumps({"ok": False, "status": "consent_required",
                          "requires_user_confirmation": True,
                          "message": "Run with --confirm-current-desktop only after the user explicitly authorizes this diagnostic."},
                         ensure_ascii=False))
        return 4
    root = Path(__file__).resolve().parents[1]
    events: Queue = Queue()
    # Read-only prevents this live check from modifying desktop state or files.
    runtime = AgentRuntime(events, API_MODEL, API_BASE_URL, API_PROXY_URL, working_dir=root,
                           full_access=False, model_provider=MODEL_PROVIDER)
    task = (
        "The user explicitly authorized this read-only diagnostic. First inspect the current active-window metadata "
        "and read deskorb-agent-debug.log in the configured working directory. Do not capture a screenshot, read the "
        "clipboard, launch applications, run shell commands, or modify files. Diagnose any observable DeskOrb connection "
        "or startup issue. Clearly separate evidence from hypotheses and list safe next actions."
    )
    try:
        runtime.run_turn(task, [])
        items = []
        while True:
            try:
                items.append(events.get_nowait())
            except Empty:
                break
        answer = "\n".join(str(value) for kind, value in items if kind == "delta")
        tools = [value[0] for kind, value in items if kind == "tool" and isinstance(value, tuple)]
        result = {"ok": bool(answer and "Active window" in tools and "Read file" in tools),
                  "tools": tools, "answer": answer[-3000:]}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:1000]}, ensure_ascii=False))
        return 3
    finally:
        if runtime.mcp:
            runtime.mcp.close()


if __name__ == "__main__":
    raise SystemExit(main())
