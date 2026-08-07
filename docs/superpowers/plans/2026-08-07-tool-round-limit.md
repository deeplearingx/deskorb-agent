# Configurable Agent Tool Round Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise the DeskOrb Agent tool-loop default from 20 to 100 rounds and make the limit configurable for complex OfficeCLI tasks without removing cancellation or process-safety protections.

**Architecture:** Keep the existing `AgentRuntime._run_task_loop` guard, but move its limit into `config.py`. The value is read once at process startup from `DESKORB_AGENT_MAX_TOOL_ROUNDS`, clamped to 20–500, and exposed as `AgentRuntime.MAX_TOOL_ROUNDS`. No MCP or OfficeCLI command behavior changes.

**Tech Stack:** Python 3.10+, existing `unittest` tests, PowerShell on Windows.

## Global Constraints

- Preserve the existing OfficeCLI MCP routing and COM workflows.
- Preserve cancellation, API timeout, process termination, and MCP error handling.
- Do not make the tool loop truly infinite.
- Default `DESKORB_AGENT_MAX_TOOL_ROUNDS` to `100` and clamp it to `20`–`500`.
- Keep the generated `Warhammer40K.pptx` artifact outside source changes.

---

### Task 1: Add the configurable limit and regression tests

**Files:**

- Modify: `config.py`
- Modify: `agent_runtime.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_agent_runtime.py`

**Interfaces:**

- Produces `config.API_MAX_TOOL_ROUNDS: int`.
- `AgentRuntime.MAX_TOOL_ROUNDS` reads `API_MAX_TOOL_ROUNDS` at import time.
- Existing callers continue to use `AgentRuntime` without constructor changes.

- [ ] **Step 1: Write the failing configuration test**

Add a test asserting that the default is 100 and that the environment parser clamps low and high values:

```python
def test_agent_tool_round_limit_has_safe_default_and_bounds(self):
    self.assertEqual(config.API_MAX_TOOL_ROUNDS, 100)
    self.assertEqual(config._env_int("DESKORB_AGENT_MAX_TOOL_ROUNDS", 100, 20, 500), 100)
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```powershell
& `"D:\Develop\deskorb-agent-main\.venv\Scripts\python.exe`" -m unittest tests.test_config.VisualCompatibilityDefaultsTests.test_agent_tool_round_limit_has_safe_default_and_bounds -v
```

Expected: failure because `config.API_MAX_TOOL_ROUNDS` does not exist.

- [ ] **Step 3: Write the failing runtime-limit test**

Add a regression assertion that the runtime consumes the configured value rather than the old literal:

```python
def test_agent_runtime_uses_configured_tool_round_limit(self):
    self.assertEqual(AgentRuntime.MAX_TOOL_ROUNDS, API_MAX_TOOL_ROUNDS)
```

Import `API_MAX_TOOL_ROUNDS` from `config` in the test module.

- [ ] **Step 4: Run the runtime test and verify it fails**

Run:

```powershell
& `"D:\Develop\deskorb-agent-main\.venv\Scripts\python.exe`" -m unittest tests.test_agent_runtime.ReadOnlyToolsTests.test_agent_runtime_uses_configured_tool_round_limit -v
```

Expected: failure because the runtime still exposes the literal value 20 and the config symbol is absent.

- [ ] **Step 5: Implement the minimal configuration wiring**

Add this value next to the other API limits in `config.py`:

```python
API_MAX_TOOL_ROUNDS = _env_int("DESKORB_AGENT_MAX_TOOL_ROUNDS", 100, 20, 500)
```

Import it in `agent_runtime.py` and replace the class literal:

```python
class AgentRuntime:
    MAX_TOOL_ROUNDS = API_MAX_TOOL_ROUNDS
```

- [ ] **Step 6: Run focused tests and verify they pass**

Run:

```powershell
& `"D:\Develop\deskorb-agent-main\.venv\Scripts\python.exe`" -m unittest tests.test_config tests.test_agent_runtime -v
```

Expected: all focused tests pass.

- [ ] **Step 7: Verify the environment override in a fresh interpreter**

Run:

```powershell
$env:DESKORB_AGENT_MAX_TOOL_ROUNDS = "150"
& `"D:\Develop\deskorb-agent-main\.venv\Scripts\python.exe`" -c "import config; print(config.API_MAX_TOOL_ROUNDS)"
$env:DESKORB_AGENT_MAX_TOOL_ROUNDS = "9999"
& `"D:\Develop\deskorb-agent-main\.venv\Scripts\python.exe`" -c "import config; print(config.API_MAX_TOOL_ROUNDS)"
Remove-Item Env:DESKORB_AGENT_MAX_TOOL_ROUNDS -ErrorAction SilentlyContinue
```

Expected output: `150` followed by `500`.

- [ ] **Step 8: Commit the implementation**

```powershell
git add config.py agent_runtime.py tests/test_config.py tests/test_agent_runtime.py
git commit -m "feat: make agent tool round limit configurable"
```

### Task 2: Document and verify complex OfficeCLI execution

**Files:**

- Modify: `README.md`
- Test: `tests/officecli_mcp_probe.py`

**Interfaces:**

- Users can set `DESKORB_AGENT_MAX_TOOL_ROUNDS` before launching DeskOrb.
- The existing OfficeCLI probe remains unchanged and continues to validate DOCX/XLSX/PPTX MCP behavior.

- [ ] **Step 1: Add configuration documentation**

Document the default and override:

```text
DESKORB_AGENT_MAX_TOOL_ROUNDS: maximum Agent/MCP tool rounds per task (default 100, allowed 20-500)
```

- [ ] **Step 2: Run the complete focused regression suite**

Run:

```powershell
$env:DESKORB_AGENT_OFFICECLI_BINARY = "D:\Develop\deskorb-agent-main\tools\officecli\officecli.exe"
& `"D:\Develop\deskorb-agent-main\.venv\Scripts\python.exe`" -m unittest tests.test_model_adapter tests.test_provider_env tests.test_credential_store tests.test_worker tests.test_mcp_client tests.test_agent_policy tests.test_config tests.test_agent_runtime -q
```

Expected: all code-related tests pass; a missing PowerShell 7 installation may leave the pre-existing shell-runner test as an environment-only failure.

- [ ] **Step 3: Run the OfficeCLI real MCP probe**

```powershell
$env:DESKORB_AGENT_OFFICECLI_BINARY = "D:\Develop\deskorb-agent-main\tools\officecli\officecli.exe"
& `"D:\Develop\deskorb-agent-main\.venv\Scripts\python.exe`" tests\officecli_mcp_probe.py
```

Expected: DOCX, XLSX, PPTX creation, validation, reads, and PPTX screenshot all pass.

- [ ] **Step 4: Commit the documentation**

```powershell
git add README.md
git commit -m "docs: document configurable agent tool rounds"
```

### Task 3: Merge the verified change back to local main

**Files:**

- Modify: local branch history only

- [ ] **Step 1: Verify the isolated worktree is clean**

```powershell
git status --short --branch
```

Expected: clean `codex/tool-rounds` worktree.

- [ ] **Step 2: Merge the feature branch into local main**

```powershell
git -c safe.directory="D:\Develop\deskorb-agent-main" merge --no-ff --no-edit codex/tool-rounds
```

- [ ] **Step 3: Confirm the existing generated PPT remains untouched**

```powershell
Test-Path -LiteralPath "D:\Develop\deskorb-agent-main\Warhammer40K.pptx"
```

Expected: `True`.
