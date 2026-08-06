import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import provider_longrun_probe as probe
from model_registry import FallbackTarget, ModelHealthStore


class ProviderLongrunProbeTests(unittest.TestCase):
    def test_probe_can_persist_privacy_safe_health_history(self):
        fallback = FallbackTarget("deepseek", "deepseek", "deepseek-chat",
                                  "https://api.deepseek.com", privacy_level="third_party")

        def fake_health_check(runtime):
            runtime.model_health_store.record(
                provider=runtime.adapter.provider, model=runtime.model, ok=True,
                latency_ms=123, operation="health", status_code=200)
            return {"ok": True, "latency_ms": 123, "error_kind": None}

        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "health.sqlite3"
            with patch.object(probe, "MODEL_FALLBACKS", [fallback]), \
                 patch.object(probe, "get_api_key", return_value="configured-key"), \
                 patch.object(probe, "get_explicit_provider_api_key", return_value="deepseek-key"), \
                 patch.object(probe.AgentRuntime, "health_check", fake_health_check):
                result = probe.run(2, 0, history)
            self.assertTrue(result["ok"])
            self.assertEqual(result["scored_targets"], 2)
            self.assertEqual(result["passed_targets"], 2)
            self.assertEqual(result["history_path"], history.name)
            store = ModelHealthStore(history)
            self.assertEqual(store.summary("deepseek", "deepseek-chat",
                                           operation="health")["samples"], 2)
            self.assertNotIn("configured-key", str(result))


if __name__ == "__main__":
    unittest.main()
