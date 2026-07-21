"""Read local Codex computer-use requirements without moving the pointer."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time


def main():
    codex = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
    if not codex:
        raise RuntimeError("codex not found")
    command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", codex] if os.name == "nt" and codex.endswith(".cmd") else [codex]
    proc = subprocess.Popen(command + ["-c", 'service_tier="fast"', "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
    assert proc.stdin and proc.stdout
    messages = queue.Queue()
    threading.Thread(target=lambda: [messages.put(line) for line in proc.stdout], daemon=True).start()
    try:
        for ident, method, params in ((1, "initialize", {"clientInfo": {"name": "probe", "version": "1"}, "capabilities": {"experimentalApi": True}}), (2, "configRequirements/read", None)):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try: message = json.loads(messages.get(timeout=1))
                except queue.Empty: continue
                if message.get("id") == ident:
                    print(json.dumps(message, ensure_ascii=False))
                    break
            else: raise TimeoutError(method)
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
