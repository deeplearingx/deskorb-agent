"""Provider-neutral adapter for the agent's canonical Responses transcript.

The local planner and tool loop use the Responses-shaped payload already used
by the DeskOrb runtime.  This module translates that payload to the common
Chat Completions protocol for compatible providers and normalizes their tool
calls back to the local representation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PROVIDERS = ("auto", "openai", "responses", "openai-compatible", "qwen", "deepseek")


def is_stream_keepalive_event(event: Any) -> bool:
    """Return whether a provider text delta is transport keepalive data.

    The configured Responses gateway emits a zero-width ``output_text.delta``
    with these markers before real content. It must never enter the assistant
    transcript because structured consumers (for example Office JSON plans)
    need the first byte of the model response to remain meaningful.
    """
    if not isinstance(event, dict):
        return False
    marker = event.get("SSE-Keep-Alive")
    if marker is True or str(marker or "").strip().casefold() in {"true", "1", "yes"}:
        return True
    return str(event.get("item_id") or "").strip().casefold() == "sse-keep-alive"


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    protocol: str
    base_url: str

    @property
    def endpoint(self) -> str:
        suffix = "/responses" if self.protocol == "responses" else "/chat/completions"
        base = self.base_url.rstrip("/")
        return base if base.lower().endswith(suffix) else base + suffix


def normalize_provider(value: str | None) -> str:
    name = str(value or "auto").strip().lower().replace("_", "-")
    aliases = {
        "gpt": "openai",
        "openai-compatible-gpt": "openai",
        "compatible": "openai-compatible",
        "chat-completions": "openai-compatible",
        "chat": "openai-compatible",
        "openai-responses": "responses",
        "responses-compatible": "responses",
        "dashscope": "qwen",
        "aliyun": "qwen",
    }
    name = aliases.get(name, name)
    return name if name in PROVIDERS else "auto"


def provider_profile(provider: str | None, base_url: str | None) -> ProviderProfile:
    """Resolve a provider protocol and a safe default endpoint locally."""
    base = str(base_url or "").strip().rstrip("/")
    requested = normalize_provider(provider)
    lowered = base.lower()
    if requested == "auto":
        if "deepseek.com" in lowered:
            requested = "deepseek"
        elif "dashscope.aliyuncs.com" in lowered or "qwen" in lowered:
            requested = "qwen"
        elif "api.openai.com" in lowered:
            requested = "openai"
        else:
            # Keep local DeskOrb's historical default for private Responses
            # gateways; compatible chat gateways can opt in explicitly.
            requested = "responses"
    defaults = {
        "openai": "https://api.openai.com/v1",
        "responses": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "openai-compatible": "http://127.0.0.1:8000/v1",
    }
    # API_BASE_URL historically defaults to OpenAI.  An explicit provider must
    # still be usable when only its API key is configured.
    if requested in {"deepseek", "qwen"} and base.lower() == "https://api.openai.com/v1":
        base = ""
    protocol = "responses" if requested in {"openai", "responses"} else "chat_completions"
    return ProviderProfile(requested, protocol, base or defaults[requested])


class ModelAdapter:
    """Translate canonical requests and replies for one provider."""

    def __init__(self, provider: str | None, base_url: str | None):
        self.profile = provider_profile(provider, base_url)

    @property
    def provider(self) -> str:
        return self.profile.name

    @property
    def protocol(self) -> str:
        return self.profile.protocol

    @property
    def endpoint(self) -> str:
        return self.profile.endpoint

    def prepare_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.protocol == "responses":
            return dict(payload)
        body: dict[str, Any] = {
            "model": payload.get("model"),
            "messages": _to_chat_messages(payload.get("instructions"), payload.get("input") or []),
            "stream": bool(payload.get("stream")),
        }
        if payload.get("tools"):
            body["tools"] = [_to_chat_tool(tool) for tool in payload["tools"] if isinstance(tool, dict)]
            body["tool_choice"] = "auto"
        if payload.get("max_output_tokens") is not None:
            body["max_tokens"] = payload["max_output_tokens"]
        return body

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Return the Responses-shaped result expected by the local runtime."""
        if self.protocol == "responses":
            return response
        choices = response.get("choices") if isinstance(response, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        if not isinstance(message, dict):
            message = {}
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        output: list[dict[str, Any]] = []
        if text:
            output.append({"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": text}]})
        for index, call in enumerate(message.get("tool_calls") or []):
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                continue
            function = call["function"]
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            arguments = function.get("arguments")
            output.append({"type": "function_call", "call_id": str(call.get("id") or f"call_{index}"),
                           "name": name,
                           "arguments": arguments if isinstance(arguments, str) else "{}"})
        return {"output_text": text, "output": output, "provider": self.provider}


def _to_chat_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": str(tool.get("name") or ""),
        "description": str(tool.get("description") or ""),
        "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), dict)
        else {"type": "object", "properties": {}},
    }}


def _to_chat_messages(instructions: Any, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    pending_calls: list[dict[str, Any]] = []

    def flush_calls() -> None:
        nonlocal pending_calls
        if pending_calls:
            messages.append({"role": "assistant", "content": None, "tool_calls": pending_calls})
            pending_calls = []

    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "function_call":
            pending_calls.append({"id": str(item.get("call_id") or ""), "type": "function",
                                  "function": {"name": str(item.get("name") or ""),
                                               "arguments": str(item.get("arguments") or "{}")}})
            continue
        if item_type == "function_call_output":
            flush_calls()
            messages.append({"role": "tool", "tool_call_id": str(item.get("call_id") or ""),
                             "content": str(item.get("output") or "")})
            continue
        flush_calls()
        role = str(item.get("role") or "user")
        if role not in {"user", "assistant", "system"}:
            role = "user"
        messages.append({"role": role, "content": _to_chat_content(item.get("content"))})
    flush_calls()
    return messages


def _to_chat_content(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return str(value or "")
    parts: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "")
        if kind in {"input_text", "output_text", "text"}:
            parts.append({"type": "text", "text": str(part.get("text") or "")})
        elif kind in {"input_image", "image_url"}:
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts
