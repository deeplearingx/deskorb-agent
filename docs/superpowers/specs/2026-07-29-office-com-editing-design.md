# Office COM Read, Preview, and Apply Design

## Goal

Extend DeskOrb's one-turn Word attachment into a controlled Office workflow:
read the current Word document or the whole current Excel workbook, let the
Agent propose content changes, show a local preview, and write those changes
only after the user clicks **Apply**. Word and Excel must remain open in their
desktop applications; DeskOrb never saves the file automatically.

## Confirmed scope

- Word: read and edit the main document's body paragraphs and table-cell text.
- Excel: read the current workbook's every worksheet and used range; edit cell
  values and formulas across worksheets.
- No first-release editing of Office formatting, comments, headers/footers,
  pictures, charts, macros, named ranges, merge state, or workbook structure.
- The initial read and every planning request are one-turn ephemeral requests:
  Office content and proposed edits do not enter normal chat memory.
- Any external content change after the read invalidates the pending preview.
- Apply writes to the in-memory Office document only. It never calls Save,
  SaveAs, or closes a document.

## Alternatives considered

1. **Dedicated COM workflow (selected).** It can use the live, unsaved Office
   document, provide structured previews, and verify identity and content
   before writing.
2. Desktop mouse/keyboard automation. It would be vulnerable to focus,
   dialogs, resolution, and UI-language differences, with no reliable preview
   or conflict protection.
3. File-level libraries. They do not reliably reflect the currently open,
   unsaved document and can conflict with Office file locking.

## Architecture

### Snapshot boundary

`office_sources.py` becomes the COM-only boundary. It exposes immutable,
in-memory `OfficeSnapshot` values with:

- Office kind (`word` or `excel`), expected root window handle, display name,
  and document/workbook identity;
- a bounded text representation for the planning request;
- a structural content fingerprint used for conflict detection; and
- typed structural targets used by the writer, not by the UI.

All COM work happens in a daemon worker thread with `CoInitialize`/cleanup.
The active COM application must still resolve to the desktop window focused
before DeskOrb opened. Read-only, protected, password-dialog, missing Office,
and changed-window cases return actionable errors.

The existing `word_sources.py` public reading entry point remains available as
a compatibility wrapper while its COM implementation moves behind the shared
boundary.

#### Word snapshot

The reader enumerates editable main-story paragraphs and table cells. Each
target has a stable, snapshot-local locator (`paragraph:N` or
`table:T/r/c`), its original plain text, and its location label. It creates a
hash from the ordered target values. The planning representation retains the
existing readable Word text while also supplying the target locators needed
for a safe edit plan.

#### Excel snapshot

The reader enumerates every worksheet and its `UsedRange`, producing targets
of `sheet-name + A1 address`, displayed value, and formula where present. It
creates a deterministic hash from worksheet names, addresses, values, and
formulas. A configurable maximum number of non-empty cells and rendered
characters prevents an unbounded workbook from exhausting the chat context;
if exceeded, the read fails rather than silently omitting editable cells.

### Plan generation and validation

`office_edits.py` defines dataclasses and strict parsing for the only plans
that may reach Apply:

- `WordTextEdit(locator, expected_text, replacement_text)` for replacement,
  plus a bounded `word_insert_paragraph_after` variant anchored to the final
  non-empty body paragraph (trailing empty paragraphs and table-cell targets
  are not treated as body anchors);
- `ExcelCellEdit(sheet, address, expected_value, expected_formula,
  value_or_formula)`.

The planning prompt tells the selected backend to return an `OFFICE_EDIT_PLAN`
JSON envelope containing a user-facing answer and either an empty edit list or
a plan. The request carries the snapshot only ephemerally. A specialized
worker/UI request mode captures the completed raw response, validates the
envelope locally, and never treats free-form model text as a write command.
Malformed JSON, unsupported operations, duplicate targets, targets absent
from the snapshot, invented paragraph locators, or edits whose `expected_*`
values do not match the snapshot are rejected and never displayed as
applicable changes. A legacy end-insertion response that points exactly one
paragraph beyond the snapshot is accepted only when its expected value is
empty and the existing final paragraph can be verified as the anchor.

This workflow works with the existing Agent, API, and Codex backends for
planning. The local UI, not the model, owns writing to Office.

### UI and lifecycle

The Chat attachment row contains **Read current Word**, **Read current Excel**,
attachment status, and **Clear**. Only one Office snapshot can be attached at a
time. Reading has the existing background-thread/timeout/late-result guards.

After the user asks a question, DeskOrb displays the model's answer and, when
there is a valid non-empty plan, an embedded preview card. The card shows:

- source document/workbook name and snapshot time;
- Word target labels with before/after text; or Excel sheet/cell addresses with
  before/after values or formulas;
- edit count, skipped unsupported scope, and the statement that the file will
  not be saved automatically; and
- **Apply changes** and **Discard** actions.

**Apply changes** is an explicit, single-use local confirmation. It is disabled
while an Office read, request, or apply is active. Discard clears the plan and
all Office text from memory. The normal transcript contains the user question,
document/workbook metadata, and concise edit summary only; never the full
Office content.

### Apply transaction

`office_edits.py` reopens the active COM object only after confirming it is the
same focused Office window and document/workbook identity. It rereads the
structural content and compares its fingerprint to the snapshot before any
mutation. A mismatch reports that the document changed and requires a new
read and preview.

For Word, each locator must still hold its `expected_text`; the writer then
replaces that complete paragraph or table-cell text. A final-paragraph insert
creates one new body paragraph before the anchor's paragraph marker and copies
nearby text formatting. For Excel, each target cell must still have its
expected value/formula; the writer assigns either a formula or a value. The
writer validates every target before the first write, then applies the plan and
rereads all edited targets to verify the result.

Office COM does not provide a portable rollback for arbitrary live document
edits. Therefore the implementation validates all preconditions before writing
and stops on the first COM failure. It reports any partial-apply result clearly
and never saves automatically, so the user retains Word/Excel Undo and the
choice to save or discard the document.

## Safety and privacy

- Office content is read only after the user explicitly presses a Read button.
- Model output can propose edits but cannot execute them; only the local Apply
  button can do that.
- Apply checks Office kind, window identity, document/workbook identity, and
  content fingerprint immediately before writing.
- Protected/read-only documents and protected Excel sheets are rejected before
  a plan is offered or applied.
- No operation calls `Save`, `SaveAs`, `Close`, or modifies document formatting.
- Existing normal Agent approval and desktop safeguards remain unchanged.

## Tests

Add mocked-COM unit coverage for:

1. Word paragraph/table and Excel multi-sheet snapshot extraction, limits,
   window identity, and COM cleanup.
2. Fingerprint conflicts, protected/read-only rejection, and no-save behavior.
3. Strict plan parsing: malformed plans, unsupported operations, stale values,
   duplicate targets, and out-of-snapshot targets are rejected.
4. Word and Excel apply prevalidation, write calls, read-back verification, and
   partial failure reporting.
5. Chat lifecycle: exactly one snapshot, ephemeral planning request, no Office
   full-text transcript leak, preview actions, discard, and single-use Apply.
6. Existing Word one-turn attachment behavior and all maintained tests remain
   covered without live Office, an API key, or GUI interaction.

## Acceptance criteria

- A user can explicitly attach the current Word document or Excel workbook.
- A modification request produces a readable, validated preview before any
  Office mutation.
- Apply changes only the displayed, fingerprint-validated targets and never
  auto-saves the document.
- A changed, protected, read-only, wrong-window, malformed, or oversized source
  cannot be written through this workflow.
- Word/Excel full text and edit-plan payloads remain outside normal chat memory
  and transcript content.
