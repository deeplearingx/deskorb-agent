"""Two real streaming turns through the overlay worker's API code path."""
from __future__ import annotations

import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import CodexWorker


events: "queue.Queue" = queue.Queue()
worker = CodexWorker(events, backend="api")


def run_turn(prompt: str) -> tuple[str, list[str], float]:
    started = time.perf_counter()
    worker._run_turn(prompt, [])
    elapsed = time.perf_counter() - started
    answer, errors = [], []
    while not events.empty():
        kind, payload = events.get_nowait()
        if kind == "delta":
            answer.append(str(payload))
        if kind == "error":
            errors.append(str(payload))
    return "".join(answer).strip(), errors, elapsed


first, first_errors, first_elapsed = run_turn("Remember this code: CEDAR-91. Reply exactly: STORED")
second, second_errors, second_elapsed = run_turn(
    "What code did I ask you to remember? Reply exactly with the code."
)
print({
    "first_elapsed_s": round(first_elapsed, 3), "first_exact": first == "STORED",
    "second_elapsed_s": round(second_elapsed, 3), "second_exact": second == "CEDAR-91",
    "errors": len(first_errors) + len(second_errors), "backend": worker._resolved_backend(),
    "model": worker._model, "history_items": len(worker._api_context.messages),
})
