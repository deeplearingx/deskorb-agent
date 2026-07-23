# DeskOrb Agent

Current release: **v0.2.0**

A Windows 10/11 always-on-top, screen-aware desktop agent adapted from
[shengyanlin/claude-overlay](https://github.com/shengyanlin/claude-overlay).

The original Tkinter UI is retained under its MIT license. The backend in
`worker.py` supports an independent API-backed local agent, an
OpenAI-compatible Responses API over HTTPS, and an optional persistent Codex
app-server compatibility backend.

## Run

1. Configure an API key and compatible endpoint in the parent-folder `.env`,
   environment variables, or Connection settings.
2. Run `setup.cmd` once.
3. Run `Start DeskOrb Agent.cmd`.

The overlay starts with `workspace-write` sandboxing. The status-bar
**Read-only** toggle switches subsequent turns to a read-only sandbox. The
adapter never uses `--dangerously-bypass-approvals-and-sandbox`.

Configuration overrides:

- `DESKORB_AGENT_WORKING_DIR`: working folder (defaults to your home folder)
- `DESKORB_AGENT_MODEL`: startup model (defaults to `gpt-5.6-sol`)
- `DESKORB_AGENT_BACKEND`: `agent`, `api`, `codex`, or `auto` (defaults to `agent`)
- `DESKORB_AGENT_API_MODEL`: startup API model
- `OPENAI_BASE_URL`: Responses-compatible API base URL (defaults to OpenAI `/v1`)
- `DESKORB_AGENT_API_PROXY`: optional HTTP/HTTPS proxy URL
- `DESKORB_AGENT_CONTEXT_TOKENS`: API context budget (defaults to `24000`)
- `DESKORB_AGENT_FRAMELESS_WINDOW`: set to `1` to opt into the legacy frameless window
  style. It is off by default for Windows/Tk compatibility.
- `DESKORB_AGENT_CUSTOM_WINDOW_REGION`: set to `1` to apply the legacy rounded Win32
  window region; it defaults to the frameless-window setting.
- `DESKORB_AGENT_RECENT_TURNS`: verbatim API recency window (defaults to `6`)
- `DESKORB_AGENT_SUMMARY_TOKENS`: rolling-summary output cap (defaults to `1200`)
- `OPENAI_API_KEY`: optional alternative to the Windows Credential Manager entry
- `DESKORB_AGENT_SERVICE_TIER`: CLI service tier (defaults to `fast`)
- `DESKORB_AGENT_SHOT_SCOPE`: `screens` or `window`

## Connection modes

Click the bottom status line or open **Gear → Connection settings**:

- **codex** keeps one local app-server process alive. It preserves Codex local
  tools and desktop-control support; the first connection on this network may
  still be slow, while later turns reuse the connection.
- **api** calls `/v1/responses` directly over HTTPS and streams text as it
  arrives. It supports arbitrary model IDs and HTTPS-compatible API endpoints.
- **agent** is the default independent API-backed runtime. It can inspect desktop
  state, list/read/search/write files, run PowerShell 7 commands, and control
  mouse, keyboard, scrolling, and window focus. Read-only inspection is automatic;
  any write, shell, or desktop-input action needs an in-chat one-time confirmation.
- **auto** uses the independent Agent when a key is configured and falls back to
  Codex if the Agent request fails and Codex CLI is available.

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

Use [`.env.example`](.env.example) as a safe template. Keep the real `.env`
outside version control.

`model_name` must be a model ID, not an API URL. Environment variables and the
settings window take precedence over this file.

API mode provides chat and image/screenshot understanding. Use **agent** mode for
local shell/file tools and Windows mouse/keyboard control; Codex mode is optional.

### One-time Word attachment in Chat

Focus an editable Microsoft Word document, open DeskOrb Agent, select **Read current
Word**, enter a question, and send it. DeskOrb reads the document text (including
table text and unsaved edits) only after that explicit action and sends it with that
single reply. If Word opens a dialog or the focused document changes while it is
being read, focus the intended document and try again.

The attachment turn sends no screenshot or pasted image. The Chat transcript shows
only the document name and character count, and DeskOrb clears the extracted text
from local memory after sending, cancellation, timeout, or failure. Normal local
Chat context and later turns cannot retrieve the Word text; any provider-side
retention remains governed by the selected Codex or API provider's policy.

### Startup diagnostics

When Agent mode starts, the chat reports whether an API key is configured, the
configured endpoint, whether PowerShell 7 (`pwsh`) is available, and the local
working folder. This is a local configuration check only; the actual API
connection is verified on the first message. Network failures are reported as
`API network connection failed`, while API responses retain their HTTP status.

## Known difference

API mode uses layered local context: recent turns stay verbatim, while older
turns are compacted into a rolling semantic summary when the token budget is
approached. This works with OpenAI-compatible HTTPS streaming services that do
not support `previous_response_id`; screenshots are sent only with the current
message. **Compact** can force an early summary and **Clear** removes both the
summary and recent turns. The default budget is 24K estimated tokens and can be
configured with `DESKORB_AGENT_CONTEXT_TOKENS`; the raw recency window defaults
to six turns and can be configured with `DESKORB_AGENT_RECENT_TURNS`.

On this machine, the CLI's WebSocket transport is blocked and it automatically
falls back to HTTPS. The overlay now keeps one local Codex app-server connection
alive: the initial connection can still take about two minutes, but subsequent
messages reuse it and are normally much faster. Do not send the same question
again while the initial connection is being established.

### Desktop control

In agent mode, shell and desktop-input actions are locally executed only after a
one-time task authorization. After approval, normal application launch, navigation,
typing, shortcuts, scrolling, and window-focus steps continue within the same task
without repeated prompts. The authorization expires when the task finishes, is
stopped, fails, or after ten minutes. Shell commands and high-impact steps—sending
or publishing content, purchases, secrets or personal data, deletion, permission or
security changes, and irreversible confirmation dialogs—still require a fresh
confirmation at the point of risk.

## Attribution

Copyright (c) 2025 Shengyan Lin. Original project and UI licensed under the MIT
license in `LICENSE`. Codex-specific adapter changes are provided under the
same license.
