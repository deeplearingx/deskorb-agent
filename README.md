# Codex Overlay

A Windows 10/11 always-on-top, screen-aware Codex chat adapted from
[shengyanlin/claude-overlay](https://github.com/shengyanlin/claude-overlay).

The original Tkinter UI is retained under its MIT license. The backend in
`worker.py` supports both a persistent Codex app-server connection and the
OpenAI-compatible Responses API over HTTPS.

## Run

1. Install and sign in to the Codex CLI.
2. Run `setup.cmd` once.
3. Run `Start Codex Overlay.cmd`.

The overlay starts with `workspace-write` sandboxing. The status-bar
**Read-only** toggle switches subsequent turns to a read-only sandbox. The
adapter never uses `--dangerously-bypass-approvals-and-sandbox`.

Configuration overrides:

- `CODEX_OVERLAY_WORKING_DIR`: working folder (defaults to your home folder)
- `CODEX_OVERLAY_MODEL`: startup model (defaults to `gpt-5.6-sol`)
- `CODEX_OVERLAY_BACKEND`: `codex`, `api`, or `auto` (defaults to `codex`)
- `CODEX_OVERLAY_API_MODEL`: startup API model
- `OPENAI_BASE_URL`: Responses-compatible API base URL (defaults to OpenAI `/v1`)
- `CODEX_OVERLAY_API_PROXY`: optional HTTP/HTTPS proxy URL
- `CODEX_OVERLAY_CONTEXT_TOKENS`: API context budget (defaults to `24000`)
- `CODEX_OVERLAY_RECENT_TURNS`: verbatim API recency window (defaults to `6`)
- `CODEX_OVERLAY_SUMMARY_TOKENS`: rolling-summary output cap (defaults to `1200`)
- `OPENAI_API_KEY`: optional alternative to the Windows Credential Manager entry
- `CODEX_OVERLAY_SERVICE_TIER`: CLI service tier (defaults to `fast`)
- `CODEX_OVERLAY_SHOT_SCOPE`: `screens` or `window`

## Connection modes

Click the bottom status line or open **Gear → Connection settings**:

- **codex** keeps one local app-server process alive. It preserves Codex local
  tools and desktop-control support; the first connection on this network may
  still be slow, while later turns reuse the connection.
- **api** calls `/v1/responses` directly over HTTPS and streams text as it
  arrives. It supports arbitrary model IDs and HTTPS-compatible API endpoints.
- **auto** uses API when a key is configured and falls back to Codex if the API
  request fails; without a key it uses Codex directly.

An optional HTTP proxy can be configured for networks where `api.openai.com`
cannot be reached directly (for example `http://127.0.0.1:7890`). The API key entered in the settings window is stored as a generic credential in
Windows Credential Manager, never in `state.json` or the project files. Non-secret
settings (backend, model ID, base URL, and proxy URL) are remembered in local app state.

For compatibility with the existing workspace setup, the overlay also reads a
parent-folder `.env` in this form:

```text
api-key: your-key
url: https://provider.example/v1
model_name: gpt-5.6-terra
```

`model_name` must be a model ID, not an API URL. Environment variables and the
settings window take precedence over this file.

API mode currently provides chat and image/screenshot understanding. Use Codex
mode for local shell/file tools and Windows mouse/keyboard control.

## Quick office actions

After using `Ctrl+Alt+Space` to open the overlay, choose one of the action buttons
above the message box: **总结**, **提取待办**, **草拟回复**, or **解释内容**. Each action
captures only the active application window, then shows a streamed result in the
overlay. Results have the normal copy button;
the overlay never overwrites the clipboard, pastes into another app, or sends a
message for you. If no usable external window is available, focus the target app
and try again.

### One-time Word attachment in Chat

Chat also has **Read current Word**. Focus a desktop Word document, open the
Overlay, click the button, enter a question, and send it. The main document text
and table text (including unsaved edits) are used only for that one reply. The
overlay sends no automatic screenshot or pasted image with this Word turn, then
clears the extracted text from memory after send, Stop, failure, or timeout.
Quick actions remain screenshot-only and do not use the Word attachment.

For this strict privacy mode, the Word reply runs in an isolated model turn: it
does not inherit ordinary Chat history and later Chat messages cannot retrieve
the Word text or automatically remember that reply. The overlay does not add the
text to local API memory or its normal Codex chat thread; any service-side request
retention remains governed by the selected Codex/API provider's own policy.

## Communication and meeting workflow center

The top navigation now separates **Chat**, **Workflows**, and **Records**. The
initial workflow templates are **Meeting minutes**, **Action tracker**, **Draft
reply**, and **Post-meeting follow-up**. Each workflow can combine materials
that you explicitly add:

- **Add active window** captures only the current external window. It never
  falls back to a full-screen capture.
- **Read current Word** reads the full main text and table text from the Word
  document that was active before you opened the Overlay. It includes unsaved
  edits, but only when you click the button; it does not use screenshots,
  clipboard contents, or a saved DOCX copy. Word must be the foreground app,
  and protected-view documents may need **Enable Editing** first.
- Paste notes into the workflow form, or add up to five UTF-8 TXT/Markdown,
  DOCX, or text-based PDF files (10 MiB per file, 100,000 extracted characters
  in total). Word text shares this limit with file text. To raise the limit for
  a launch, set `CODEX_OVERLAY_WORKFLOW_TEXT_LIMIT` (for example, PowerShell:
  `$env:CODEX_OVERLAY_WORKFLOW_TEXT_LIMIT="300000"`). API mode may also need a
  larger `CODEX_OVERLAY_CONTEXT_TOKENS` budget. Scanned PDFs and XLSX files are
  not supported in this release.

Results stream into Chat and are automatically saved in the local **Records**
library. Records keep generated Markdown, structured fields, timestamps, and
file/window names only; screenshots, pasted text, and source document contents
are not retained. Edit saved text and action items in Records, then manually
copy or export a Markdown file. The overlay does not auto-paste, auto-send,
sync external services, or show background reminders.

Word source records keep only the Word document name and character count. They
never keep the extracted text, file path, Word window handle, or whether the
document had unsaved changes.

## Known difference

API mode uses layered local context: recent turns stay verbatim, while older
turns are compacted into a rolling semantic summary when the token budget is
approached. This works with OpenAI-compatible HTTPS streaming services that do
not support `previous_response_id`; screenshots are sent only with the current
message. **Compact** can force an early summary and **Clear** removes both the
summary and recent turns. The default budget is 24K estimated tokens and can be
configured with `CODEX_OVERLAY_CONTEXT_TOKENS`; the raw recency window defaults
to six turns and can be configured with `CODEX_OVERLAY_RECENT_TURNS`.

On this machine, the CLI's WebSocket transport is blocked and it automatically
falls back to HTTPS. The overlay now keeps one local Codex app-server connection
alive: the initial connection can still take about two minutes, but subsequent
messages reuse it and are normally much faster. Do not send the same question
again while the initial connection is being established.

### Desktop control

This machine's Codex CLI has the stable `computer_use` capability enabled. The
overlay explicitly permits mouse and keyboard control only after the user asks
for a desktop action. Set `CODEX_OVERLAY_COMPUTER_USE=0` before launch to turn
it off. High-impact actions (sending messages, purchases, secrets, irreversible
confirmation dialogs) still require a fresh confirmation.

## Attribution

Copyright (c) 2025 Shengyan Lin. Original project and UI licensed under the MIT
license in `LICENSE`. Codex-specific adapter changes are provided under the
same license.
