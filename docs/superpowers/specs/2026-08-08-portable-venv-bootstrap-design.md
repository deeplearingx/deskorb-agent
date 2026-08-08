# Portable Virtual Environment Bootstrap Design

## Goal

Make the existing Windows launch workflow recover automatically when the
project was copied from another computer and `.venv` still contains absolute
paths to the old Python installation. The scope is local reliability: use the
currently installed Python and the existing `requirements.txt`, without adding
a Python-version lock or an exact dependency lock file.

## Confirmed scope

- Keep `setup.cmd` and `Start DeskOrb Agent.cmd` as the user-facing commands.
- Treat a virtual environment as healthy only when its interpreter can execute
  a small probe successfully; file existence alone is insufficient.
- Rebuild a missing or unhealthy `.venv` with the current system Python.
- Install the existing runtime requirements and verify that Pillow, keyboard,
  and pywin32 can be imported before reporting that setup succeeded.
- Preserve the current copied environment as `.venv.stale-20260808` during this
  repair. Delete that backup only after the replacement environment starts and
  all 220 Python unit tests pass.
- Do not change application behavior, backend configuration, model settings,
  OfficeCLI, or the current dependency-version policy.

## Alternatives

1. **Testable Python bootstrap helper plus thin batch wrappers (selected).**
   Put environment probing and creation in `tools/venv_bootstrap.py`. The batch
   files remain the entry points but delegate decisions to code that can be
   covered by `unittest`. This adds one small helper while avoiding duplicated
   and difficult-to-test batch logic.
2. **Inline checks in both batch files.** Run `.venv\\Scripts\\python.exe -c`
   directly and call `python -m venv --clear` on failure. This changes fewer
   files, but duplicates behavior and provides only source-text tests rather
   than a focused behavioral unit.
3. **Manual deletion and recreation only.** This repairs the current machine,
   but the next copied environment fails in the same way. It does not satisfy
   automatic local recovery and is rejected.

## Components

### `tools/venv_bootstrap.py`

The helper exposes small functions and a command-line interface:

- resolve the expected console and windowed interpreters below a project root;
- probe an interpreter in a bounded subprocess and return unhealthy for a
  missing executable, launch error, timeout, or non-zero result;
- `check` exits successfully only for a healthy `.venv`;
- `ensure` recreates an unhealthy `.venv` with
  `sys.executable -m venv --clear <project>/.venv`, then probes the new
  interpreter and returns a non-zero exit code with an actionable message if
  creation did not produce a working environment.

The helper does not install packages or launch the application. Those remain
explicit orchestration steps in the batch files.

### `setup.cmd`

`setup.cmd` verifies that the system `python` command is Python 3.10 or newer,
then calls the bootstrap helper in `ensure` mode. It uses the resulting virtual
environment interpreter to upgrade pip, install `requirements.txt`, and import
the three required dependency families. Any failure terminates setup with a
non-zero status and does not print the ready message.

### `Start DeskOrb Agent.cmd`

The launcher calls the helper in `check` mode. A failed check runs `setup.cmd`;
if setup fails, startup stops. A healthy environment launches
`deskorb_agent.py` through the virtual environment's `pythonw.exe`, preserving
the current no-console behavior.

## Repair and startup flow

1. Rename the copied `.venv` to `.venv.stale-20260808` after verifying that
   both paths remain inside the project root.
2. Run `setup.cmd` with the system Python 3.14 installation currently available
   on this machine.
3. The bootstrap helper creates and probes a new `.venv`.
4. Setup installs `requirements.txt` and runs the dependency import smoke test.
5. Run the complete `unittest` suite from the new virtual environment.
6. Run the launcher and verify that the DeskOrb process stays alive rather than
   immediately exiting because of an interpreter or import failure.
7. Only after all checks pass, remove `.venv.stale-20260808`.

## Error handling

- A missing system Python reports the existing Python 3.10-or-newer message.
- An unhealthy copied interpreter is a normal repair condition, not a fatal
  setup error.
- Failure to create or probe the replacement environment stops before pip.
- Pip or dependency import failure stops before the application is launched.
- A failed repair leaves `.venv.stale-20260808` available for inspection and
  retry; it is not deleted as part of failure cleanup.

## Testing

Add `tests/test_venv_bootstrap.py` before implementation and observe its
reproduction test fail. Cover these behaviors using temporary directories:

1. A missing virtual-environment interpreter is unhealthy.
2. The current working Python interpreter passes the probe.
3. An interpreter path that exists but cannot execute is unhealthy, reproducing
   the copied-environment defect rather than testing file existence alone.
4. `ensure` invokes environment creation only for an unhealthy environment and
   verifies the resulting interpreter.
5. A healthy environment is reused without recreation.
6. The command-line `check` and `ensure` modes return meaningful exit statuses.

After the focused test is green, run all 220 test methods with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

The final run must report 220 tests executed with zero failures and zero errors.

## Acceptance criteria

- A copied `.venv` whose executable points to a missing Python installation is
  detected as unhealthy even though `python.exe` and `pythonw.exe` files exist.
- Running `setup.cmd` creates a usable replacement environment and installs all
  current runtime dependencies.
- Running `Start DeskOrb Agent.cmd` self-recovers from a missing or unhealthy
  environment and launches the application after successful setup.
- The full test suite reports exactly 220 passing tests.
- The stale backup is deleted only after the new environment, tests, and launch
  verification all succeed.
