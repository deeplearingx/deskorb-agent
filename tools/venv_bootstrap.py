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
