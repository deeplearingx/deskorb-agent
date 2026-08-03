# Persistent Office Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one Word or Excel attachment available until Clear and preserve a memory-only edit history that supports safe follow-up edits and undo previews.

**Architecture:** The UI owns a `PersistentOfficeAttachment` containing the latest immutable snapshot, a bounded ordered edit ledger, a generation id, and one pending plan. Each Office-aware request receives a temporary prompt containing the current snapshot and history; ordinary conversation context stores only the user's question and answer. Apply rereads and preflights the live Office document, writes and verifies, then refreshes the snapshot and appends a successful edit record.

**Tech Stack:** Python 3, Tkinter, pywin32 COM, existing Worker/Agent/API/Codex backends, unittest and unittest.mock.

## Global Constraints

- Keep one active Office attachment at a time; a successful new read replaces the previous attachment and history.
- The attachment, Office content, pending plans, and edit history remain memory-only and clear on Clear, reset, shutdown, failed replacement, or stale identity.
- Never put Office full text or history into normal transcript rendering, copy controls, debug logs, or `ConversationContext`.
- Word scope remains body paragraphs/table cells; Excel scope remains values/formulas in every worksheet `UsedRange`.
- Every mutation requires a local preview and explicit **Apply changes**; never call Save, SaveAs, or Close.
- Office-aware requests send no screenshot so text-only models remain usable.
- Changed identity, window, target value/formula, protected/read-only source, malformed plan, or stale generation rejects the operation before writing.
- Preserve existing one-turn Word attachment tests and normal Chat behavior where no persistent Office attachment exists.

---

### Task 1: Add persistent attachment and edit-history models

**Files:**
- Modify: `office_edits.py`
- Modify: `tests/test_office_edits.py`

**Interfaces:**
- Add frozen `OfficeEditRecord(operation_id: str, kind: str, identity: str, edits: tuple[WordTextEdit | ExcelCellEdit, ...])`.
- Add `record_from_plan(plan: OfficeEditPlan, snapshot: OfficeSnapshot, operation_id: str) -> OfficeEditRecord`.
- Add `inverse_plan(record: OfficeEditRecord, snapshot: OfficeSnapshot) -> OfficeEditPlan` that creates Word replacements and Excel value/formula edits whose expected fields equal the record after-values.

- [ ] **Step 1: Write failing history and inverse tests.**

Use literal Word and Excel snapshots. Assert a successful Word replacement records `Old -> New`, and its inverse plan expects `New` and replaces it with `Old`. Assert an Excel formula record inverts to the prior formula and an Excel value record inverts to the prior value.

- [ ] **Step 2: Run the focused tests and confirm the API is absent.**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_office_edits.OfficeHistoryTests -v
```

Expected: import failure for `OfficeEditRecord` or missing history functions.

- [ ] **Step 3: Implement immutable records and inverse plans.**

For each `WordTextEdit`, store the original expected value as `before` and replacement value as `after`; inverse creates `WordTextEdit(locator, after, before)`. For each `ExcelCellEdit`, store the expected value/formula as before and the selected value/formula as after; inverse sets expected fields to after and writes the before representation. Reject inverse generation if the current snapshot target does not equal the record after-value.

- [ ] **Step 4: Run focused tests and commit.**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_office_edits.OfficeHistoryTests -v
git add office_edits.py tests/test_office_edits.py
git commit -m "feat: track Office edit history"
```

### Task 2: Persist the attachment in the UI lifecycle

**Files:**
- Modify: `deskorb_agent.py`
- Modify: `tests/test_chat_office_attachment.py`

**Interfaces:**
- Add `self.office_edit_history: list[OfficeEditRecord]` and `self._office_generation: int`.
- Add `_clear_persistent_office(cancel_read: bool = False) -> None`.
- Add `_append_office_history(record: OfficeEditRecord) -> None`.
- Keep `chat_office_snapshot` after successful send, plan completion, and Apply; only Clear/reset/replacement failure removes it.

- [ ] **Step 1: Write failing lifecycle tests.**

Test that a snapshot remains after `_send_chat_office_attachment` and after a successful `office_apply` event. Test `_clear_persistent_office` removes snapshot, history, pending plan, raw response, and increments generation. Test a second successful read starts with an empty history.

- [ ] **Step 2: Run the focused tests and confirm current one-turn clearing fails them.**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_chat_office_attachment.OfficePersistenceTests -v
```

- [ ] **Step 3: Implement lifecycle state and generation guards.**

Replace post-send clearing with pending-plan cleanup only. On Apply success, keep the attachment, replace the snapshot with a fresh COM snapshot, append the record, and clear only the pending plan. On Discard, clear only the pending plan. Make Clear/reset call `_clear_persistent_office(cancel_read=True)`.

- [ ] **Step 4: Run UI lifecycle and existing Word tests, then commit.**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_chat_office_attachment tests.test_chat_word_attachment -v
git add deskorb_agent.py tests/test_chat_office_attachment.py
git commit -m "feat: keep Office attachment until Clear"
```

### Task 3: Inject persistent Office context without normal-memory leakage

**Files:**
- Modify: `deskorb_agent.py`
- Modify: `worker.py`
- Modify: `agent_runtime.py`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_agent_runtime.py`
- Modify: `tests/test_chat_office_attachment.py`

**Interfaces:**
- Add `CodexWorker.ask_office_context(question: str, office_prompt: str) -> None`.
- Add `AgentRuntime.run_office_context_turn(question: str, office_prompt: str) -> None`.
- Add `_build_persistent_office_prompt(snapshot: OfficeSnapshot, history: list[OfficeEditRecord], question: str) -> str`.

- [ ] **Step 1: Write failing context-isolation tests.**

Assert two Office-aware requests contain the same current document text and the prior edit summary, while `ConversationContext.build_input()` never contains the document text. Assert Office-aware requests send empty image paths and Agent payload tools are `[]`.

- [ ] **Step 2: Run tests and confirm new APIs are absent.**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_worker.WorkerOfficeContextTests tests.test_agent_runtime.ReadOnlyToolsTests.test_persistent_office_context_is_ephemeral -v
```

- [ ] **Step 3: Implement prompt construction and backend routing.**

Build a temporary Office prompt from snapshot rendered text, history locators/before/after values, strict JSON schema, and the user question. Worker routes this request to the existing no-tools ephemeral path for Agent/API/Codex, but uses `question` as the normal context key so only the answer is added to ordinary context. Do not call `ask_office_plan` for persistent context without carrying the history.

- [ ] **Step 4: Update UI send behavior and run focused tests.**

When `chat_office_snapshot` exists, every non-empty user message calls `ask_office_context`; no screenshot paths are passed. A valid answer-only response keeps the attachment and history. Commit:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_worker tests.test_agent_runtime tests.test_chat_office_attachment -v
git add deskorb_agent.py worker.py agent_runtime.py tests/test_worker.py tests/test_agent_runtime.py tests/test_chat_office_attachment.py
git commit -m "feat: inject persistent Office context ephemerally"
```

### Task 4: Support safe follow-up undo and post-Apply refresh

**Files:**
- Modify: `deskorb_agent.py`
- Modify: `office_edits.py`
- Modify: `tests/test_chat_office_attachment.py`
- Modify: `tests/test_office_edits.py`

**Interfaces:**
- Add `history` data to the persistent Office prompt.
- Add `_refresh_office_after_apply(snapshot, plan, result) -> None` or equivalent UI callback that rereads the live source before appending history.
- Keep Apply generation-specific and single-use.

- [ ] **Step 1: Write failing undo and refresh tests.**

After a successful Word Apply, assert history contains one record, the attachment remains, and a subsequent prompt includes the after-value. Assert an inverse request produces a preview from the record. Assert a current target that no longer equals the record after-value rejects inverse Apply and performs no write.

- [ ] **Step 2: Run tests to observe the missing history/refresh behavior.**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_chat_office_attachment.OfficeUndoTests tests.test_office_edits.OfficeHistoryTests -v
```

- [ ] **Step 3: Implement refresh and inverse preflight.**

After Apply returns, call `read_active_office_snapshot` for the same root; append a record only after the reread succeeds and replace the stored snapshot. Use `inverse_plan` when the model requests undo, then run the same all-target preflight before writing. Clear pending state on conflict but preserve the attachment so the user can reread.

- [ ] **Step 4: Run focused Office suite and commit.**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_office_sources tests.test_office_edits tests.test_chat_office_attachment -v
git add deskorb_agent.py office_edits.py tests/test_office_edits.py tests/test_chat_office_attachment.py
git commit -m "feat: support safe Office undo follow-ups"
```

### Task 5: Clear, late events, documentation, and regression

**Files:**
- Modify: `deskorb_agent.py`
- Modify: `README.md`
- Modify: `tests/test_chat_office_attachment.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Clear and reset must invalidate every Office generation.
- README documents persistent retention until Clear, per-request provider visibility, undo previews, and the no-save rule.

- [ ] **Step 1: Write failing late-event and Clear tests.**

Queue a plan completion and Apply result, call Clear, then deliver both events; assert no attachment, history, preview, or transcript Office text is restored. Assert ordinary non-Office turns retain existing screenshot behavior when no attachment is active.

- [ ] **Step 2: Implement generation checks and documentation.**

Include the generation in read, plan, and Apply UI queue payloads. Ignore mismatched events. Update README with the exact Clear lifecycle and memory/provider-retention boundary.

- [ ] **Step 3: Run complete verification.**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_office_sources tests.test_office_edits tests.test_chat_office_attachment tests.test_chat_word_attachment tests.test_worker tests.test_agent_runtime -v
.\.venv\Scripts\python.exe -m py_compile office_sources.py office_edits.py deskorb_agent.py worker.py agent_runtime.py tests\office_com_probe.py
git diff --check
git status --short --branch
```

Expected: all Office and existing targeted tests pass. The known PowerShell-dependent shell test may still fail when `pwsh` is absent; record it as the existing environment issue.

- [ ] **Step 4: Commit final lifecycle regression.**

```powershell
git add deskorb_agent.py README.md tests/test_chat_office_attachment.py tests/test_worker.py
git commit -m "fix: clear stale persistent Office state"
```

## Spec coverage review

- Persistent attachment until Clear: Tasks 2 and 5.
- Memory-only per-request injection and normal-context isolation: Task 3.
- Successful edit ledger, refreshed snapshot, and inverse preview: Tasks 1 and 4.
- Changed/protected/read-only/no-save safeguards: existing Office layers plus Task 4.
- Late events, reset, documentation, and regression: Task 5.
