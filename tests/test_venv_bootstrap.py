from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "tools" / "venv_bootstrap.py"


def _load_bootstrap():
    if not SCRIPT.is_file():
        raise AssertionError(f"bootstrap helper is missing: {SCRIPT}")
    spec = importlib.util.spec_from_file_location("deskorb_venv_bootstrap", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("bootstrap helper could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VenvBootstrapTests(unittest.TestCase):
    def test_check_mode_returns_unhealthy_when_venv_is_missing(self):
        with tempfile.TemporaryDirectory() as raw_root:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "check", "--project-root", raw_root],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(1, completed.returncode, completed.stderr)

    def test_current_python_executable_is_healthy(self):
        bootstrap = _load_bootstrap()
        self.assertTrue(bootstrap.is_python_healthy(Path(sys.executable)))

    def test_existing_unlaunchable_interpreter_is_unhealthy(self):
        bootstrap = _load_bootstrap()
        with tempfile.TemporaryDirectory() as raw_root:
            executable = Path(raw_root) / "python.exe"
            executable.write_text("not a Windows executable", encoding="utf-8")
            self.assertFalse(bootstrap.is_python_healthy(executable))

    def test_ensure_reuses_healthy_environment(self):
        bootstrap = _load_bootstrap()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            health_check = mock.Mock(return_value=True)
            creator = mock.Mock()
            executable, recreated = bootstrap.ensure_venv(
                root, health_check=health_check, creator=creator,
            )
        self.assertEqual(root.resolve() / ".venv" / "Scripts" / "python.exe", executable)
        self.assertFalse(recreated)
        creator.assert_not_called()

    def test_ensure_recreates_unhealthy_environment(self):
        bootstrap = _load_bootstrap()
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            health_check = mock.Mock(side_effect=[False, True])
            creator = mock.Mock()
            executable, recreated = bootstrap.ensure_venv(
                root, health_check=health_check, creator=creator,
            )
        self.assertEqual(root.resolve() / ".venv" / "Scripts" / "python.exe", executable)
        self.assertTrue(recreated)
        creator.assert_called_once_with(root.resolve())

    def test_create_venv_raises_with_command_output(self):
        bootstrap = _load_bootstrap()
        failed = subprocess.CompletedProcess([], 1, stdout="creation output", stderr="creation failed")
        runner = mock.Mock(return_value=failed)
        with tempfile.TemporaryDirectory() as raw_root:
            with self.assertRaisesRegex(bootstrap.BootstrapError, "creation failed"):
                bootstrap.create_venv(Path(raw_root), runner=runner)

    def test_ensure_mode_returns_failure_when_creation_fails(self):
        bootstrap = _load_bootstrap()
        with tempfile.TemporaryDirectory() as raw_root:
            stderr = io.StringIO()
            with mock.patch.object(
                bootstrap, "ensure_venv", side_effect=bootstrap.BootstrapError("broken environment")
            ):
                with contextlib.redirect_stderr(stderr):
                    result = bootstrap.main(["ensure", "--project-root", raw_root])
        self.assertEqual(1, result)
        self.assertIn("broken environment", stderr.getvalue())


class BatchEntrypointTests(unittest.TestCase):
    @staticmethod
    def _script(name: str) -> str:
        return (PROJECT_ROOT / name).read_text(encoding="utf-8").replace("/", "\\").lower()

    def test_setup_delegates_to_bootstrap_ensure(self):
        setup = self._script("setup.cmd")
        self.assertIn('python "tools\\venv_bootstrap.py" ensure --project-root "%cd%"', setup)

    def test_setup_checks_python_version_and_runtime_imports(self):
        setup = self._script("setup.cmd")
        self.assertIn("sys.version_info >= (3, 10)", setup)
        self.assertIn("from pil import image; import keyboard; import win32com.client", setup)

    def test_default_launcher_delegates_to_conda_environment(self):
        launcher = self._script("Start DeskOrb Agent.cmd")
        self.assertIn('start deskorb agent conda.cmd', launcher)


if __name__ == "__main__":
    unittest.main()
