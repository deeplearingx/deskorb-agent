# Repository Guidelines

## Project Structure & Module Organization

This is a Windows desktop Codex Overlay written in Python. The Tkinter UI and
application entry point are in `claude_overlay.py`; background Codex/API work is
in `worker.py`. Keep configuration defaults and environment parsing in
`config.py` and `provider_env.py`. Platform and credential helpers live in
`win32utils.py` and `credential_store.py`. Put automated tests in `tests/`;
files named `test_*.py` are the regular suite, while `*_probe.py` files are
manual diagnostic utilities.

## Build, Test, and Development Commands

- `setup.cmd` creates `.venv`, installs `requirements.txt`, and checks for the
  Codex CLI. Run it once on a new Windows checkout.
- `Start Codex Overlay.cmd` starts the application without a console window.
- `.venv\Scripts\python.exe claude_overlay.py` runs the UI with a console,
  which is preferable while debugging.
- `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`
  runs the maintained unit-test suite.

## Coding Style & Naming Conventions

Use Python with four-space indentation and preserve the established ordering:
standard-library imports first, then third-party imports, then local modules.
Use `snake_case` for functions, variables, and modules; `PascalCase` for
classes; and `UPPER_SNAKE_CASE` for configuration constants. Keep Windows- or
Tkinter-specific code in its existing boundary modules instead of spreading it
through worker or configuration code. No formatter or linter is configured, so
match the surrounding file and keep changes narrowly scoped.

## Testing Guidelines

Add focused `unittest.TestCase` coverage for behavior changes. Name tests
`test_<expected_behavior>` and use `unittest.mock.patch` for network, Codex
CLI, credential-store, and UI boundaries. Do not rely on a live API key,
network service, or GUI interaction in `test_*.py`; place exploratory checks
in a clearly named `*_probe.py` script.

## Commit & Pull Request Guidelines

This checkout has no commit history to infer an existing convention. Use short,
imperative commit subjects such as `Fix API base URL normalization`. Keep each
commit cohesive. Pull requests should describe the user-visible effect, list
the test command run, link related issues when applicable, and include a
screenshot for UI changes.

## Security & Configuration

Never commit API keys, `.env` files, local state, screenshots, or generated
logs. Prefer the documented `CODEX_OVERLAY_*` environment variables and Windows
Credential Manager for secrets. Preserve sandboxing and confirmation safeguards;
do not add bypass flags for Codex approvals or sandbox controls.
