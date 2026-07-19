"""Safe connectivity probe for the colon-delimited third-party .env file.

Never prints the API key or Authorization header.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def config(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def main() -> int:
    values = config(Path(__file__).resolve().parents[2] / ".env")
    base = values.get("url", "").rstrip("/")
    key = values.get("api-key", "")
    configured_model = values.get("model_name", "")
    if not base.startswith(("https://", "http://")) or not key:
        print(json.dumps({"error": "Need url and api-key in ../.env"}, ensure_ascii=False))
        return 2
    started = time.perf_counter()
    request = urllib.request.Request(
        base + "/models",
        headers={"Authorization": "Bearer " + key, "User-Agent": "codex-overlay-probe/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read(512 * 1024)
            status = response.status
    except urllib.error.HTTPError as exc:
        print(json.dumps({"reachable": True, "status": exc.code,
                          "elapsed_s": round(time.perf_counter() - started, 3)}, ensure_ascii=False))
        return 3
    except Exception as exc:
        print(json.dumps({"reachable": False, "error_type": type(exc).__name__,
                          "elapsed_s": round(time.perf_counter() - started, 3)}, ensure_ascii=False))
        return 4
    try:
        payload = json.loads(raw)
        ids = [str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
    except Exception:
        ids = []
    model_is_url = "://" in configured_model
    print(json.dumps({
        "reachable": True, "status": status, "elapsed_s": round(time.perf_counter() - started, 3),
        "host": urlsplit(base).netloc, "configured_model_is_url": model_is_url,
        "model_count": len(ids), "models": ids[:40],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
