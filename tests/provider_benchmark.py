"""Small, low-cost chat-completions benchmark for the configured provider."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from third_party_probe import config


TASKS = [
    ("exact", "Reply with exactly: PINE-47", lambda value: value.strip() == "PINE-47"),
    ("math", "What is 17 multiplied by 19? Reply with only the number.", lambda value: value.strip() == "323"),
    ("json", "Return only compact JSON with keys city and code for: city=Hangzhou, code=310000.",
     lambda value: _json_ok(value)),
]


def _json_ok(value: str) -> bool:
    try:
        data = json.loads(value)
        return data.get("city") == "Hangzhou" and str(data.get("code")) == "310000"
    except Exception:
        return False


def request(base: str, key: str, model: str, prompt: str, stream: bool) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 80,
        "stream": stream,
    }
    req = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "Accept": "text/event-stream" if stream else "application/json"},
    )
    started = time.perf_counter()
    first = None
    chunks: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            if not stream:
                data = json.loads(response.read())
                text = data["choices"][0]["message"].get("content") or ""
                return {"ok": True, "text": text, "first_s": round(time.perf_counter() - started, 3),
                        "total_s": round(time.perf_counter() - started, 3)}
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    continue
                event = json.loads(data)
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {}).get("content") or ""
                if delta and first is None:
                    first = time.perf_counter()
                chunks.append(delta)
        ended = time.perf_counter()
        return {"ok": True, "text": "".join(chunks),
                "first_s": round((first or ended) - started, 3), "total_s": round(ended - started, 3)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def main() -> int:
    values = config(Path(__file__).resolve().parents[2] / ".env")
    base, key = values["url"].rstrip("/"), values["api-key"]
    available = json.loads(urllib.request.urlopen(urllib.request.Request(
        base + "/models", headers={"Authorization": "Bearer " + key}), timeout=30).read())
    ids = {str(item.get("id")) for item in available.get("data", []) if isinstance(item, dict)}
    candidates = [model for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna") if model in ids]
    results = []
    for model in candidates:
        model_result = {"model": model, "tasks": []}
        for index, (name, prompt, checker) in enumerate(TASKS):
            result = request(base, key, model, prompt, stream=(index == 0))
            result["task"] = name
            result["passed"] = bool(result.get("ok") and checker(result.get("text", "")))
            result.pop("text", None)
            model_result["tasks"].append(result)
        passed = sum(1 for task in model_result["tasks"] if task["passed"])
        model_result["completion_rate"] = f"{passed}/{len(TASKS)}"
        model_result["first_token_s"] = model_result["tasks"][0].get("first_s")
        model_result["stream_total_s"] = model_result["tasks"][0].get("total_s")
        results.append(model_result)
    print(json.dumps({"models_tested": len(results), "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
