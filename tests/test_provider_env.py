import unittest

from unittest.mock import patch

import provider_env
from provider_env import _parse_lines


class ProviderEnvTests(unittest.TestCase):
    def test_colon_value_can_contain_equals(self):
        values = _parse_lines(["api-key: token=with=equals", "url: https://example.test/v1"])
        self.assertEqual(values["api-key"], "token=with=equals")
        self.assertEqual(values["url"], "https://example.test/v1")

    def test_equals_format_is_supported(self):
        self.assertEqual(_parse_lines(["OPENAI_API_KEY=abc"])["openai_api_key"], "abc")

    def test_standard_openai_key_is_accepted(self):
        with patch.object(provider_env, "_VALUES", {"openai_api_key": "abc"}), \
             patch.dict(provider_env.os.environ, {}, clear=True):
            self.assertEqual(provider_env.api_key(), "abc")

    def test_vendor_specific_api_keys_are_accepted(self):
        with patch.object(provider_env, "_VALUES", {"deepseek_api_key": "deepseek-key"}), \
             patch.dict(provider_env.os.environ, {}, clear=True):
            self.assertEqual(provider_env.api_key(), "deepseek-key")

    def test_provider_specific_key_wins_over_generic_key(self):
        with patch.object(provider_env, "_VALUES", {"openai_api_key": "generic", "deepseek_api_key": "deep"}), \
             patch.dict(provider_env.os.environ, {}, clear=True):
            self.assertEqual(provider_env.api_key("deepseek"), "deep")
