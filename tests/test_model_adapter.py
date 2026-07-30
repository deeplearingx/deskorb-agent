import unittest

from model_adapter import ModelAdapter, provider_profile
from responses_tool_protocol import function_call_output


class ModelAdapterTests(unittest.TestCase):
    def test_auto_detects_official_vendor_protocols(self):
        self.assertEqual(provider_profile("auto", "https://api.deepseek.com").protocol, "chat_completions")
        self.assertEqual(provider_profile("auto", "https://dashscope.aliyuncs.com/compatible-mode/v1").protocol,
                         "chat_completions")
        self.assertEqual(provider_profile("auto", "https://api.openai.com/v1").protocol, "responses")
        self.assertEqual(provider_profile("deepseek", "https://api.openai.com/v1").base_url,
                         "https://api.deepseek.com")

    def test_chat_adapter_converts_tools_and_images(self):
        adapter = ModelAdapter("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        body = adapter.prepare_request({
            "model": "qwen-plus",
            "instructions": "Be precise.",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Inspect this image"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ]}],
            "tools": [{"type": "function", "name": "inspect", "description": "Inspect data",
                       "parameters": {"type": "object", "properties": {}}}],
            "stream": False,
            "max_output_tokens": 321,
        })
        self.assertEqual(adapter.endpoint, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["tools"][0]["function"]["name"], "inspect")
        self.assertEqual(body["max_tokens"], 321)
        self.assertEqual(body["messages"][1]["content"][1]["type"], "image_url")

    def test_chat_adapter_preserves_tool_call_round_trip(self):
        adapter = ModelAdapter("deepseek", "https://api.deepseek.com")
        normalized = adapter.normalize_response({"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "call-weather", "type": "function", "function": {
                "name": "weather", "arguments": '{"city":"Hangzhou"}'}}],
        }}]})
        call = normalized["output"][0]
        self.assertEqual(call["name"], "weather")
        body = adapter.prepare_request({"model": "deepseek-v4-pro", "instructions": "Use tools.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Weather?"}]},
                *normalized["output"],
                function_call_output("call-weather", '{"temperature":26}'),
            ], "stream": False})
        self.assertEqual(body["messages"][2]["role"], "assistant")
        self.assertEqual(body["messages"][2]["tool_calls"][0]["id"], "call-weather")
        self.assertEqual(body["messages"][3]["role"], "tool")
        self.assertEqual(body["messages"][3]["tool_call_id"], "call-weather")

    def test_openai_responses_payload_stays_unchanged(self):
        adapter = ModelAdapter("openai", "https://api.openai.com/v1")
        payload = {"model": "gpt-5", "input": "hello", "stream": False}
        self.assertEqual(adapter.prepare_request(payload), payload)
        self.assertEqual(adapter.endpoint, "https://api.openai.com/v1/responses")


if __name__ == "__main__":
    unittest.main()
