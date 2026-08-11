import importlib
import unittest


class DesktopUIAModuleTests(unittest.TestCase):
    def test_desktop_uia_module_is_available_to_real_desktop_probes(self):
        module = importlib.import_module("desktop_uia")
        self.assertTrue(callable(module.DesktopUIA))


if __name__ == "__main__":
    unittest.main()
