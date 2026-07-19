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
