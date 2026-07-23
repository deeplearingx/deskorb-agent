# Word Chat Attachment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a one-turn Microsoft Word attachment to DeskOrb Agent Chat while preserving DeskOrb Agent v0.2.0's Agent, API, and Codex backends.

**Architecture:** Keep Word COM code in a lazy-import module, expose it to Tk only through background-thread results, and carry an `ephemeral` flag through the worker. Ephemeral backend paths must not read or mutate normal conversation state; ordinary turns remain unchanged.

**Tech Stack:** Python 3, Tkinter, `unittest`, `unittest.mock`, Windows COM through `pywin32`, Codex app-server protocol, OpenAI-compatible Responses API.

## Global Constraints

- Work from `main`; do not merge or copy the `word_extraction` branch wholesale.
- Preserve DeskOrb Agent filenames, `DESKORB_AGENT_*` configuration names, Agent runtime, permission safeguards, and existing UI behavior.
- Word text is transient: do not write it to records, the ordinary transcript, `ConversationContext`, normal Codex threads, logs, or files.
- A Word attachment turn sends no screenshot, image, or pre-captured image.
- Tests use mocked COM, worker, and UI boundaries; no test needs Word, a GUI, an API key, or network access.

## File map

| File | Responsibility |
| --- | --- |
| `word_sources.py` | Lazy Word COM read, target-window verification, normalization, user-facing errors. |
| `requirements.txt` | Declares `pywin32>=306`. |
| `agent_runtime.py` | Runs an Agent request without using or changing ordinary `ConversationContext`. |
| `worker.py` | Queues `ask_ephemeral` and routes an isolated turn through all three backends. |
| `deskorb_agent.py` | Chat attachment controls, lifecycle, and isolated dispatch. |
| `tests/test_word_sources.py` | COM extraction and error tests. |
| `tests/test_agent_runtime.py`, `tests/test_worker.py` | Agent, API, and Codex context-isolation tests. |
| `tests/test_chat_word_attachment.py` | Attachment privacy, lifecycle, and screenshot-suppression tests. |

### Task 1: Word extraction boundary

**Files:**
- Create: `word_sources.py`
- Create: `tests/test_word_sources.py`
- Modify: `requirements.txt`

**Interfaces:**
- Produces `WordMaterial(document_id, name, text, character_count, has_unsaved_changes, window_title)`.
- Produces `WordMaterialError(ValueError)`.
- Produces `read_active_word_document(expected_hwnd: int) -> WordMaterial`.

- [ ] **Step 1: Write failing COM-boundary tests**

Create a mocked COM test for normalized main-story/table text:

```python
def test_reads_expected_document_and_normalizes_word_text(self):
    from word_sources import read_active_word_document
    document = Mock()
    document.Name, document.Saved = "Project brief.docx", False
    document.Content.Text = "Title\\rFirst cell\\r\\x07Second cell\\r\\x07\\x0cFinal\\r"
    application = Mock()
    application.ActiveWindow.Hwnd, application.ActiveDocument = 101, document
    pythoncom, client = Mock(), Mock()
    client.GetActiveObject.return_value = application
    with patch("word_sources.is_word_window", return_value=True), \
            patch("word_sources.root_window", side_effect=lambda hwnd: hwnd), \
            patch("word_sources._load_com_modules", return_value=(pythoncom, client)):
        material = read_active_word_document(101)
    self.assertEqual(material.text, "Title\\nFirst cell\\nSecond cell\\nFinal")
    self.assertTrue(material.has_unsaved_changes)
    pythoncom.CoUninitialize.assert_called_once_with()
```

Add tests for a changed active window (error contains `does not match`), empty normalized text, and missing pywin32 (actionable error).

- [ ] **Step 2: Run the test and observe RED**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_word_sources`

Expected: `ModuleNotFoundError: No module named 'word_sources'`.

- [ ] **Step 3: Implement the minimal Word boundary**

Add a frozen dataclass and lazy COM loader. Implement:

```python
def read_active_word_document(expected_hwnd: int) -> WordMaterial:
    if not is_word_window(expected_hwnd):
        raise WordMaterialError(
            "Focus a Microsoft Word document, then open DeskOrb Agent and try again."
        )
    pythoncom, client = _load_com_modules()
    pythoncom.CoInitialize()
    try:
        return _read_document_from_application(
            client.GetActiveObject("Word.Application"), expected_hwnd
        )
    except WordMaterialError:
        raise
    except Exception as exc:
        raise WordMaterialError(_word_error_message(exc)) from exc
    finally:
        pythoncom.CoUninitialize()
```

`_read_document_from_application` compares `root_window(application.ActiveWindow.Hwnd)` to `root_window(expected_hwnd)`, reads `ActiveDocument.Content.Text`, normalizes `\\r\\x07`, `\\x07`, `\\r`, `\\v`, and `\\f`, collapses blank lines, and rejects empty content. Add `pywin32>=306` to `requirements.txt`.

- [ ] **Step 4: Run the focused tests and observe GREEN**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_word_sources`

Expected: all tests pass without launching Word.

- [ ] **Step 5: Commit**

```powershell
git add word_sources.py requirements.txt tests/test_word_sources.py
git commit -m "feat: read active Word document"
```

### Task 2: Ephemeral Agent and worker requests

**Files:**
- Modify: `agent_runtime.py:377-568`
- Modify: `worker.py:72-482`
- Modify: `tests/test_agent_runtime.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Produces `AgentRuntime.run_ephemeral_turn(text: str, image_paths: list[str])`.
- Produces `CodexWorker.ask_ephemeral(text: str, image_paths=None)`.
- Extends worker private runners with `ephemeral: bool = False`.

- [ ] **Step 1: Write failing Agent isolation tests**

Seed `runtime.context` with an ordinary turn. Mock the request response, call `runtime.run_ephemeral_turn("private Word text", [])`, then assert normal context is unchanged:

```python
context_before = runtime.context.build_input("next question")
runtime.run_ephemeral_turn("private Word text", [])
self.assertEqual(runtime.context.build_input("next question"), context_before)
self.assertNotIn("private Word text", runtime.context.build_input("next question"))
```

Assert the mocked request receives an `input_text` item containing only `private Word text`. Add a control test that ordinary `run_turn` still appends its input and answer to context.

- [ ] **Step 2: Run Agent tests and observe RED**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_agent_runtime`

Expected: failure because `run_ephemeral_turn` is undefined.

- [ ] **Step 3: Implement Agent isolation**

Expose:

```python
def run_ephemeral_turn(self, text: str, image_paths: list[str]):
    try:
        return self._run_turn(text, image_paths, ephemeral=True)
    except BaseException:
        self._task_authorized_until = 0.0
        return None
```

Extend the existing private runner with `ephemeral=False`. In ephemeral mode: skip compaction, use `text` directly instead of `self.context.build_input(text)`, and skip `self.context.add_turn(...)` and the context percentage update. Preserve tool, approval, interruption, and error behavior.

- [ ] **Step 4: Write failing worker routing tests**

Assert:

```python
worker.ask_ephemeral("private", [])
self.assertEqual(worker.req.get_nowait(), ("ask_ephemeral", ("private", [])))
```

Mock each backend runner and assert `_run_turn("private", [], ephemeral=True)` forwards the flag for API, Codex, and Agent. Seed `_api_context` and assert it is unchanged after an API ephemeral turn. Mock Codex `thread/start` and assert the temporary thread id never replaces `_session_id`.

- [ ] **Step 5: Run worker tests and observe RED**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_worker`

Expected: missing `ask_ephemeral` or missing `ephemeral` keyword failures.

- [ ] **Step 6: Implement worker routing**

Add:

```python
def ask_ephemeral(self, text: str, image_paths=None):
    self.req.put(("ask_ephemeral", (text, list(image_paths or []))))
```

Handle `ask_ephemeral` in `run()` with `_run_turn(*payload, ephemeral=True)`. For API, skip context compaction, use `text` directly, and do not append a turn. For Codex, start a fresh app-server thread, use its id for the isolated turn and interruption, and never assign it to `_session_id`. For Agent, call `self._agent.run_ephemeral_turn(text, image_paths)`.

- [ ] **Step 7: Run focused isolation tests and observe GREEN**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_agent_runtime tests.test_worker`

Expected: focused tests pass and ordinary backend tests remain green.

- [ ] **Step 8: Commit**

```powershell
git add agent_runtime.py worker.py tests/test_agent_runtime.py tests/test_worker.py
git commit -m "feat: isolate ephemeral chat turns"
```

### Task 3: Chat attachment UI and lifecycle

**Files:**
- Modify: `deskorb_agent.py:initialization, _build_input, _send_or_stop, dispatch, reset, _handle`
- Create: `tests/test_chat_word_attachment.py`

**Interfaces:**
- Consumes Task 1 `WordMaterial`, `is_word_window`, and `read_active_word_document`.
- Consumes Task 2 `CodexWorker.ask_ephemeral`.
- Produces `_add_chat_word`, `_finish_chat_word_read`, `_clear_chat_word_attachment`, and `_send_chat_word_attachment`.

- [ ] **Step 1: Write failing UI lifecycle tests**

Use an `Overlay.__new__(Overlay)` fixture and mocks. The principal privacy test is:

```python
overlay.chat_word_attachment = WordMaterial(
    "word-window:101", "Brief.docx", "Secret full document.", 21, True, "Brief.docx"
)
overlay._send_chat_word_attachment("Summarize risks.")
prompt, shots, images = overlay._dispatch_turn.call_args.args
self.assertIn("Secret full document.", prompt)
self.assertEqual(shots, [])
self.assertEqual(images, [])
self.assertTrue(overlay._dispatch_turn.call_args.kwargs["ephemeral"])
self.assertIsNone(overlay.chat_word_attachment)
self.assertNotIn("Secret full document.", overlay.add_user.call_args.args[0])
```

Add tests proving attached sends bypass `capture()`, stale results are ignored, a failed replacement clears an old attachment, Clear cancels a pending request id, reset calls `_clear_chat_word_attachment(cancel_read=True)`, and a non-Word target starts no read thread.

- [ ] **Step 2: Run UI tests and observe RED**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_chat_word_attachment`

Expected: missing `word_sources` imports or attachment methods.

- [ ] **Step 3: Add chat-only controls and state**

Initialize `chat_word_attachment`, `_chat_word_read_request`, `_chat_word_read_sequence`, and `_chat_word_read_timeout_after`. Add a bottom Chat row:

```python
self.chat_word_button = tk.Button(wrap, text="Read current Word", command=self._add_chat_word)
self.chat_word_label = tk.Label(wrap, text="")
self.chat_word_clear_button = tk.Button(wrap, text="Clear", command=self._clear_chat_word_attachment)
```

Refresh on busy-state changes. The label may show only `Attached: <name> · <count> chars · <saved/unsaved changes>`; it must never show document text.

- [ ] **Step 4: Implement asynchronous reading**

`_add_chat_word` gets `hwnd = self._capture_target_hwnd()`, verifies `is_word_window(hwnd)`, increments a request id, schedules a 15-second Tk timeout, and starts a daemon thread. The thread posts `("chat_word_read", (request_id, material, error))` to `ui_q`; add a matching `_handle` case. `_finish_chat_word_read` accepts only the current id and cancels the timeout. `_clear_chat_word_attachment(cancel_read=True)` invalidates the id and cancels its timeout.

- [ ] **Step 5: Dispatch the one-time prompt**

Extend the existing UI dispatch with `ephemeral=False`: call `worker.ask_ephemeral` only when true, otherwise retain `worker.ask`. Before normal screenshot logic in `_send_or_stop`, detect an attachment, require a non-empty question, clear pre-capture state, and call `_send_chat_word_attachment`. Build this exact prompt, add metadata only to the transcript, clear the attachment, and dispatch with empty shots/images and `ephemeral=True`:

```text
[TEMPORARY WORD DOCUMENT — use only for this one response]
Document name: <name>
[BEGIN WORD DOCUMENT]
<text>
[END WORD DOCUMENT]

User question:
<question>
```

- [ ] **Step 6: Run UI tests and observe GREEN**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_chat_word_attachment`

Expected: all lifecycle and privacy tests pass without Tk mainloop or Word.

- [ ] **Step 7: Commit**

```powershell
git add deskorb_agent.py tests/test_chat_word_attachment.py
git commit -m "feat: attach active Word document to chat"
```

### Task 4: Documentation and final regression

**Files:**
- Modify: `README.md`
- Verify: `tests/test_*.py`

- [ ] **Step 1: Add a focused metadata-only transcript assertion**

Extend the Task 3 privacy test to assert `add_user` receives document name and character count but never `material.text`; it must also assert the private prompt contains the temporary-document marker and the user question.

- [ ] **Step 2: Run the test and confirm it protects the privacy contract**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_chat_word_attachment.ChatWordAttachmentTests.test_word_attachment_sends_once_without_images_or_transcript_text`

Expected: PASS only when the transcript is metadata-only and the backend prompt contains the document text.

- [ ] **Step 3: Document the user workflow**

Add a concise README section: focus Word before opening DeskOrb Agent, choose **Read current Word**, ask a question, and send. State that document text is used for one reply only, no screenshot is sent for this turn, normal local chat context does not retain it, and provider retention is governed by the chosen provider.

- [ ] **Step 4: Run the maintained suite**

Run: `.venv\\Scripts\\python.exe -m unittest discover -s tests -p "test_*.py"`

Expected: all tests pass without Word, GUI, API key, or network access.

- [ ] **Step 5: Review and commit**

Run:

```powershell
git diff origin/main...HEAD --check
git status --short
git log --oneline origin/main..HEAD
```

Expected: no whitespace errors and a clean worktree before committing README changes.

```powershell
git add README.md tests/test_chat_word_attachment.py
git commit -m "docs: explain Word chat attachments"
```
