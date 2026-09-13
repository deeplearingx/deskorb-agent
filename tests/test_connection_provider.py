import unittest
from unittest.mock import patch

from deskorb_agent import _state_provider


class ConnectionProviderTests(unittest.TestCase):
    def test_saved_provider_is_restored_for_connection_settings(self):
        self.assertEqual(_state_provider({"model_provider": "openai-compatible"}),
                         "openai-compatible")
        self.assertEqual(_state_provider({"model_provider": "dashscope"}), "qwen")

    def test_missing_or_unknown_saved_provider_uses_configured_default(self):
        with patch("deskorb_agent.MODEL_PROVIDER", "responses"):
            self.assertEqual(_state_provider({}), "responses")
            self.assertEqual(_state_provider({"model_provider": "not-a-provider"}), "auto")


if __name__ == "__main__":
    unittest.main()
