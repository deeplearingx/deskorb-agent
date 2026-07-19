"""Inspect safe event metadata for the provider's Responses API stream."""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from third_party_probe import config


values = config(Path(__file__).resolve().parents[2] / ".env")
req = urllib.request.Request(
    values["url"].rstrip("/") + "/responses",
    data=json.dumps({"model": "gpt-5.6-terra", "input": "Reply exactly: STREAM_OK", "stream": True}).encode(),
    method="POST",
    headers={"Authorization": "Bearer " + values["api-key"], "Content-Type": "application/json", "Accept": "text/event-stream"},
)
started = time.perf_counter(); first = None; types = []; text = []
with urllib.request.urlopen(req, timeout=90) as response:
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        event = json.loads(data)
        kind = event.get("type", "")
        if kind not in types: types.append(kind)
        delta = event.get("delta")
        if delta:
            if first is None: first = time.perf_counter()
            text.append(str(delta))
ended = time.perf_counter()
print(json.dumps({"types": types, "first_s": round((first or ended)-started, 3),
                  "total_s": round(ended-started, 3), "exact": "".join(text).strip() == "STREAM_OK"}))
