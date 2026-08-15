import json
from queue import Queue
from unittest.mock import patch

from agent_runtime import AgentRuntime
from responses_tool_protocol import function_calls


class _StreamResponse:
    def __init__(self, *frames):
        self._frames = [frame.encode("utf-8") for frame in frames]
        self.headers = {}
        self.closed = False

    def __iter__(self):
        return iter(self._frames)

    def close(self):
        self.closed = True


def _sse(event):
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def test_responses_stream_emits_text_deltas_and_strips_runtime_fields_from_wire():
    events = Queue()
    runtime = AgentRuntime(events, "test", "https://example.test/v1")
    response = _StreamResponse(
        _sse({"type": "response.created"}),
        _sse({"type": "response.output_text.delta", "delta": "Hel"}),
        _sse({"type": "response.output_text.delta", "delta": "lo"}),
        _sse({
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "output_text": "Hello",
                "output": [{
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello"}],
                }],
            },
        }),
    )

    with patch("agent_runtime.urllib.request.urlopen", return_value=response) as open_call:
        result = runtime._request({
            "model": "test",
            "input": [],
            "stream": True,
            "_deskorb_emit_stream": True,
            "_deskorb_stream_channel": "delta",
        }, "key")

    request = open_call.call_args.args[0]
    body = json.loads(request.data.decode("utf-8"))
    assert body["stream"] is True
    assert "_deskorb_emit_stream" not in body
    assert request.get_header("Accept") == "text/event-stream"
    assert result["output_text"] == "Hello"
    assert result["_deskorb_stream_emitted"] is True
    assert [events.get_nowait(), events.get_nowait()] == [("delta", "Hel"), ("delta", "lo")]
    assert response.closed is True


def test_responses_stream_ignores_gateway_keepalive_text_delta():
    events = Queue()
    runtime = AgentRuntime(events, "test", "https://example.test/v1")
    response = _StreamResponse(
        _sse({
            "type": "response.output_text.delta",
            "item_id": "SSE-Keep-Alive",
            "delta": "\u200b",
            "SSE-Keep-Alive": True,
        }),
        _sse({"type": "response.output_text.delta", "delta": "{"}),
        _sse({"type": "response.output_text.delta", "delta": "\"answer\":\"ok\"}"}),
        _sse({"type": "response.completed", "response": {"id": "resp_keepalive"}}),
    )

    with patch("agent_runtime.urllib.request.urlopen", return_value=response):
        result = runtime._request({
            "model": "test", "stream": True,
            "_deskorb_emit_stream": True,
        }, "key")

    assert result["output_text"] == '{"answer":"ok"}'
    assert events.get_nowait() == ("delta", "{")
    assert events.get_nowait() == ("delta", '"answer":"ok"}')
    assert events.empty()


def test_responses_stream_reassembles_function_call_arguments_without_emitting_them():
    runtime = AgentRuntime(Queue(), "test", "https://example.test/v1")
    response = _StreamResponse(
        _sse({
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": "browser_action_batch", "arguments": "",
            },
        }),
        _sse({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1", "delta": '{"actions":',
        }),
        _sse({
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1", "delta": ' [{"action":"snapshot","arguments":{}}]}',
        }),
        _sse({
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "arguments": '{"actions":[{"action":"snapshot","arguments":{}}]}',
        }),
        _sse({"type": "response.completed", "response": {"id": "resp_2", "output": []}}),
    )

    with patch("agent_runtime.urllib.request.urlopen", return_value=response):
        result = runtime._request({"model": "test", "stream": True}, "key")
    calls = function_calls(result)
    assert len(calls) == 1
    assert calls[0].name == "browser_action_batch"
    assert json.loads(calls[0].arguments) == {
        "actions": [{"action": "snapshot", "arguments": {}}],
    }
    assert runtime.ui.empty()


def test_chat_completions_stream_reassembles_tool_call_fragments():
    runtime = AgentRuntime(
        Queue(), "test", "https://example.test/v1", model_provider="openai-compatible",
    )
    response = _StreamResponse(
        _sse({"choices": [{"delta": {
            "tool_calls": [{"index": 0, "id": "call_7", "function": {
                "name": "browser_action_batch", "arguments": '{"actions":',
            }}],
        }, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {
            "tool_calls": [{"index": 0, "function": {
                "arguments": '[]}',
            }}],
        }, "finish_reason": "tool_calls"}]}),
        "data: [DONE]\n\n",
    )

    with patch("agent_runtime.urllib.request.urlopen", return_value=response):
        result = runtime._request({"model": "test", "stream": True}, "key")
    calls = function_calls(result)
    assert len(calls) == 1
    assert calls[0].call_id == "call_7"
    assert calls[0].name == "browser_action_batch"
    assert json.loads(calls[0].arguments) == {"actions": []}
