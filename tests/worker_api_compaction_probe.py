"""Real provider check for rolling-summary compaction and recall."""
from __future__ import annotations

import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from credential_store import get_api_key
from worker import CodexWorker


events: "queue.Queue" = queue.Queue()
worker = CodexWorker(events, backend="api")
worker._api_context.add_turn("The account reference is ORCHID-472.", "I will remember that reference.")
worker._api_context.add_turn("The deployment region is Hangzhou.", "Noted: Hangzhou.")
worker._api_context.add_turn("Use the Terra model for routine requests.", "Understood.")
worker._api_context.add_turn("Never include screenshots in historical context.", "Understood.")
worker._api_context.add_turn("The open task is API latency monitoring.", "I will keep that task open.")

started = time.perf_counter()
meta = worker._compact_api_context(get_api_key(), force=True)
compact_elapsed = time.perf_counter() - started

started = time.perf_counter()
worker._run_turn("What was the account reference? Reply exactly with the reference.", [])
recall_elapsed = time.perf_counter() - started
answer, errors = [], []
while not events.empty():
    kind, payload = events.get_nowait()
    if kind == "delta":
        answer.append(str(payload))
    elif kind == "error":
        errors.append(str(payload))

print({
    "compact_elapsed_s": round(compact_elapsed, 3),
    "recall_elapsed_s": round(recall_elapsed, 3),
    "summary_created": bool(worker._api_context.summary),
    "recent_items_after_recall": len(worker._api_context.messages),
    "tokens_reduced": meta["post_tokens"] < meta["pre_tokens"],
    "recall_exact": "".join(answer).strip() == "ORCHID-472",
    "errors": len(errors),
})
