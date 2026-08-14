import unittest
from pathlib import Path
from unittest.mock import Mock

from desktop_app_adapters import DesktopAppAdapters


class FakeRuntime:
    def __init__(self, responses):
        self.working_dir = Path("C:/DeskOrb/temporary")
        self.responses = responses
        self.calls = []

    def _run_local_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        response = self.responses.get(name)
        if callable(response):
            return response(name, arguments, self)
        return response or {"ok": False, "failure_kind": "fixture_missing"}


class DesktopAppAdapterTests(unittest.TestCase):
    def test_notepad_adapter_uses_one_semantic_set_and_exact_readback(self):
        observed = {"ok": True, "uia_observation_id": "O1", "controls": [
            {"control_id": "U1", "name": "Editor", "control_type": "Edit",
             "enabled": True, "actions": ["set_value"]},
        ]}
        runtime = FakeRuntime({
            "application_launch": {"ok": True, "window_handle": 11},
            "desktop_uia_observe": observed,
            "desktop_uia_set_value": {"ok": True, "verified": True,
                                       "postcondition_passed": True,
                                       "postcondition_kind": "uia_value_readback"},
        })
        result = DesktopAppAdapters(runtime).notepad("DeskOrb E2E")
        self.assertTrue(result["ok"])
        self.assertEqual(result["postcondition_kind"], "uia_value_readback")
        self.assertEqual([call[0] for call in runtime.calls], [
            "application_launch", "desktop_uia_observe", "desktop_uia_set_value",
        ])

    def test_explorer_adapter_rejects_paths_outside_the_temporary_root(self):
        runtime = FakeRuntime({})
        result = DesktopAppAdapters(runtime).explorer("C:/Users/Administrator/Documents")
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_kind"], "desktop_path_outside_working_dir")
        self.assertEqual(runtime.calls, [])

    def test_calculator_adapter_requires_exact_display_postcondition(self):
        state = {"step": 0}

        def observe(_name, _arguments, _runtime):
            if state["step"] < 4:
                controls = [
                    {"control_id": "U2", "name": "2", "control_type": "Button",
                     "enabled": True, "actions": ["invoke"]},
                    {"control_id": "UP", "name": "Plus", "control_type": "Button",
                     "enabled": True, "actions": ["invoke"]},
                    {"control_id": "UE", "name": "Equals", "control_type": "Button",
                     "enabled": True, "actions": ["invoke"]},
                ]
            else:
                controls = [{"control_id": "UD", "name": "4", "control_type": "Text",
                             "enabled": True, "actions": []}]
            return {"ok": True, "uia_observation_id": f"O{state['step']}", "controls": controls,
                    "observation_fingerprint": f"F{state['step']}"}

        def invoke(_name, _arguments, _runtime):
            state["step"] += 1
            return {"ok": True, "after_observation": observe("desktop_uia_observe", {}, _runtime)}

        runtime = FakeRuntime({
            "application_launch": {"ok": True, "window_handle": 12},
            "desktop_uia_observe": observe,
            "desktop_uia_invoke": invoke,
        })
        result = DesktopAppAdapters(runtime).calculator("2+2")
        self.assertTrue(result["ok"])
        self.assertEqual(result["postcondition_kind"], "calculator_result_observation")
        self.assertEqual(len([call for call in runtime.calls if call[0] == "desktop_uia_invoke"]), 4)


if __name__ == "__main__":
    unittest.main()
