import unittest

from responses_tool_protocol import continue_input, function_call_output, function_calls


class ResponsesToolProtocolTests(unittest.TestCase):
    def test_extracts_complete_function_calls_only(self):
        response = {
            "output": [
                {"type": "reasoning", "id": "rs_1"},
                {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                 "name": "echo_probe", "arguments": '{"text":"TOOL_OK"}'},
                {"type": "function_call", "name": "invalid"},
            ]
        }
        calls = function_calls(response)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "echo_probe")
        self.assertEqual(calls[0].arguments, '{"text":"TOOL_OK"}')

    def test_builds_function_call_output(self):
        self.assertEqual(
            function_call_output("call_1", "TOOL_OK"),
            {"type": "function_call_output", "call_id": "call_1", "output": "TOOL_OK"},
        )

    def test_stateless_continuation_preserves_all_model_output(self):
        initial = [{"role": "user", "content": "Call the probe"}]
        response = {"output": [
            {"type": "reasoning", "id": "rs_1"},
            {"type": "function_call", "call_id": "call_1", "name": "echo_probe",
             "arguments": '{"text":"TOOL_OK"}'},
        ]}
        transcript = continue_input(initial, response, [function_call_output("call_1", "TOOL_OK")])
        self.assertEqual(transcript[0], initial[0])
        self.assertEqual(transcript[1], response["output"][0])
        self.assertEqual(transcript[-1]["type"], "function_call_output")


if __name__ == "__main__":
    unittest.main()
