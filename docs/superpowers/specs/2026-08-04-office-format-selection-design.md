# Office Formatting, Selection, and Undo Reliability Design

## Goal

Make confirmed Word and Excel edits easier to review and undo while preserving
the existing COM safety boundary:

- Word and Excel edits keep their existing preview, stale-source, and Apply
  confirmation requirements.
- Word inserted or replacement content inherits nearby formatting unless the
  user explicitly requests formatting.
- A successful Apply makes the changed Word text or Excel cell visibly selected.
- If selection is unavailable, the implementation keeps the DeskOrb preview as
  a non-mutating visual marker and reports that fallback in the UI.
- Word edits that leave a paragraph empty remain undoable.
- A failed verification never becomes history; a confirmed, verified operation
  always remains available until Clear, a new Office read, or reset.

## Scope and non-goals

In scope:

- Word body paragraphs and table-cell text already supported by `office_edits.py`.
- Excel cell values and formulas already supported by `office_edits.py`.
- Font properties visible in normal Word text (font name, size, color, bold,
  italic, underline, highlight, strike-through, superscript, and subscript),
  plus the nearest paragraph formatting needed for inserted paragraph text.
- Selection of the changed Word target or Excel cell after verification.

Out of scope:

- Word comments, headers/footers, tracked changes, charts, macros, or saving
  files.
- Excel workbook structure, charts, conditional formatting, or formula
  rewriting.
- Persisting Office content or edit history to disk or normal conversation
  context.
- Reconstructing an old edit that failed before it was recorded in memory. The
  user can still use Word/Excel's native Undo for that already-applied change.

## Current failure and root cause

The Word reader omits empty paragraphs from `OfficeSnapshot.targets`. A valid
delete can therefore leave a paragraph locator such as `paragraph:60` absent
from the refreshed snapshot. The inverse planner must treat a missing Word
paragraph locator as an empty string, while the live COM Apply path still
revalidates and resolves the real paragraph range.

Word COM also represents paragraph and manual line breaks differently from the
normalized text sent to the provider. Verification must compare normalized
Word text on both sides so a successful write is not reported as a failure.

## Design

### Word write and format inheritance

The COM write helper classifies the requested change from the snapshot's
expected text and the proposed value:

1. For an append or prepend, it inserts only the changed text into the existing
   range, preserving the original text's formatting.
2. The inserted range copies formatting from the nearest existing character
   (or the nearest paragraph when inserting paragraph text).
3. For a general replacement, it writes the validated replacement and copies
   the nearest available formatting to the replacement range. Existing safety
   checks still require the expected value to match before writing.

The helper copies only COM formatting properties that are safe and meaningful
for this text operation. It does not change document-wide styles or formatting
outside the edited target. If copying a specific optional property is not
available in a Word version, the write continues with the remaining properties
and the existing style inheritance.

### Excel write and format inheritance

Writing a value or formula to an existing Excel cell does not replace the cell's
format, so the current cell style remains intact. The implementation explicitly
avoids clearing or recreating the cell. If a supported target has no existing
format metadata, Excel's current local default remains the fallback; adding a
new-cell style-copy system is deferred because current snapshots expose only
non-empty cells.

### Selection and visual fallback

After all edits are written and verified:

- Word selects the changed target text range, excluding the paragraph marker.
- Excel selects the last changed cell (or the last cell on the final changed
  worksheet when a plan spans sheets).
- Selection is attempted only after verification, so a UI selection failure
  cannot make an unverified edit look successful.
- If COM selection fails, the overlay reports that Office selection was
  unavailable and keeps the already-rendered DeskOrb preview as the
  non-mutating marker. DeskOrb does not add a persistent Office highlight just
  to mark a change, and the fallback does not replace the Apply/Discard decision.

### Undo and history

`OfficeEditRecord` remains in-memory and is appended only after Apply writes,
verifies, and refreshes the snapshot. `inverse_plan()` accepts a missing Word
paragraph as empty when the recorded edit's after-value is empty. Live Apply
still resolves the COM paragraph and rejects a missing or changed real target.

The previous error operation cannot be reconstructed if it was never recorded;
the UI should state this explicitly and recommend native Office Undo. New
successful operations must be undoable until the existing Clear/reset/new-read
boundaries remove their history.

## Data flow

1. Read Word/Excel into the existing in-memory snapshot.
2. Provider proposes a validated plan without receiving normal screenshots or
   conversation memory.
3. UI shows the preview and waits for Apply.
4. Background COM thread validates identity/fingerprint and target values.
5. COM writes while preserving formatting, then reads back normalized values.
6. COM selects the changed target or applies the fallback marker.
7. Background code rereads the Office snapshot and appends the history record.
8. UI keeps the attachment active and reports that the changed target is
   selected/highlighted.

## Error handling

- Stale identity, changed expected values, protected/read-only sources, and
  verification mismatches remain hard failures with no history append.
- A missing Word paragraph is only treated as empty for an inverse plan or
  live validation; COM range resolution remains authoritative before writing.
- Selection/format-copy failures are non-fatal only after the edit has already
  passed write and verification. They produce a concise fallback notice.
- Clear, reset, replacement reads, and stale generation events continue to
  invalidate pending plans and history.

## Tests

Add focused tests for:

- Word line-break normalization during verification.
- Undo of a Word paragraph whose current snapshot omits the now-empty target.
- Word append/replacement format-copy calls and selection range calculation.
- Excel edits retaining the existing cell format and selecting the changed cell.
- Selection failure using the visual fallback without changing Apply semantics.
- Successful Apply appending history; failed verification not appending history.
- Existing Word/Excel stale-source, privacy, no-screenshot, and Clear/late-event
  regressions.
