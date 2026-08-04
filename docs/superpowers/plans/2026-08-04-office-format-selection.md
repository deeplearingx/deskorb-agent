# Office Formatting, Selection, and Undo Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve nearby Word/Excel formatting, select verified edits, and keep Word edits undoable when paragraphs become empty or contain embedded line breaks.

**Architecture:** Keep the existing `office_edits.py` COM boundary and immutable snapshot/history model. Normalize Word line breaks before writing and verification, copy formatting only inside the edited range, and select targets only after verification. Return selection status through `OfficeApplyResult.message`; if COM selection fails, keep the validated DeskOrb preview as the non-mutating fallback.

**Tech Stack:** Python 3.10+, pywin32 Word/Excel COM, Tkinter, `unittest.mock`, existing `OfficeSnapshot`, `OfficeEditPlan`, and `OfficeEditRecord` types.

## Global Constraints

- Office正文、pending plans、format metadata, and history remain memory-only; never add them to normal conversation context, logs, or disk state.
- Apply must revalidate Office identity, fingerprint, and expected target values before writing.
- Word remains limited to body paragraphs/table-cell text; Excel remains limited to existing cell values/formulas.
- DeskOrb never saves, closes, or creates Office files.
- Format-copy and selection failures are non-fatal only after write and verification succeed.
- Existing Clear/reset/new-read generation invalidation remains authoritative.

---

### Task 1: Add failing coverage for formatting, selection, and history

**Files:**
- Modify: `tests/test_office_edits.py`
- Modify: `tests/test_chat_office_attachment.py`

**Interfaces:** Tests target `_write_word_text`, `_verify_targets`, `_validate_live_targets`, `_write_excel_cell`, and `apply_office_plan` with COM-shaped `Mock` objects; no live Office process is used.

- [ ] **Step 1: Write failing Word tests**

Add tests for: embedded `\n` being written in a stable Word target; nearest font properties copied to inserted text; verified Word range selection; empty-paragraph inverse; and no history append after verification failure.

- [ ] **Step 2: Write failing Excel and UI tests**

Add tests that an Excel `Value2`/`Formula` write does not clear a sentinel cell format, a verified cell calls `Select()`, and a selection fallback appears in `OfficeApplyResult.message` without clearing the attachment/history.

- [ ] **Step 3: Run the focused tests and confirm RED**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_office_edits tests.test_chat_office_attachment -v
```

Expected: the new format/focus tests fail because the current COM path does not copy formatting or select verified targets.

- [ ] **Step 4: Commit the tests**

```powershell
git add tests/test_office_edits.py tests/test_chat_office_attachment.py
git commit -m "test: cover Office formatting and selection"
```

---

### Task 2: Implement Word formatting inheritance and selection

**Files:**
- Modify: `office_edits.py`
- Test: `tests/test_office_edits.py`

**Interfaces:**
- Add `_word_text_for_write(value: str) -> str` to map normalized `\n`/`\r` to Word-safe in-range line breaks while leaving the paragraph terminator outside the writable range.
- Add `_copy_word_format(source_range: Any, target_range: Any) -> None` to copy `Font.Name`, `Size`, `Color`, `ColorIndex`, `Bold`, `Italic`, `Underline`, `HighlightColorIndex`, `StrikeThrough`, `Subscript`, and `Superscript`, ignoring optional unavailable COM properties.
- Add `_focus_word_target(document: Any, locator: str) -> bool` to select the target without its paragraph marker, collapsing to an insertion point when empty.
- Change `_verify_targets(...) -> bool` to return whether every verified target was selected; selection errors must not turn a verified edit into a failed Apply.

- [ ] **Step 1: Normalize embedded Word line breaks**

Use `_word_text_for_write` in `_write_word_text`; keep `_read_word_text` and expected comparison on `_normalize_word_text`. This keeps a replacement with embedded line breaks in one stable `paragraph:<n>` target and fixes the repeated `Office did not verify` error for summary text.

- [ ] **Step 2: Copy the nearest text format**

For append/prepend/replacement, choose the nearest existing character (falling back to the target range when empty), write only within the validated range, then copy the safe font properties to the newly written range. Do not modify document-wide styles or unrelated ranges.

- [ ] **Step 3: Select only after equality verification**

After each Word target matches, call `_focus_word_target`. Accumulate selection success and compose the existing result message with either `Changed Word content was selected.` or `Word selection was unavailable; keep the DeskOrb preview as the change marker.`

- [ ] **Step 4: Run and commit**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_office_edits -v
git add office_edits.py tests/test_office_edits.py
git commit -m "feat: preserve Word formatting and select edits"
```

---

### Task 3: Preserve and select Excel cell edits

**Files:**
- Modify: `office_edits.py`
- Test: `tests/test_office_edits.py`

**Interfaces:** Add `_focus_excel_cell(workbook: Any, locator: str) -> bool` using `_resolve_excel_cell` and `Range.Select()`. `_write_excel_cell` must continue assigning only `Value2` or `Formula`; it must not clear formats or recreate the cell.

- [ ] **Step 1: Add format-preservation coverage**

Use a fake cell with sentinel `NumberFormat`, `Font`, and `Interior`; assert that only `Value2` or `Formula` changes.

- [ ] **Step 2: Add verified-cell focus**

In the Excel branch of `_verify_targets`, compare value/formula first, then call `_focus_excel_cell`. Return `False` on `Select()` failure so the same preview fallback message is used.

- [ ] **Step 3: Run and commit**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_office_edits tests.test_office_sources -v
git add office_edits.py tests/test_office_edits.py
git commit -m "feat: select verified Excel edits"
```

---

### Task 4: Connect Apply status and undo history to the overlay

**Files:**
- Modify: `deskorb_agent.py`
- Modify: `tests/test_chat_office_attachment.py`

**Interfaces:** `_apply_office_plan_bg` continues posting `(generation, refreshed, record, result, error)`; `_handle("office_apply", payload)` appends history only when `error is None` and generation matches.

- [ ] **Step 1: Show selection/fallback status**

Ensure `OfficeApplyResult.message` is displayed after the refreshed snapshot is installed. A selection fallback must not clear the attachment or history.

- [ ] **Step 2: Cover failure and success history**

Simulate a verification error and assert no record is appended. Simulate a later successful Apply and assert the refreshed snapshot and record remain available for the next undo request.

- [ ] **Step 3: Run and commit**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_chat_office_attachment tests.test_worker tests.test_agent_runtime -v
git add deskorb_agent.py tests/test_chat_office_attachment.py
git commit -m "fix: report Office selection and preserve undo history"
```

---

### Task 5: Update documentation and run regression checks

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-04-office-format-selection-design.md`

- [ ] **Step 1: Document the user-visible behavior**

Document that Word inserted/replacement text inherits nearby formatting unless explicitly specified, Apply selects the changed Word target or Excel cell after verification, the DeskOrb preview is the fallback marker, and Clear removes memory-only history. State that an already-failed unrecorded operation cannot be reconstructed by Agent.

- [ ] **Step 2: Run syntax and focused regression checks**

```powershell
.\.venv\Scripts\python.exe -m py_compile office_edits.py deskorb_agent.py worker.py agent_runtime.py
.\.venv\Scripts\python.exe -m unittest tests.test_office_edits tests.test_chat_office_attachment tests.test_office_sources tests.test_word_sources tests.test_chat_word_attachment
```

- [ ] **Step 3: Run the complete maintained suite**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Expected: functional tests pass. If the only failure remains `test_shell_runner_returns_bounded_evidence`, report the documented missing PowerShell 7 prerequisite.

- [ ] **Step 4: Commit documentation and verify the worktree**

```powershell
git add README.md docs/superpowers/specs/2026-08-04-office-format-selection-design.md
git commit -m "docs: document Office formatting and selection"
git status --short --branch
```

Expected: clean worktree on `main`; do not push unless explicitly requested.
