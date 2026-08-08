# Portable Virtual Environment Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Windows launcher detect and rebuild a copied or otherwise unusable `.venv`, then verify the replacement by running all 220 existing tests plus 10 new startup tests.

**Architecture:** A small standard-library helper in `tools/venv_bootstrap.py` owns interpreter probing and virtual-environment creation. `setup.cmd` and `Start DeskOrb Agent.cmd` remain thin Windows entry points that delegate health decisions to the helper, install dependencies, and stop on any failed prerequisite.

**Tech Stack:** Python 3.10+, `venv`, `subprocess`, `unittest`, Windows batch scripts.

---

## File structure

- Create `tools/venv_bootstrap.py`: testable environment probe, creation logic, and `check`/`ensure` CLI.
- Create `tests/test_venv_bootstrap.py`: seven helper tests and three batch-entry-point contract tests.
- Modify `setup.cmd`: validate system Python, ensure `.venv`, install dependencies, and smoke-import runtime packages.
- Modify `Start DeskOrb Agent.cmd`: run a real environment health check before launching `pythonw.exe`.
- Modify `docs/superpowers/specs/2026-08-08-portable-venv-bootstrap-design.md`: correct the final test count from 220 to 230.

### Task 1: Build the testable virtual-environment bootstrap

**Files:**
- Create: `tests/test_venv_bootstrap.py`
- Create: `tools/venv_bootstrap.py`

- [ ] **Step 1: Write seven failing bootstrap tests**

Create `tests/test_venv_bootstrap.py` with this content:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m unittest discover -s tests -p "test_venv_bootstrap.py" -v
```

Expected: seven failures. The first reports return code `2` instead of `1`; the remaining tests report `bootstrap helper is missing`. These failures reproduce the missing health-check implementation rather than a test syntax problem.

- [ ] **Step 3: Implement the bootstrap helper**

Create `tools/venv_bootstrap.py` with this complete content:

```python
"""Validate or recreate DeskOrb's local Windows virtual environment."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from typing import Callable, Sequence


PROBE_CODE = "import sys; print(sys.executable)"


class BootstrapError(RuntimeError):
    """The virtual environment could not be created or validated."""


def venv_python_path(project_root: str | Path) -> Path:
    return Path(project_root).expanduser().resolve() / ".venv" / "Scripts" / "python.exe"


def is_python_healthy(
    executable: str | Path,
    *,
    timeout_seconds: int = 10,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    path = Path(executable)
    if not path.is_file():
        return False
    try:
        completed = runner(
            [str(path), "-I", "-c", PROBE_CODE],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def create_venv(
    project_root: str | Path,
    *,
    system_python: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    root = Path(project_root).expanduser().resolve()
    python = Path(system_python or sys.executable)
    completed = runner(
        [str(python), "-m", "venv", "--clear", str(root / ".venv")],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "unknown venv error").strip()
        raise BootstrapError(f"Could not create .venv: {detail[:1000]}")


def ensure_venv(
    project_root: str | Path,
    *,
    system_python: str | Path | None = None,
    health_check=None,
    creator=None,
) -> tuple[Path, bool]:
    root = Path(project_root).expanduser().resolve()
    executable = venv_python_path(root)
    check = health_check or is_python_healthy
    make = creator or (lambda value: create_venv(value, system_python=system_python))
    if check(executable):
        return executable, False
    make(root)
    if not check(executable):
        raise BootstrapError(f"Created virtual environment is not runnable: {executable}")
    return executable, True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "ensure"))
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    executable = venv_python_path(args.project_root)
    if args.mode == "check":
        if is_python_healthy(executable):
            print(f"Virtual environment is healthy: {executable}")
            return 0
        print(f"Virtual environment is missing or unhealthy: {executable}", file=sys.stderr)
        return 1
    try:
        _, recreated = ensure_venv(args.project_root)
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    action = "Recreated" if recreated else "Reusing"
    print(f"{action} virtual environment: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```powershell
python -m unittest discover -s tests -p "test_venv_bootstrap.py" -v
```

Expected: `Ran 7 tests` and `OK`.

- [ ] **Step 5: Commit the bootstrap helper**

```powershell
git add -- tools/venv_bootstrap.py tests/test_venv_bootstrap.py
git commit -m "fix: detect copied virtual environments"
```

### Task 2: Wire setup and launch scripts to the health check

**Files:**
- Modify: `tests/test_venv_bootstrap.py`
- Modify: `setup.cmd`
- Modify: `Start DeskOrb Agent.cmd`

- [ ] **Step 1: Add three failing batch-entry-point tests**

Insert this class before the final `if __name__ == "__main__":` block in `tests/test_venv_bootstrap.py`:

```python
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

    def test_launcher_checks_environment_before_starting(self):
        launcher = self._script("Start DeskOrb Agent.cmd")
        check = 'python "tools\\venv_bootstrap.py" check --project-root "%cd%"'
        launch = 'start "" ".venv\\scripts\\pythonw.exe" "deskorb_agent.py"'
        self.assertIn(check, launcher)
        self.assertLess(launcher.index(check), launcher.index(launch))
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m unittest discover -s tests -p "test_venv_bootstrap.py" -v
```

Expected: the seven helper tests pass and the three `BatchEntrypointTests` fail because the existing scripts only test whether `python.exe` exists.

- [ ] **Step 3: Replace `setup.cmd` with the self-healing setup flow**

Use this complete content:

```bat
@echo off
setlocal
cd /d "%~dp0"

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  exit /b 1
)

python "tools\venv_bootstrap.py" ensure --project-root "%CD%"
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -c "from PIL import Image; import keyboard; import win32com.client"
if errorlevel 1 (
  echo DeskOrb Agent dependencies could not be imported.
  exit /b 1
)

echo.
echo DeskOrb Agent is ready.
echo Run: Start DeskOrb Agent.cmd
endlocal
```

- [ ] **Step 4: Replace `Start DeskOrb Agent.cmd` with the health-checked launcher**

Use this complete content:

```bat
@echo off
setlocal
cd /d "%~dp0"

python "tools\venv_bootstrap.py" check --project-root "%CD%" >nul 2>nul
if errorlevel 1 (
  echo DeskOrb Agent virtual environment is missing or unhealthy. Running setup...
  call setup.cmd
  if errorlevel 1 exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "deskorb_agent.py"
endlocal
```

- [ ] **Step 5: Run the focused test and verify GREEN**

Run:

```powershell
python -m unittest discover -s tests -p "test_venv_bootstrap.py" -v
```

Expected: `Ran 10 tests` and `OK`.

- [ ] **Step 6: Commit the entry-point changes**

```powershell
git add -- setup.cmd "Start DeskOrb Agent.cmd" tests/test_venv_bootstrap.py
git commit -m "fix: rebuild unhealthy local environment on startup"
```

### Task 3: Replace the copied environment and verify the full application

**Files:**
- Move temporarily: `.venv` to `.venv.stale-20260808`
- Generate: `.venv`
- Verify: all Python source and tests

- [ ] **Step 1: Verify the exact migration paths**

Run:

```powershell
$projectRoot = (Resolve-Path -LiteralPath 'D:\develop\deskorb-agent-main').Path
$oldVenv = (Resolve-Path -LiteralPath 'D:\develop\deskorb-agent-main\.venv').Path
if (-not $oldVenv.StartsWith($projectRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Refusing to move a path outside the project root.' }
if (Test-Path -LiteralPath 'D:\develop\deskorb-agent-main\.venv.stale-20260808') { throw 'The stale backup target already exists.' }
```

Expected: no output and exit code `0`.

- [ ] **Step 2: Preserve the copied environment**

Run:

```powershell
Move-Item -LiteralPath 'D:\develop\deskorb-agent-main\.venv' -Destination 'D:\develop\deskorb-agent-main\.venv.stale-20260808'
```

Expected: `.venv` is absent and `.venv.stale-20260808` exists.

- [ ] **Step 3: Run the supported setup entry point**

Run:

```powershell
cmd.exe /d /c setup.cmd
```

Expected: the helper reports a recreated environment, pip installs Pillow, keyboard, and pywin32, the import smoke check succeeds, and setup prints `DeskOrb Agent is ready.`

- [ ] **Step 4: Run all 230 tests from the replacement environment**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Expected: `Ran 230 tests` and `OK`. If any test fails, keep the stale backup and debug the first root cause before continuing.

- [ ] **Step 5: Verify source compilation**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall -q -f -x "OfficeCLI-main|\.venv|\.worktrees" .
```

Expected: exit code `0` with no syntax errors.

- [ ] **Step 6: Restart the DeskOrb process and verify it stays alive**

List `pythonw.exe` processes with their executable paths and command lines, stop
only a process whose executable is this project's `.venv` interpreter and whose
command line contains `deskorb_agent.py`, run `Start DeskOrb Agent.cmd`, then
confirm the replacement process remains present after five seconds. Do not stop
unrelated Python processes.

```powershell
$pythonw = (Resolve-Path -LiteralPath 'D:\develop\deskorb-agent-main\.venv\Scripts\pythonw.exe').Path
$existing = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.ExecutablePath -eq $pythonw -and $_.CommandLine -match 'deskorb_agent\.py' }
$existing | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
cmd.exe /d /c "Start DeskOrb Agent.cmd"
Start-Sleep -Seconds 5
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.ExecutablePath -eq $pythonw -and $_.CommandLine -match 'deskorb_agent\.py' }
if (-not $running) { throw 'DeskOrb Agent exited during startup.' }
```

Expected: exactly the project-specific DeskOrb process is running after five seconds.

- [ ] **Step 7: Delete the preserved environment only after every gate passes**

Resolve and verify the backup path again before deletion:

```powershell
$projectRoot = (Resolve-Path -LiteralPath 'D:\develop\deskorb-agent-main').Path
$backup = (Resolve-Path -LiteralPath 'D:\develop\deskorb-agent-main\.venv.stale-20260808').Path
if (-not $backup.StartsWith($projectRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Refusing to delete a path outside the project root.' }
if ((Split-Path -Leaf $backup) -ne '.venv.stale-20260808') { throw 'Unexpected backup directory name.' }
Remove-Item -LiteralPath $backup -Recurse -Force
```

Expected: `.venv.stale-20260808` no longer exists and the working `.venv` remains healthy.

- [ ] **Step 8: Record final repository and environment evidence**

Run:

```powershell
git status --short --branch
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe "tools\venv_bootstrap.py" check --project-root $PWD.Path
```

Expected: the new implementation commits are present, pre-existing unrelated modifications remain untouched, Python reports its version, and the bootstrap helper reports a healthy virtual environment.
