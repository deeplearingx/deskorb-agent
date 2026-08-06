import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import provider_capability_probe as probe


class ProviderCapabilityProbeTests(unittest.TestCase):
    def test_response_text_reads_responses_output(self):
        self.assertEqual(probe._response_text({
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "TOOL_OK"}]}]
        }), "TOOL_OK")

    def test_missing_required_provider_fails_closed(self):
        targets = [{"id": "primary", "provider": "responses", "model": "gpt", "base_url": "https://example.test/v1"}]
        with patch.object(probe, "_targets", return_value=targets), \
             patch.object(probe, "get_api_key", return_value="primary-key"):
            result = probe.run(1, required_providers=["deepseek"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_required_providers"], ["deepseek"])
        self.assertNotIn("primary-key", str(result))

    def test_fallback_uses_explicit_key_and_reports_success(self):
        target = {"id": "deepseek", "provider": "deepseek", "model": "deepseek-chat",
                  "base_url": "https://api.deepseek.com"}
        with patch.object(probe, "_targets", return_value=[target]), \
             patch.object(probe, "get_explicit_provider_api_key", return_value="deepseek-key"), \
             patch.object(probe, "_probe_once", return_value={"ok": True, "total_latency_ms": 12}):
            result = probe.run(2, required_providers=["deepseek"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["passed_targets"], 1)
        self.assertEqual(result["targets"][0]["summary"]["success_rate"], 1.0)
        self.assertNotIn("deepseek-key", str(result))

    def test_history_records_tool_probe_without_content(self):
        target = {"id": "primary", "provider": "responses", "model": "gpt",
                  "base_url": "https://example.test/v1"}
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "health.sqlite3"
            with patch.object(probe, "_targets", return_value=[target]), \
                 patch.object(probe, "get_api_key", return_value="primary-key"), \
                 patch.object(probe, "_probe_once", return_value={"ok": True, "total_latency_ms": 20,
                                                                     "first_status": 200}):
                result = probe.run(1, history_path=history)
            self.assertTrue(result["ok"])
            self.assertEqual(result["history_path"], history.name)
            self.assertNotIn("primary-key", str(result))

    def test_probe_once_uses_stateless_canonical_continuation(self):
        target = {"id": "qwen", "provider": "qwen", "model": "qwen-plus",
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
        first = {"output": [{"type": "function_call", "call_id": "call-1",
                              "name": "echo_probe", "arguments": '{"text":"TOOL_OK"}'}],
                 "_probe_http_status": 200}
        second = {"output_text": "TOOL_OK", "output": [], "_probe_http_status": 200}
        with patch.object(probe, "_post", side_effect=[first, second]) as post:
            result = probe._probe_once(target, "qwen-key", timeout=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["continuation_strategy"], "stateless_output_only")
        second_payload = post.call_args_list[1].args[2]
        self.assertNotIn("previous_response_id", second_payload)
        self.assertEqual(second_payload["input"][-1]["type"], "function_call_output")

    def test_chat_completions_wire_contract_round_trip(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib protocol hook
                size = int(self.headers.get("Content-Length", "0"))
                requests.append(json.loads(self.rfile.read(size).decode("utf-8")))
                if len(requests) == 1:
                    payload = {"choices": [{"message": {"content": None, "tool_calls": [{
                        "id": "call-local", "type": "function",
                        "function": {"name": "echo_probe", "arguments": '{"text":"TOOL_OK"}'},
                    }]}}]}
                else:
                    payload = {"choices": [{"message": {"content": "TOOL_OK"}}]}
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target = {"id": "qwen", "provider": "qwen", "model": "qwen-plus",
                      "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1"}
            result = probe._probe_once(target, "local-key", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["tools"][0]["function"]["name"], "echo_probe")
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")
        self.assertNotIn("local-key", json.dumps(requests))


if __name__ == "__main__":
    unittest.main()
