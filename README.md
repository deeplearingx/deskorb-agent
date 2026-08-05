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
- `DESKORB_AGENT_SHOW_IN_SCREEN_SHARE`: whether the overlay appears in screen shares
  (defaults to `1`). Set to `0` to request private mode only after verifying the
  window remains visible on the current desktop environment.
- `DESKORB_AGENT_FRAMELESS_WINDOW`: `1` by default, so the custom titlebar is the only
  close/minimize control; set to `0` only when troubleshooting native Windows decorations.
- `DESKORB_AGENT_CUSTOM_WINDOW_REGION`: applies the rounded Win32 window region; it defaults
  to the frameless-window setting.
- `DESKORB_AGENT_RECENT_TURNS`: verbatim API recency window (defaults to `6`)
- `DESKORB_AGENT_SUMMARY_TOKENS`: rolling-summary output cap (defaults to `1200`)
- `DESKORB_AGENT_MODEL_FALLBACKS`: optional JSON catalog of explicitly authorized fallback models; it never switches automatically
- `DESKORB_AGENT_MODEL_CAPABILITIES`: optional JSON declarations for per-model tool, vision, streaming, context and privacy capabilities
- `OPENAI_API_KEY`: optional alternative to the Windows Credential Manager entry
- `DESKORB_AGENT_SERVICE_TIER`: CLI service tier (defaults to `fast`)
- `DESKORB_AGENT_SHOT_SCOPE`: `screens` or `window`
- `DESKORB_AGENT_THEME`: `tech` (默认科技风)、`dark` 或 `light`

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
settings window take precedence over this file. The overlay watches the file for
changes while it is running; after an authentication 401 it refreshes the key
once and retries the request, so rotating a key does not require a full restart.

API mode provides chat and image/screenshot understanding. Use **agent** mode for
local shell/file tools and Windows mouse/keyboard control; Codex mode is optional.

### Model providers

DeskOrb has a model adapter layer. It uses OpenAI's **Responses API** for OpenAI and
Responses-compatible gateways, and automatically switches to the OpenAI-compatible
**Chat Completions API** for DeepSeek and Qwen/DashScope. Agent tool calls, tool results,
images, and multi-step tasks remain in one internal format, so the desktop tools do not
need vendor-specific implementations.

Choose a provider in **Connection settings**, or set `DESKORB_AGENT_PROVIDER` to one of
`auto`, `openai`, `responses`, `openai-compatible`, `qwen`, or `deepseek`. In `auto`
mode, official DeepSeek and DashScope URLs are detected; unknown custom URLs retain the
existing Responses-compatible behavior. Use `openai-compatible` for compatible
Chat-Completions endpoints such as a trusted local gateway.

Fallback models are opt-in. Configure a small JSON catalog without API keys, for example:

```text
DESKORB_AGENT_MODEL_FALLBACKS=[{"id":"deepseek","provider":"deepseek","model":"deepseek-chat","base_url":"https://api.deepseek.com","privacy_level":"third_party"}]
```

When the active provider fails, the runtime only reports available targets. A user must
confirm the target, and must separately consent before existing conversation context is
shared with another provider. Targets without declared tool support are rejected.

If a gateway does not support image input or tool calls, declare that explicitly:

```text
DESKORB_AGENT_MODEL_CAPABILITIES=[{"provider":"deepseek","model":"deepseek-chat","base_url":"https://api.deepseek.com","supports_tools":true,"supports_vision":false,"privacy_level":"third_party"}]
```

### Local MCP: browser and PowerToys

Agent mode includes two local MCP servers on demand: Microsoft's Playwright MCP for
browser/web/search tasks, and DeskOrb's `powertoys_mcp.py` for supported PowerToys
configuration tasks. The task router starts only the relevant server: a web task does
not boot PowerToys, a PowerToys task does not boot Playwright, and an ordinary desktop
task starts neither. Custom servers can declare lightweight intent metadata; DeskOrb
then routes matching requests automatically. When a request is ambiguous, the model sees
only the small configured capability catalog, selects one integration, and then loads
only that server's real tools. Browser snapshots are read-only,
while navigation, clicking, typing and scrolling use the same task-level confirmation
policy as desktop input. Submitting, purchasing, sending, uploading private data,
deleting, or changing permissions still requires a fresh confirmation.

The PowerToys MCP uses the locally installed `PowerToys.DSC.exe`, never a shell.
It can inspect installed modules, settings, schemas, and backups; it can dry-run setting
changes. It can also apply a small multi-module productivity profile (up to eight changes)
after preflighting every entry; if a later entry fails, already-applied entries are rolled
back automatically. Writes are deliberately limited to reversible productivity modules:
**Advanced Paste, Always on Top, Awake, Color Picker, Crop and Lock, FancyZones, Image
Resizer, mouse utilities, Peek, PowerRename, Shortcut Guide, Workspaces, and ZoomIt**.
Every write is a fresh high-risk confirmation. The service preflights the proposed partial
change, takes an in-memory backup, applies it, and rereads it to verify. A returned backup
ID can be used by the separately confirmed restore action while the MCP session remains
open. Keyboard Manager, Hosts, Environment Variables, Registry Preview, and global App
settings remain read-only until explicitly reviewed and added to the allowlist.

To use another trusted local MCP server, copy
[`mcp.servers.example.json`](mcp.servers.example.json), edit it, and set:

```text
DESKORB_AGENT_MCP_CONFIG=C:\path\to\mcp.servers.json
```

The file uses the standard `mcpServers` JSON shape (`command`, `args`, optional
`env`, `cwd`, and `enabled`). A custom file replaces the defaults, so keep the
`powertoys` entry if you want that integration. Set `DESKORB_AGENT_PLAYWRIGHT_MCP=0`
to disable the default Playwright server. Chrome DevTools MCP can be configured this
way when you explicitly want to attach to a Chrome instance started with remote
debugging; Playwright MCP is the default because it runs in a separate local browser
profile.

Custom MCP entries are fail-closed. Their `deskorb.allowed_tools` list must name every
tool that DeskOrb may expose, and optional `deskorb.allowed_domains` limits URL arguments
to approved hosts. Raw page-code evaluation, host command execution, filesystem writes,
and downloads are blocked even if a server advertises them. This prevents a server update
or malicious web page from silently expanding the assistant's authority.

Browser work uses `isolated-playwright` by default. This opens a separate browser profile
with no personal login state. To let a task operate in a tab you explicitly select from
Chrome or Edge, install the official Playwright MCP extension and set
`DESKORB_AGENT_BROWSER_BACKEND=connected-playwright` before launching DeskOrb. The overlay
announces the connected mode for each task. It never stores cookies or passwords, and it
closes the browser task space when the task ends or is cancelled.

Complex browser steps may be sent as a bounded semantic batch containing navigation,
snapshot, reference click, fill, wait, or verification actions. Raw JavaScript, CDP,
uploads, downloads, submissions, and arbitrary network requests are not batch actions.
Recipe reuse requires the same origin and a final structured verification; a model's
natural-language answer is not enough to mark the task complete. Successful browser
observations update a short hash checkpoint, so a resumed task re-observes the page before
acting again.

For automatic routing of a custom integration, add the optional DeskOrb-only metadata
(other MCP clients safely ignore it):

```json
"deskorb": {
  "description": "Search the company knowledge base",
  "keywords": ["knowledge base", "wiki", "内部文档", "知识库"],
  "allowed_tools": ["knowledge_search"],
  "allowed_domains": ["wiki.example.com"]
}
```

### Practical Windows desktop control

In Agent mode, DeskOrb can launch supported applications and use verified mouse,
keyboard, hotkey, and scrolling input. For reliable window operations it now lists
visible top-level windows first and returns a short-lived `window_id`; the agent then
uses that exact ID to focus, minimize, maximize, restore, snap left/right, move/resize,
or toggle topmost. This avoids guessing window titles or stale screen coordinates.

When `pywinauto` is installed, DeskOrb first inspects the foreground application's
Windows UI Automation tree. It can invoke a named control or set an editable control's
value using a short-lived `control_id`. This is the preferred route for buttons, menus,
and text fields. The existing screenshot-guarded coordinate controls remain the fallback
for applications that do not expose usable accessibility information.

One task confirmation creates a ten-minute local authorization lease. It covers only the
approved desktop controls and MCP servers, not every tool in the process. Closing a window
always needs a fresh confirmation. Reading clipboard text also needs a fresh confirmation
because clipboard contents can contain passwords, private text, or tokens. After
coordinate/keyboard/window actions, DeskOrb captures a fresh desktop observation before
continuing. A privacy-aware local task journal records task state, tool outcome and failure
category, but redacts free-form typed text, clipboard data, credentials, and tokens.

If a request explicitly asks to inspect the current desktop, active window, visible state, or
local DeskOrb logs, the overlay shows a clickable one-turn privacy card before capturing or
sending anything. Until **允许本次读取** is clicked, it does not reuse a pre-captured frame,
read the current state, or contact the configured model for that diagnostic. An approved
diagnostic captures the active window only and consumes the grant after one turn. Ordinary
requests keep the existing Auto-shot behavior selected by the user; the diagnostic gate does
not change that standing toggle. The same one-turn grant is checked again in the Worker and
AgentRuntime, so a caller cannot bypass the overlay by submitting a diagnostic directly.

The runtime exposes a minimal model health check that sends only `health check` and expects
`OK`. It records provider, protocol, success rate, and latency percentiles locally. Health
metadata never includes screenshots, conversation history, page text, or API keys.

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
stopped, fails, or after ten minutes. Shell commands and high-impact steps, including sending
or publishing content, purchases, secrets or personal data, deletion, permission or
security changes, and irreversible confirmation dialogs, still require a fresh
confirmation at the point of risk.

## Attribution

Copyright (c) 2025 Shengyan Lin. Original project and UI licensed under the MIT
license in `LICENSE`. Codex-specific adapter changes are provided under the
same license.
