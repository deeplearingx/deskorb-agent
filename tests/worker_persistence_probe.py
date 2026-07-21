"""Manual smoke test for the overlay's actual persistent worker."""
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import CodexWorker


def wait_done(events, timeout=180):
    deadline = time.monotonic() + timeout
    answer = []
    while time.monotonic() < deadline:
        try:
            kind, payload = events.get(timeout=1)
        except queue.Empty:
            continue
        if kind == "delta":
            answer.append(payload)
        if kind == "error":
            raise RuntimeError(payload)
        if kind == "turn_done":
            return "".join(answer)
    raise TimeoutError("worker turn timed out")


if __name__ == "__main__":
    events = queue.Queue()
    worker = CodexWorker(events)
    worker.start()
    worker.ask("Reply with exactly: WORKER_PERSIST_ONE")
    started = time.monotonic()
    answer = wait_done(events)
    print("TURN1", round(time.monotonic() - started, 1), answer)
    worker.ask("Reply with exactly: WORKER_PERSIST_TWO")
    started = time.monotonic()
    answer = wait_done(events)
    print("TURN2", round(time.monotonic() - started, 1), answer)
    worker.shutdown()
    worker.join(10)
