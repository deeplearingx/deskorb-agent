"""Small, provider-neutral helpers for Responses API function calling.

This module deliberately has no network or local-tool side effects.  It keeps
the wire-format handling in one place so the future Agent Runtime can validate
and replay tool calls without relying on ``previous_response_id`` support from
an API gateway.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FunctionCall:
    """A validated-enough function-call envelope returned by a provider."""

    call_id: str
    name: str
    arguments: str
    item: dict[str, Any]


def function_calls(response: dict[str, Any]) -> list[FunctionCall]:
    """Return complete function calls from a non-streaming Responses payload.

    Arguments remain JSON text here.  The future tool registry owns schema
    validation and JSON decoding, so malformed model input cannot reach a
    local executor merely because it was present in a provider response.
    """
    calls: list[FunctionCall] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments")
        if not isinstance(call_id, str) or not isinstance(name, str):
            continue
        calls.append(FunctionCall(
            call_id=call_id,
            name=name,
            arguments=arguments if isinstance(arguments, str) else "",
            item=item,
        ))
    return calls


def function_call_output(call_id: str, output: str) -> dict[str, str]:
    """Build the standard Responses input item for a completed local tool."""
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def continue_input(initial_input: list[dict[str, Any]], response: dict[str, Any],
                   outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a stateless continuation transcript.

    All provider output items are preserved, not merely function calls.  This
    matters for reasoning-capable models, whose returned reasoning items must
    accompany later tool outputs.
    """
    prior_output = [item for item in response.get("output") or [] if isinstance(item, dict)]
    return [*initial_input, *prior_output, *outputs]
