import unittest
import os
import tempfile
from pathlib import Path

from unittest.mock import patch

import provider_env
from provider_env import _parse_lines


class ProviderEnvTests(unittest.TestCase):
    def test_project_volcengine_env_is_preferred_over_parent_env(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "volcengine.env").write_text("provider: openai-compatible\n", encoding="utf-8")
            parent = project.parent / ".env"
            parent.write_text("provider: deepseek\n", encoding="utf-8")
            with patch.object(provider_env, "_PROJECT_ROOT", project), \
                 patch.object(provider_env, "_PARENT_ENV_PATH", parent), \
                 patch.dict(provider_env.os.environ, {}, clear=True):
                self.assertEqual(provider_env._resolve_env_path(), (project / "volcengine.env").resolve())

    def test_explicit_env_file_overrides_default_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.env"
            path.write_text("provider: openai-compatible\n", encoding="utf-8")
            with patch.dict(provider_env.os.environ, {"DESKORB_AGENT_ENV_FILE": str(path)}, clear=True):
                self.assertEqual(provider_env._resolve_env_path(), path.resolve())

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
            self.assertEqual(provider_env.api_key("deepseek"), "deepseek-key")

    def test_provider_specific_key_wins_over_generic_key(self):
        with patch.object(provider_env, "_VALUES", {"openai_api_key": "generic", "deepseek_api_key": "deep"}), \
             patch.dict(provider_env.os.environ, {}, clear=True):
            self.assertEqual(provider_env.api_key("deepseek"), "deep")

    def test_openai_compatible_does_not_reuse_another_vendor_key(self):
        with patch.object(provider_env, "_VALUES", {"api-key": "ark-key"}), \
             patch.dict(provider_env.os.environ, {"DEEPSEEK_API_KEY": "deep-key"}, clear=True):
            self.assertEqual(provider_env.api_key("openai-compatible"), "ark-key")

    def test_explicit_provider_key_does_not_use_generic_primary_key(self):
        with patch.object(provider_env, "_VALUES", {"api-key": "generic"}), \
             patch.dict(provider_env.os.environ, {}, clear=True):
            self.assertEqual(provider_env.explicit_api_key("deepseek"), "")

    def test_explicit_qwen_key_uses_dashscope_alias(self):
        with patch.object(provider_env, "_VALUES", {"dashscope_api_key": "qwen-key"}), \
             patch.dict(provider_env.os.environ, {}, clear=True):
            self.assertEqual(provider_env.explicit_api_key("qwen"), "qwen-key")

    def test_provider_can_be_set_in_environment_or_dotenv(self):
        with patch.object(provider_env, "_VALUES", {"provider": "deepseek"}), \
             patch.dict(provider_env.os.environ, {}, clear=True):
            self.assertEqual(provider_env.model_provider(), "deepseek")
        with patch.object(provider_env, "_VALUES", {"provider": "deepseek"}), \
             patch.dict(provider_env.os.environ, {"DESKORB_AGENT_PROVIDER": "qwen"}, clear=True):
            self.assertEqual(provider_env.model_provider(), "qwen")

    def test_dotenv_is_reloaded_after_process_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("api-key: old-key\nurl: https://old.example\n", encoding="utf-8")
            old_mtime = path.stat().st_mtime_ns
            with patch.object(provider_env, "_ENV_PATH", path), \
                 patch.object(provider_env, "_ENV_MTIME_NS", old_mtime), \
                 patch.object(provider_env, "_VALUES", {"api-key": "old-key", "url": "https://old.example"}), \
                 patch.dict(provider_env.os.environ, {}, clear=True):
                self.assertEqual(provider_env.api_key(), "old-key")
                self.assertEqual(provider_env.api_base_url(), "https://old.example")
                path.write_text("api-key: new-key\nurl: https://new.example\n", encoding="utf-8")
                os.utime(path, ns=(old_mtime + 1_000_000, old_mtime + 1_000_000))
                self.assertEqual(provider_env.api_key(), "new-key")
                self.assertEqual(provider_env.api_base_url(), "https://new.example")
