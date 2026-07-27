# Word Chat Attachment Design

## Goal

Add an explicit, one-turn Microsoft Word document attachment to DeskOrb Agent's
Chat view without changing the existing DeskOrb Agent runtime, configuration
names, workflows, records, or quick actions.

## Scope

The feature lets a user focus an editable Microsoft Word desktop document,
open DeskOrb Agent, choose **Read current Word**, ask a question, and send the
document text only with that reply.

It must not add workflow, record, or quick-action features from the
`word_extraction` branch. It must not remove or replace DeskOrb Agent's
independent Agent backend.

## Architecture

### Word extraction boundary

Create `word_sources.py` as the sole Word COM integration boundary. It exposes
an immutable `WordMaterial` value containing a transient document identifier,
document name, normalized text, character count, unsaved-change state, and
window title. `read_active_word_document(expected_hwnd)` verifies that the
caller-selected Word window belongs to `WINWORD.EXE`, initializes COM in the
background thread, obtains the active Word application, and verifies its
active window resolves to `expected_hwnd` before reading the document.

The extractor reads the main document story and Word table text available
through `ActiveDocument.Content.Text`, normalizes Word paragraph, table-cell,
vertical-tab, and form-feed control characters to readable lines, and rejects
empty documents. It reports user-actionable `WordMaterialError` messages for
protected-view documents, Word dialogs, COM failures, and a changed active
Word window.

### Chat UI and lifecycle

`deskorb_agent.py` adds one small Chat-only attachment row with **Read current
Word**, a status label, and **Clear**. It keeps the selected `WordMaterial` in
memory only. Reading happens on a daemon background thread and posts its
result to the existing UI queue. A 15-second Tk timeout and monotonically
increasing request id make late or cancelled reads harmless.

When an attachment is present, sending requires a non-empty question. The UI
builds a temporary prompt containing the document name, document text, and the
question, while showing only the document name and character count in the chat
transcript. It clears the attachment before dispatching the request, skips
automatic screenshots and pre-captured images, and uses the worker's
ephemeral request API. Reset, Clear, errors, timeouts, and late background
results also clear or ignore the material.

### One-turn worker isolation

`worker.py` adds `ask_ephemeral(text, image_paths=None)` and carries an
`ephemeral` flag through worker dispatch. It must preserve the normal
conversation state for all existing calls.

For the Codex backend, an ephemeral request starts a fresh app-server thread
and sends the turn there without updating the normal `_session_id`. For the
direct Responses API backend, it sends only the temporary prompt and does not
compact, read, or update the ordinary `ConversationContext`. For the DeskOrb
Agent backend, `agent_runtime.py` exposes an ephemeral turn entry point that
does not compact, read, or append to its `ConversationContext`; normal tool and
approval safeguards continue to apply.

## Dependencies

Add `pywin32` to `requirements.txt`. Imports remain lazy inside
`word_sources.py` so startup and tests remain usable on systems without Word;
attempting to read Word without pywin32 gives a clear error.

## Error handling and privacy

- Word can be read only after the user explicitly chooses **Read current Word**.
- The target Word window must match the window active before DeskOrb took focus.
- Word text is never persisted into records, normal chat transcript, normal
  conversation context, or future Codex thread state.
- The feature submits no screenshot or image with the Word turn.
- Provider-side retention remains governed by the selected provider's policy.
- Any Word read timeout, cancellation, or failure leaves no usable attachment.

## Testing

Add focused `unittest` coverage with mocked COM, worker, and UI boundaries:

1. Word text normalization, expected-window validation, empty-document
   rejection, and COM cleanup.
2. Chat sends the attachment exactly once, displays only metadata, clears it
   before dispatch, and sends no screenshots or images.
3. Late, cancelled, timed-out, and failed reads cannot attach document text.
4. Ephemeral Codex, API, and Agent requests leave their normal session or
   conversation context unchanged.
5. Existing maintained tests continue to pass with no live Word, GUI, API key,
   or network requirement.

## Acceptance criteria

- `main` retains DeskOrb Agent v0.2.0 behavior and naming.
- A focused Word document can be explicitly attached to one Chat question.
- The reply receives the Word text only once and no later normal Chat turn can
  retrieve it from local conversation state.
- The UI never displays the full Word text and does not send a screenshot with
  the attachment.
- The maintained unit-test suite passes.
