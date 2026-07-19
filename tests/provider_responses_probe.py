"""Verify whether the configured provider supports the Responses API."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from third_party_probe import config


values = config(Path(__file__).resolve().parents[2] / ".env")
request = urllib.request.Request(
    values["url"].rstrip("/") + "/responses",
    data=json.dumps({"model": "gpt-5.6-terra", "input": "Reply exactly: RSP_OK"}).encode(),
    method="POST",
    headers={"Authorization": "Bearer " + values["api-key"], "Content-Type": "application/json"},
)
started = time.perf_counter()
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read(128 * 1024))
    print(json.dumps({"supported": True, "status": 200, "elapsed_s": round(time.perf_counter()-started, 3),
                      "response_id": bool(data.get("id")), "has_output": bool(data.get("output") or data.get("output_text"))}))
except urllib.error.HTTPError as exc:
    print(json.dumps({"supported": False, "status": exc.code, "elapsed_s": round(time.perf_counter()-started, 3)}))
