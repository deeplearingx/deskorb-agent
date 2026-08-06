"""Protocol-neutral, privacy-safe provider tool-call acceptance matrix.

Unlike the legacy Responses-only probe, this script exercises the same
stateless canonical transcript that :class:`ModelAdapter` uses in DeskOrb.
It sends a no-op ``echo_probe`` function call and never exposes a key, prompt
body, response body, screenshot, file, or local tool implementation.

The probe is intentionally opt-in for fallback providers: a missing
DeepSeek/Qwen key is reported as ``skipped`` unless the caller lists that
provider with ``--require-provider``.  This prevents a generic primary key
from being sent to another vendor while still making CI fail clearly when a
release image promises a fallback.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from credential_store import get_api_key, get_explicit_provider_api_key
from model_adapter import ModelAdapter, normalize_provider
from model_registry import ModelHealthStore
from provider_longrun_probe import _targets
from responses_tool_protocol import continue_input, function_call_output, function_calls
from task_runtime import classify_failure


ECHO_TOOL = {
    "type": "function",
    "name": "echo_probe",
    "description": "Returns the supplied text. This is a no-op compatibility probe.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
}
INITIAL_INPUT = [{
    "role": "user",
    "content": "Call echo_probe exactly once with text TOOL_OK. Do not answer before calling it.",
}]


class ProviderProbeError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _safe_error(value: Any, api_key: str) -> str:
    text = str(value or "")
    if api_key:
        text = text.replace(api_key, "[redacted]")
    return text[:500]


def _post(adapter: ModelAdapter, api_key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = adapter.prepare_request(payload)
    request = urllib.request.Request(
        adapter.endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "deskorb-agent-provider-probe/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024).decode("utf-8", "replace")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ProviderProbeError("provider returned a non-object response", int(response.status))
            normalized = adapter.normalize_response(parsed)
            if isinstance(normalized, dict):
                normalized = {**normalized, "_probe_http_status": int(response.status or 200)}
            return normalized
    except urllib.error.HTTPError as exc:
        detail = exc.read(64 * 1024).decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict):
                detail = str((parsed.get("error") or parsed).get("message")
                             if isinstance(parsed.get("error") or parsed, dict) else parsed)
        except Exception:
            pass
        raise ProviderProbeError(f"HTTP {exc.code}: {_safe_error(detail, api_key)}", int(exc.code)) from exc
    except ProviderProbeError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderProbeError(_safe_error(exc, api_key)) from exc


def _response_text(response: dict[str, Any]) -> str:
    text = str(response.get("output_text") or "").strip()
    if text:
        return text
    chunks: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                chunks.append(str(part.get("text") or ""))
    return "".join(chunks).strip()


def _probe_once(target: dict[str, str], api_key: str, timeout: float) -> dict[str, Any]:
    adapter = ModelAdapter(target["provider"], target["base_url"])
    started = time.perf_counter()
    report: dict[str, Any] = {
        "ok": False,
        "provider": adapter.provider,
        "protocol": adapter.protocol,
        "model": target["model"],
        "native_tools": False,
        "continuation": False,
    }
    try:
        first = _post(adapter, api_key, {
            "model": target["model"], "input": INITIAL_INPUT, "tools": [ECHO_TOOL],
            "parallel_tool_calls": False, "stream": False,
        }, timeout)
        report["first_latency_ms"] = round((time.perf_counter() - started) * 1000)
        report["first_status"] = first.get("_probe_http_status")
        report["output_types"] = sorted({str(item.get("type")) for item in first.get("output") or []
                                         if isinstance(item, dict)})
        calls = function_calls(first)
        if len(calls) != 1 or calls[0].name != "echo_probe":
            raise ProviderProbeError("provider did not return one echo_probe function call")
        try:
            arguments = json.loads(calls[0].arguments)
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        if not isinstance(arguments, dict) or arguments.get("text") != "TOOL_OK":
            raise ProviderProbeError("echo_probe arguments were not TOOL_OK")
        report["native_tools"] = True

        transcript = continue_input(INITIAL_INPUT, first,
                                    [function_call_output(calls[0].call_id, "TOOL_OK")])
        second = _post(adapter, api_key, {
            "model": target["model"], "input": transcript, "tools": [ECHO_TOOL],
            "parallel_tool_calls": False, "stream": False,
        }, timeout)
        report["continuation_status"] = second.get("_probe_http_status")
        report["continuation_latency_ms"] = round((time.perf_counter() - started) * 1000) - int(report["first_latency_ms"])
        text = _response_text(second)
        report["continuation_strategy"] = "stateless_output_only"
        if not text and not function_calls(second):
            transcript.append({"role": "user", "content": "Continue and reply exactly TOOL_OK."})
            second = _post(adapter, api_key, {
                "model": target["model"], "input": transcript, "tools": [ECHO_TOOL],
                "parallel_tool_calls": False, "stream": False,
            }, timeout)
            report["continuation_status"] = second.get("_probe_http_status")
            report["continuation_strategy"] = "explicit_prompt_after_empty_message"
            text = _response_text(second)
        report["continuation"] = text == "TOOL_OK"
        if not report["continuation"]:
            raise ProviderProbeError("stateless tool continuation did not return TOOL_OK")
        report["ok"] = True
    except ProviderProbeError as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:500]
        report["status_code"] = exc.status_code
        report["error_kind"] = classify_failure(str(exc))
    report["total_latency_ms"] = round((time.perf_counter() - started) * 1000)
    return report


def run(repetitions: int = 3, delay_seconds: float = 0.0,
        required_providers: list[str] | None = None,
        history_path: str | Path | None = None) -> dict[str, Any]:
    repetitions = max(1, min(20, int(repetitions)))
    delay_seconds = max(0.0, min(30.0, float(delay_seconds)))
    required = {normalize_provider(item) for item in (required_providers or ()) if str(item).strip()}
    store = ModelHealthStore(Path(history_path).expanduser()) if history_path else None
    reports: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="deskorb-provider-capability-"):
        for target in _targets():
            provider = normalize_provider(target["provider"])
            key = (get_api_key(provider) if target["id"] == "primary"
                   else get_explicit_provider_api_key(provider))
            report: dict[str, Any] = {
                "id": target["id"], "provider": provider, "model": target["model"],
                "protocol": ModelAdapter(provider, target["base_url"]).protocol,
                "configured": bool(key), "runs": [],
            }
            if not key:
                report["skipped"] = True
                report["skip_reason"] = "provider key is not configured"
                reports.append(report)
                continue
            for index in range(repetitions):
                result = _probe_once(target, key, timeout=120.0)
                result["attempt"] = index + 1
                report["runs"].append({key: value for key, value in result.items() if key != "provider" and key != "model"})
                if store:
                    store.record(provider=provider, model=target["model"], operation="tool_probe",
                                 ok=bool(result.get("ok")), latency_ms=float(result.get("total_latency_ms") or 0),
                                 status_code=result.get("status_code") or result.get("continuation_status")
                                 or result.get("first_status"), retry_count=0,
                                 failure_kind=result.get("error_kind"))
                if delay_seconds and index + 1 < repetitions:
                    time.sleep(delay_seconds)
            successful = [item for item in report["runs"] if item.get("ok")]
            report["summary"] = {
                "runs": len(report["runs"]), "success_rate": round(len(successful) / len(report["runs"]), 4),
                "p50_total_latency_ms": sorted(item.get("total_latency_ms", 0) for item in report["runs"])[len(report["runs"]) // 2],
            }
            reports.append(report)
    scored = [item for item in reports if not item.get("skipped")]
    missing_required = sorted(provider for provider in required
                               if not any(item["provider"] == provider and item.get("configured") for item in reports))
    passed = sum(1 for item in scored if all(run.get("ok") for run in item["runs"]))
    return {
        "ok": bool(scored) and passed == len(scored) and not missing_required,
        "repetitions": repetitions, "required_providers": sorted(required),
        "missing_required_providers": missing_required,
        "history_path": Path(history_path).name if history_path else None,
        "targets": reports, "scored_targets": len(scored), "passed_targets": passed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run stateless no-op tool-call checks for configured providers")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--history-path", type=Path)
    parser.add_argument("--require-provider", action="append", default=[],
                        help="Provider that must have an explicit key; repeat for multiple providers")
    args = parser.parse_args(argv)
    payload = run(args.repetitions, args.delay_seconds, args.require_provider, args.history_path)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
