import importlib
import os
import unittest
from unittest.mock import patch


class WorkflowConfigurationTests(unittest.TestCase):
    def test_workflow_text_limit_defaults_to_100000_and_can_be_overridden(self):
        import config

        with patch.dict(os.environ, {"CODEX_OVERLAY_WORKFLOW_TEXT_LIMIT": "150000"}, clear=False):
            config = importlib.reload(config)
            self.assertEqual(config.WORKFLOW_TEXT_LIMIT, 150_000)

        with patch.dict(os.environ, {"CODEX_OVERLAY_WORKFLOW_TEXT_LIMIT": "invalid"}, clear=False):
            config = importlib.reload(config)
            self.assertEqual(config.WORKFLOW_TEXT_LIMIT, 100_000)

        importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
