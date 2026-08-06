"""Bounded, privacy-safe multi-provider health acceptance probe.

The probe performs only the runtime's minimal ``health check`` request.  It
never sends conversation text, screenshots, files, or tools, and prints only
provider/model metadata, timings, error categories, and aggregate rates.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from queue import Queue
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_runtime import AgentRuntime
from config import API_BASE_URL, API_MODEL, MODEL_FALLBACKS, MODEL_PROVIDER
from credential_store import get_api_key, get_explicit_provider_api_key
from model_adapter import normalize_provider, provider_profile
from model_registry import ModelHealthStore


def _targets() -> list[dict[str, str]]:
    primary_provider = provider_profile(normalize_provider(MODEL_PROVIDER), API_BASE_URL).name
    result = [{"id": "primary", "provider": primary_provider,
               "model": API_MODEL, "base_url": API_BASE_URL}]
    seen = {(result[0]["provider"], result[0]["model"], result[0]["base_url"])}
    for target in MODEL_FALLBACKS:
        item = {"id": target.target_id, "provider": target.capabilities.provider,
                "model": target.model, "base_url": target.base_url}
        key = (item["provider"], item["model"], item["base_url"])
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result[:8]


def run(repetitions: int, delay_seconds: float, history_path: str | Path | None = None) -> dict:
    repetitions = max(1, min(20, int(repetitions)))
    delay_seconds = max(0.0, min(30.0, float(delay_seconds)))
    reports: list[dict] = []
    persistent_history = Path(history_path).expanduser() if history_path else None
    shared_store = ModelHealthStore(persistent_history) if persistent_history else None
    with tempfile.TemporaryDirectory(prefix="deskorb-provider-probe-") as directory:
        for target in _targets():
            key = (get_api_key(target["provider"]) if target["id"] == "primary"
                   else get_explicit_provider_api_key(target["provider"]))
            report = {"id": target["id"], "provider": target["provider"],
                      "model": target["model"], "configured": bool(key), "runs": []}
            if not key:
                report["skipped"] = True
                report["skip_reason"] = "provider key is not configured"
                reports.append(report)
                continue
            runtime = AgentRuntime(Queue(), target["model"], target["base_url"],
                                   working_dir=Path(directory), model_provider=target["provider"])
            if shared_store is not None:
                # Keep task/journal files isolated while sending health metrics
                # to one explicit, privacy-safe history database.
                runtime._model_health_store = shared_store
            for index in range(repetitions):
                started = time.monotonic()
                with patch("agent_runtime.get_api_key", return_value=key):
                    result = runtime.health_check()
                report["runs"].append({
                    "attempt": index + 1,
                    "ok": bool(result.get("ok")),
                    "latency_ms": int(result.get("latency_ms") or round((time.monotonic() - started) * 1000)),
                    "error_kind": result.get("error_kind"),
                })
                if delay_seconds and index + 1 < repetitions:
                    time.sleep(delay_seconds)
            report["summary"] = runtime.model_health_store.summary(
                target["provider"], target["model"], operation="health", limit=100)
            reports.append(report)
    scored = [item for item in reports if not item.get("skipped")]
    passed = sum(1 for item in scored if all(run_item["ok"] for run_item in item["runs"]))
    return {"ok": bool(scored) and passed == len(scored), "repetitions": repetitions,
            # Do not echo an absolute user profile path in CI logs.
            "history_path": persistent_history.name if persistent_history else None,
            "targets": reports, "scored_targets": len(scored), "passed_targets": passed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded provider health checks")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--history-path", type=Path,
                        help="Optional SQLite path for privacy-safe health history across probe runs.")
    args = parser.parse_args(argv)
    payload = run(args.repetitions, args.delay_seconds, args.history_path)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
