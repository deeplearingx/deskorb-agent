"""Safe real-provider probe for native Responses API function calling.

The only advertised tool is ``echo_probe``.  It has no local implementation,
does not read files, start a process, or control the desktop.  The script
prints capability metadata only and never prints credentials or request bodies.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import API_BASE_URL, API_MODEL, API_PROXY_URL
from responses_tool_protocol import continue_input, function_call_output, function_calls
from third_party_probe import config


def _post(endpoint: str, api_key: str, payload: dict) -> dict:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "deskorb-agent-tool-probe/1",
        },
    )
    proxy_handler = urllib.request.ProxyHandler(
        {"http": API_PROXY_URL, "https": API_PROXY_URL} if API_PROXY_URL else None
    )
    opener = urllib.request.build_opener(proxy_handler)
    try:
        with opener.open(request, timeout=90) as response:
            return json.loads(response.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(64 * 1024).decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            message = str((parsed.get("error") or parsed).get("message") or parsed)
        except Exception:
            message = detail[:500]
        raise RuntimeError(f"HTTP {exc.code}: {message[:500]}") from exc


def main() -> int:
    # The provider file belongs to this repository.  ``parents[2]`` points to
    # D:\workspace on the Windows layout and silently hid the configured
    # project credentials behind a misleading "missing env" result.
    values = config(Path(__file__).resolve().parents[1] / ".env")
    base = API_BASE_URL.rstrip("/")
    api_key = values.get("api-key", "")
    model = API_MODEL
    if not base.startswith(("https://", "http://")) or not api_key or "://" in model:
        print(json.dumps({"ok": False, "error": "Need url, api-key, and a model ID in ../.env"}))
        return 2

    tools = [{
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
    }]
    initial_input = [{"role": "user", "content": "Call echo_probe exactly once with text TOOL_OK. Do not answer before calling it."}]
    endpoint = base + "/responses"
    started = time.perf_counter()
    report = {"ok": False, "native_tools": False, "continuation": False, "model": model}
    try:
        first = _post(endpoint, api_key, {"model": model, "input": initial_input, "tools": tools,
                                          "parallel_tool_calls": False, "stream": False})
        calls = function_calls(first)
        report["first_response_s"] = round(time.perf_counter() - started, 3)
        report["output_types"] = sorted({str(item.get("type")) for item in first.get("output") or []
                                         if isinstance(item, dict)})
        if len(calls) != 1 or calls[0].name != "echo_probe":
            report["error"] = "Provider accepted request but did not return one echo_probe function_call."
            print(json.dumps(report, ensure_ascii=False))
            return 3
        try:
            arguments = json.loads(calls[0].arguments)
        except json.JSONDecodeError:
            arguments = {}
        if arguments.get("text") != "TOOL_OK":
            report["error"] = "Function-call arguments were not the requested TOOL_OK payload."
            print(json.dumps(report, ensure_ascii=False))
            return 4

        report["native_tools"] = True
        transcript = continue_input(initial_input, first, [function_call_output(calls[0].call_id, "TOOL_OK")])
        second = _post(endpoint, api_key, {
            "model": model,
            "input": transcript,
            "tools": tools,
            "parallel_tool_calls": False,
            "stream": False,
        })
        text = str(second.get("output_text") or "").strip()
        if not text:
            text = "".join(
                str(part.get("text") or "")
                for item in second.get("output") or [] if isinstance(item, dict)
                for part in item.get("content") or [] if isinstance(part, dict)
                and part.get("type") in ("output_text", "text")
            ).strip()
        report["continuation"] = text == "TOOL_OK"
        report["ok"] = report["continuation"]
        report["total_s"] = round(time.perf_counter() - started, 3)
        if not report["continuation"]:
            report["error"] = "Function output continuation did not return TOOL_OK exactly."
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 5
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:500]
        report["total_s"] = round(time.perf_counter() - started, 3)
        print(json.dumps(report, ensure_ascii=False))
        return 6


if __name__ == "__main__":
    sys.exit(main())
