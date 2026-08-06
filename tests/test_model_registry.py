import unittest
import tempfile
from pathlib import Path

from model_adapter import provider_profile
from model_registry import (FallbackTarget, ModelCapabilities, ModelHealthStore,
                             ProviderCapabilityRegistry, capabilities_for, parse_capability_overrides,
                             parse_fallback_targets, route_model)


class ModelRegistryTests(unittest.TestCase):
    def test_supported_provider_has_a_safe_capability_declaration(self):
        capability = capabilities_for(provider_profile("deepseek", "https://api.deepseek.com"))
        self.assertTrue(capability.supports_tools)
        self.assertEqual(capability.protocol, "chat_completions")
        self.assertNotIn("key", capability.safe_dict())

    def test_router_rejects_undeclared_required_capability_without_fallback(self):
        capability = ModelCapabilities("private", "chat_completions", False, True, True)
        decision = route_model("private-model", capability, needs_tools=True)
        self.assertFalse(decision.accepted)
        self.assertIn("tool-call", decision.reason)

    def test_router_accepts_model_that_declares_needed_capabilities(self):
        capability = ModelCapabilities("private", "responses", True, True, True)
        self.assertTrue(route_model("private-model", capability, needs_tools=True, needs_vision=True).accepted)

    def test_registry_and_health_store_keep_explainable_history(self):
        registry = ProviderCapabilityRegistry()
        capability = ModelCapabilities("fixture", "chat_completions", True, False, True)
        registry.register(capability)
        self.assertEqual(registry.catalog()[0]["provider"], "fixture")
        with tempfile.TemporaryDirectory() as directory:
            store = ModelHealthStore(Path(directory) / "health.sqlite3")
            store.record(provider="fixture", model="fixture-1", ok=True, latency_ms=50)
            store.record(provider="fixture", model="fixture-1", ok=False, latency_ms=100,
                         failure_kind="transient_network")
            summary = store.summary("fixture", "fixture-1")
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["success_rate"], 0.5)
        self.assertEqual(summary["p95_latency_ms"], 100.0)

    def test_health_history_exposes_safe_request_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ModelHealthStore(Path(directory) / "health.sqlite3", retention=20)
            store.record(provider="fixture", model="fixture-1", ok=True, latency_ms=250,
                         operation="model_turn", first_token_ms=80, status_code=200,
                         retry_count=1, tool_rounds=3, verification_passed=True)
            summary = store.summary("fixture", "fixture-1", operation="model_turn")
        self.assertEqual(summary["samples"], 1)
        self.assertEqual(summary["p50_first_token_ms"], 80.0)
        self.assertEqual(summary["retry_rate"], 1.0)
        self.assertEqual(summary["verification_rate"], 1.0)
        self.assertEqual(summary["status_codes"], {"200": 1})

    def test_health_history_is_bounded_per_provider_and_model(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ModelHealthStore(Path(directory) / "health.sqlite3", retention=20)
            for index in range(25):
                store.record(provider="fixture", model="fixture-1", ok=True, latency_ms=index)
            summary = store.summary("fixture", "fixture-1", limit=100)
        self.assertEqual(summary["samples"], 20)
        self.assertEqual(summary["p50_latency_ms"], 15.0)

    def test_fallback_catalog_is_bounded_and_never_accepts_url_credentials(self):
        targets = parse_fallback_targets({"targets": [
            {"id": "deepseek", "provider": "deepseek", "model": "deepseek-chat",
             "base_url": "https://api.deepseek.com", "privacy_level": "third_party"},
            {"id": "bad", "provider": "openai", "model": "x",
             "base_url": "https://user:secret@example.test/v1"},
        ]})
        self.assertEqual([item.target_id for item in targets], ["deepseek"])
        safe = targets[0].safe_dict(key_configured=False)
        self.assertNotIn("secret", str(safe))
        self.assertTrue(safe["requires_confirmation"])

    def test_fallback_target_declares_capabilities(self):
        target = FallbackTarget("fixture", "openai-compatible", "fixture-model",
                                "http://127.0.0.1:8000/v1", supports_tools=False)
        self.assertFalse(route_model(target.model, target.capabilities, needs_tools=True).accepted)

    def test_fallback_boolean_strings_are_parsed_safely(self):
        targets = parse_fallback_targets([{"id": "fixture", "provider": "openai-compatible",
                                           "model": "fixture", "base_url": "http://127.0.0.1:8000/v1",
                                           "supports_tools": "false"}])
        self.assertFalse(targets[0].supports_tools)

    def test_qwen_fallback_uses_chat_completions_profile(self):
        targets = parse_fallback_targets([{
            "id": "qwen", "provider": "qwen", "model": "qwen-plus",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }])
        self.assertEqual(targets[0].capabilities.provider, "qwen")
        self.assertEqual(targets[0].capabilities.protocol, "chat_completions")

    def test_gpt_alias_is_accepted_in_fallback_catalog(self):
        targets = parse_fallback_targets([{
            "id": "gpt-backup", "provider": "gpt", "model": "gpt-5",
            "base_url": "https://api.openai.com/v1",
        }])
        self.assertEqual(targets[0].capabilities.provider, "openai")

    def test_per_model_capability_override_is_selected_before_provider_default(self):
        overrides = parse_capability_overrides({"models": [{
            "provider": "deepseek", "model": "deepseek-chat", "base_url": "https://api.deepseek.com",
            "supports_tools": False, "supports_vision": False, "privacy_level": "third_party",
        }]})
        registry = ProviderCapabilityRegistry()
        for model, capability in overrides:
            registry.register_model(model, capability)
        selected = registry.for_profile(provider_profile("deepseek", "https://api.deepseek.com"), "deepseek-chat")
        self.assertFalse(selected.supports_tools)
        self.assertFalse(selected.supports_vision)
        self.assertEqual(selected.privacy_level, "third_party")


if __name__ == "__main__":
    unittest.main()
