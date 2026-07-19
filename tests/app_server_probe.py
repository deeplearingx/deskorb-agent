# Manual integration probe for Codex app-server persistence.
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def launch() -> subprocess.Popen[str]:
    codex = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
    if not codex:
        raise RuntimeError("codex executable not found")
    if os.name == "nt" and codex.lower().endswith((".cmd", ".bat")):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", codex]
    else:
        command = [codex]
    command += ["-c", 'service_tier="fast"', "app-server", "--stdio"]
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=CREATE_NO_WINDOW,
    )


def main():
    proc = launch()
    assert proc.stdin and proc.stdout
    lines: "queue.Queue[str | None]" = queue.Queue()

    def reader():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    next_id = 1

    def send(method, params):
        nonlocal next_id
        ident = next_id
        next_id += 1
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return ident

    def wait_for(predicate, timeout=180):
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            try:
                raw = lines.get(timeout=min(1, max(0.1, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if raw is None:
                raise RuntimeError("app-server exited: " + "\n".join(seen[-10:]))
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                seen.append(raw)
                continue
            if predicate(message):
                return message
            seen.append(raw)
        raise TimeoutError("timed out: " + "\n".join(seen[-10:]))

    try:
        init_id = send("initialize", {
            "clientInfo": {"name": "codex-overlay-probe", "version": "0.1"},
            "capabilities": {"experimentalApi": True},
        })
        wait_for(lambda m: m.get("id") == init_id, timeout=20)

        thread_id = send("thread/start", {
            "cwd": str(Path.home()),
            "model": "gpt-5.6-sol",
            "sandbox": "workspace-write",
            "approvalPolicy": "never",
            "serviceTier": "fast",
        })
        thread = wait_for(lambda m: m.get("id") == thread_id, timeout=30)
        tid = thread["result"]["thread"]["id"]
        print("THREAD", tid)

        def turn(prompt):
            request_id = send("turn/start", {
                "threadId": tid,
                "input": [{"type": "text", "text": prompt}],
                "approvalPolicy": "never",
                "serviceTier": "fast",
            })
            wait_for(lambda m: m.get("id") == request_id, timeout=30)
            started = time.monotonic()
            answer = []
            while True:
                message = wait_for(
                    lambda m: m.get("method") in ("item/agentMessage/delta", "turn/completed"),
                    timeout=180,
                )
                method = message.get("method")
                params = message.get("params", {})
                if method == "item/agentMessage/delta":
                    answer.append(str(params.get("delta", "")))
                elif method == "turn/completed":
                    return round(time.monotonic() - started, 1), "".join(answer), params

        print("TURN1", turn("Reply with exactly: APP_SERVER_ONE"))
        print("TURN2", turn("Reply with exactly: APP_SERVER_TWO"))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()

