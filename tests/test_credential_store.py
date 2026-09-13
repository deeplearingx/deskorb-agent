import os
import unittest
from unittest.mock import patch

import credential_store
import provider_env


class ProviderCredentialTests(unittest.TestCase):
    def test_delete_existing_credential_does_not_read_unbound_error(self):
        with patch.object(credential_store._advapi32, "CredDeleteW", return_value=1):
            credential_store.delete_api_key("openai")

    def test_explicit_provider_targets_are_separate(self):
        self.assertEqual(credential_store._credential_target("deepseek"),
                         "DeskOrbAgent/DeepSeekAPIKey")
        self.assertEqual(credential_store._credential_target("qwen"),
                         "DeskOrbAgent/QwenAPIKey")
        self.assertEqual(credential_store._credential_target("auto"),
                         credential_store.TARGET)

    def test_provider_env_does_not_reuse_openai_key_for_vendor(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "primary", "DEEPSEEK_API_KEY": "",
                                     "QWEN_API_KEY": "", "DASHSCOPE_API_KEY": ""}, clear=True), \
             patch.object(provider_env, "_read_file", return_value={}), \
             patch.object(provider_env, "_file_mtime_ns", return_value=7), \
             patch.object(provider_env, "_ENV_MTIME_NS", 7), \
             patch.object(provider_env, "_VALUES", {}):
            self.assertEqual(provider_env.api_key("deepseek"), "")
            self.assertEqual(provider_env.api_key("qwen"), "")
            self.assertEqual(provider_env.api_key(), "primary")

    def test_explicit_env_key_wins_for_vendor(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deep-key"}, clear=True), \
             patch.object(provider_env, "_read_file", return_value={}), \
             patch.object(provider_env, "_file_mtime_ns", return_value=8), \
             patch.object(provider_env, "_ENV_MTIME_NS", 8), \
             patch.object(provider_env, "_VALUES", {}):
            self.assertEqual(provider_env.explicit_api_key("deepseek"), "deep-key")

    def test_openai_compatible_environment_does_not_use_vendor_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "deep-key"}, clear=True):
            self.assertEqual(credential_store._environment_api_key("openai-compatible"), "")


if __name__ == "__main__":
    unittest.main()
