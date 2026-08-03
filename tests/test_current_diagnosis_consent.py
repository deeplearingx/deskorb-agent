import contextlib
import io
import json
import unittest
from unittest.mock import patch

from tests import e2e_current_diagnosis_probe


class CurrentDiagnosisConsentTests(unittest.TestCase):
    def test_probe_refuses_without_explicit_current_desktop_consent(self):
        output = io.StringIO()
        with patch.dict(e2e_current_diagnosis_probe.os.environ, {}, clear=False), \
             patch.object(e2e_current_diagnosis_probe.os, "environ", {"PATH": "fixture"}), \
             contextlib.redirect_stdout(output):
            result = e2e_current_diagnosis_probe.main([])
        self.assertEqual(result, 4)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "consent_required")
        self.assertTrue(payload["requires_user_confirmation"])


if __name__ == "__main__":
    unittest.main()
