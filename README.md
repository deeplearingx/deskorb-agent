# DeskOrb Agent

Current release: **v0.2.0**

A Windows 10/11 always-on-top, screen-aware desktop agent adapted from
[shengyanlin/claude-overlay](https://github.com/shengyanlin/claude-overlay).

The original Tkinter UI is retained under its MIT license. The backend in
`worker.py` supports an independent API-backed local agent, an
OpenAI-compatible Responses API over HTTPS, and an optional persistent Codex
app-server compatibility backend.

## Run

1. Configure an API key and compatible endpoint in `volcengine.env`, the
   parent-folder `.env`, environment variables, or Connection settings.
2. Run `setup-conda.cmd` once. This creates or updates the dedicated Conda
   environment named `deskorb-agent` and installs the local Playwright MCP.
3. Run `Start DeskOrb Agent.cmd` (it delegates to the Conda launcher).

DeskOrb development and tests use the same isolated environment. From a
PowerShell prompt, run commands as `conda run -n deskorb-agent python ...` or
activate it with `conda activate deskorb-agent`. Do not use an unrelated
environment such as `marketmind` for this project. The legacy `setup.cmd`
portable-`.venv` flow remains available for compatibility, but it is not the
default project runtime.

The overlay starts with `workspace-write` sandboxing. The status-bar
**Read-only** toggle switches subsequent turns to a read-only sandbox. The
adapter never uses `--dangerously-bypass-approvals-and-sandbox`.

Configuration overrides:

- `DESKORB_AGENT_WORKING_DIR`: working folder (defaults to your home folder)
- `DESKORB_AGENT_MODEL`: startup model (defaults to `gpt-5.6-sol`)
- `DESKORB_AGENT_BACKEND`: `agent`, `api`, `codex`, or `auto` (defaults to `agent`)
- `DESKORB_AGENT_PROVIDER`: `auto`, `openai`, `responses`, `openai-compatible`,
  `deepseek`, or `qwen` (defaults to `auto`; `auto` detects the configured base URL)
- `DESKORB_AGENT_API_MODEL`: startup API model
- `DESKORB_AGENT_MAX_TOOL_ROUNDS`: maximum Agent/MCP tool rounds per task
  (defaults to `100`, allowed range `20`–`500`)
- `OPENAI_BASE_URL`: API base URL (Responses for OpenAI, Chat Completions for compatible providers)
- `DESKORB_AGENT_API_PROXY`: optional HTTP/HTTPS proxy URL
- `DESKORB_AGENT_CONTEXT_TOKENS`: API context budget (defaults to `24000`)
- `DESKORB_AGENT_OFFICECLI_TIMEOUT`: timeout for one OfficeCLI operation in
  seconds (defaults to `180`)
- `DESKORB_AGENT_OFFICECLI_MAX_TOOL_ROUNDS`: maximum Agent/MCP rounds for an
  OfficeCLI task (defaults to `300`)
- `DESKORB_AGENT_SHOW_IN_SCREEN_SHARE`: whether the overlay appears in screen shares
  (defaults to `1`). Set to `0` to request private mode only after verifying the
  window remains visible on the current desktop environment.
- `DESKORB_AGENT_FRAMELESS_WINDOW`: set to `1` to opt into the legacy frameless window
  style. It is off by default for Windows/Tk compatibility.
- `DESKORB_AGENT_CUSTOM_WINDOW_REGION`: set to `1` to apply the legacy rounded Win32
  window region; it defaults to the frameless-window setting.
- `DESKORB_AGENT_RECENT_TURNS`: verbatim API recency window (defaults to `6`)
- `DESKORB_AGENT_SUMMARY_TOKENS`: rolling-summary output cap (defaults to `1200`)
- `DESKORB_AGENT_API_IMAGE_INPUT`: set to `1` only when the selected API model accepts
  Responses API image input; defaults to `0` for text-only endpoints such as the tested
  `glm-5.2` configuration
- `DESKORB_AGENT_OFFICE_MAX_NONEMPTY_CELLS`: Excel attachment cell cap (defaults to `10000`)
- `DESKORB_AGENT_OFFICE_MAX_RENDERED_CHARS`: Office attachment character cap (defaults to `120000`)
- `DESKORB_AGENT_OFFICECLI_AUTO_APPROVE`: retained for compatibility. In Full access,
  all non-delete operations are automatic; file deletion confirmation cannot be disabled.
- `OPENAI_API_KEY`: optional alternative to the Windows Credential Manager entry
- `DEEPSEEK_API_KEY`, `QWEN_API_KEY`, `DASHSCOPE_API_KEY`: provider-specific keys when
  `DESKORB_AGENT_PROVIDER` selects a compatible provider; they are not sent to another provider
- `DESKORB_AGENT_SERVICE_TIER`: CLI service tier (defaults to `fast`)
- `DESKORB_AGENT_SHOT_SCOPE`: `screens` or `window`
- `DESKORB_AGENT_MEETING_DIR`: meeting audio/transcript output directory
- `DESKORB_AGENT_MEETING_CHUNK_CHARS`: maximum transcript characters per summary chunk (default `8000`)
- `DESKORB_AGENT_MEETING_MERGE_BATCH`: maximum partial summaries per merge request (default `8`)
- `DESKORB_AGENT_WHISPERX_ROOT`: WhisperX checkout (defaults to the repository's `whisperX-main` folder)
- `DESKORB_AGENT_WHISPERX_PYTHON`: Python executable for WhisperX; otherwise DeskOrb discovers
  `.venv\Scripts\python.exe` below the checkout
- `DESKORB_AGENT_WHISPERX_MODEL`, `DESKORB_AGENT_WHISPERX_DEVICE`, and
  `DESKORB_AGENT_WHISPERX_COMPUTE_TYPE`: WhisperX runtime options (defaults `small`, `cpu`, `int8`)
- `DESKORB_AGENT_WHISPERX_LANGUAGE`: optional language code; `DESKORB_AGENT_WHISPERX_DIARIZE=1`
  enables diarization; `DESKORB_AGENT_WHISPERX_ALIGN=1` enables word alignment
- The transcript and structured minutes pass through OpenCC `t2s` post-processing, so Traditional Chinese is saved as Simplified Chinese.

## Meeting recording with WhisperX

The status bar's `Record` button starts a local meeting recording. Click `Stop`
to finish; audio capture and WhisperX transcription run in background threads, so chat,
Office integration, and other controls remain usable while recording or transcribing.

The WhisperX source is included in this repository at `whisperX-main\`. Its heavy ML
runtime stays isolated in the ignored `whisperX-main\.venv\`, while DeskOrb itself keeps
using its normal lightweight environment.

1. From the DeskOrb repository root, create and install the bundled WhisperX environment
   (Python 3.10--3.13 is supported by this checkout):
   ```powershell
   .\setup-whisperx.cmd
   ```
   The script creates `whisperX-main\.venv` and installs the local `whisperX-main` project.
2. Start DeskOrb and click `Record` / `Stop`. By default files are written to
   `%USERPROFILE%\Documents\DeskOrb Meetings`; set `DESKORB_AGENT_MEETING_DIR` to
   choose another directory.
3. The default `DESKORB_AGENT_WHISPERX_ROOT` points to the repository's
   `whisperX-main` folder. Set it only when intentionally using another checkout; set
   `DESKORB_AGENT_WHISPERX_PYTHON` only when its Python executable is outside that folder.

After `Stop`, one session produces the audio plus WhisperX transcript under the meeting directory:

- `meeting-001.wav` — the recorded microphone/speaker mix
- `whisperx\meeting-001.json` and `.txt` — the timestamped transcript
- `minutes\meeting-001.md` and `.json` — the agent-generated meeting minutes (summary, key points, decisions, action items, and questions)

The minutes request is sent through the same configured DeskOrb agent. If the agent/API is unavailable,
the transcript and audio are still kept; the UI shows the minutes error instead of discarding the recording.

For long meetings, DeskOrb summarizes bounded transcript chunks first and then merges those
structured summaries hierarchically. The full transcript remains on disk, while the UI reports
chunk and merge progress in the background. If a chunk or merge fails, completed partial summaries
are kept in `minutes\meeting-001.partial.json` for diagnosis or retry; no incomplete final minutes are written.

The default transcription mode is CPU + `int8` with the `small` model. Word alignment is disabled by default
so the base transcription does not require a second alignment model; set `DESKORB_AGENT_WHISPERX_ALIGN=1`
when word-level timestamps are required. Transcript text is normalized from Traditional to Simplified Chinese after WhisperX and before TXT/JSON/minutes are saved. Speaker diarization
is opt-in because it may require extra model credentials; enable it with
`DESKORB_AGENT_WHISPERX_DIARIZE=1`.

## Connection modes

Click the bottom status line or open **Gear → Connection settings**:

- **codex** keeps one local app-server process alive. It preserves Codex local
  tools and desktop-control support; the first connection on this network may
  still be slow, while later turns reuse the connection.
- **api** calls `/v1/responses` directly over HTTPS and streams text as it
  arrives. It supports arbitrary model IDs and HTTPS-compatible API endpoints.
- **agent** is the default independent API-backed runtime. It can inspect desktop
  state, list/read/search/write files, run PowerShell 7 commands, and control
  mouse, keyboard, scrolling, and window focus. In Full access, requested actions
  run automatically; only file deletion needs an in-chat confirmation.
- **auto** uses the independent Agent when a key is configured and falls back to
  Codex if the Agent request fails and Codex CLI is available.

An optional HTTP proxy can be configured for networks where `api.openai.com`
cannot be reached directly (for example `http://127.0.0.1:7890`). The API key entered in the settings window is stored as a generic credential in
Windows Credential Manager, never in `state.json` or the project files. Non-secret
settings (backend, model ID, base URL, and proxy URL) are remembered in local app state.

For compatibility with the existing workspace setup, the overlay reads the
first existing file in this order: project `volcengine.env`, project `.env`,
parent-folder `.env`. `DESKORB_AGENT_ENV_FILE` can point to an explicit file.
The file uses this form:

```text
api-key: your-key
url: https://provider.example/v1
model_name: gpt-5.6-terra
```

Provider-specific `.env` keys such as `deepseek_api_key` and `qwen_api_key` are also
accepted. The long-lived process reloads the `.env` file after an in-place edit.

Use [`.env.example`](.env.example) as a safe template. Keep the real `.env`
outside version control.

`model_name` must be a model ID, not an API URL. Environment variables and the
settings window take precedence over this file.

For Volcengine Ark Coding Plan, use a Coding Plan API key together with the
Coding Plan endpoint and a Coding Plan model ID, for example:

```text
provider: openai-compatible
api-key: <Ark Coding Plan API Key>
url: https://ark.cn-beijing.volces.com/api/coding/v3
model_name: ark-code-latest
```

For a regular Ark inference endpoint, use the standard Ark API key and endpoint
instead; do not mix the two configurations:

```text
provider: openai-compatible
api-key: <Ark standard API Key>
url: https://ark.cn-beijing.volces.com/api/v3
model_name: <your-endpoint-id>
```

Copy [`volcengine.env.example`](volcengine.env.example) to `volcengine.env`
and replace the placeholders. The real file is ignored by Git and must never
be committed.

API mode provides direct text chat. Use **agent** mode for local shell/file tools and
Windows mouse/keyboard control; Codex mode is optional. When
`DESKORB_AGENT_PROVIDER=deepseek` or `qwen` is set, the runtime uses that provider's
Chat Completions endpoint while retaining the same internal tool transcript. If API mode receives an
OfficeCLI task, DeskOrb automatically routes that task through the MCP-capable Agent
Runtime. Automatic screenshots are omitted from API/Agent requests by default because
the configured `glm-5.2` endpoint accepts text only; enable them only after verifying
the selected model supports image input.

### Local MCP: browser, PowerToys, and OfficeCLI

Agent mode includes two local MCP servers on demand: Microsoft's Playwright MCP for
browser/web/search tasks, and DeskOrb's `powertoys_mcp.py` for supported PowerToys
configuration tasks. The task router starts only the relevant server: a web task does
not boot PowerToys, a PowerToys task does not boot Playwright, and an ordinary desktop
task starts neither. Custom servers can declare lightweight intent metadata; DeskOrb
then routes matching requests automatically. When a request is ambiguous, the model sees
only the small configured capability catalog, selects one integration, and then loads
only that server's real tools. Browser snapshots are read-only, while navigation,
clicking, typing and scrolling run automatically after one task-level confirmation
in Full access. After confirmation DeskOrb starts the isolated headed browser,
performs a bounded initial snapshot, and reports `starting → visible → ready →
running → verifying` in the overlay. The browser window is foregrounded once;
later actions do not repeatedly steal focus. If Windows rejects foreground
activation, the taskbar is flashed and the overlay offers a safe “switch to
browser” hint. Startup failures return a specific diagnostic code instead of
waiting for the full task timeout.

Login, CAPTCHA, upload, submit, publish, purchase, and other high-risk browser
actions remain separate confirmation or human-handoff boundaries. Completion is
not inferred from model prose: structured extraction and an independent
verification step are required before DeskOrb reports success. The current
policy still asks for confirmation before deleting files as well.

比赛答辩的真实网站脚本、量化指标和消融对比见
[`docs/competition-demo-plan.md`](docs/competition-demo-plan.md)。

当前稳定化进度和下一步切片记录在 [`tasks/plan.md`](tasks/plan.md) 与
[`tasks/todo.md`](tasks/todo.md)。已验证的真实网站语义探针报告保存在
`artifacts/public-books-browser-action.json`；真实模型若出现
`provider_timeout_after_tools`，只计为供应商预算问题，不作为浏览器成功证据。

The PowerToys MCP uses the locally installed `PowerToys.DSC.exe`, never a shell.
It can inspect installed modules, settings, schemas, and backups; it can dry-run setting
changes. It can also apply a small multi-module productivity profile (up to eight changes)
after preflighting every entry; if a later entry fails, already-applied entries are rolled
back automatically. Writes are deliberately limited to reversible productivity modules:
**Advanced Paste, Always on Top, Awake, Color Picker, Crop and Lock, FancyZones, Image
Resizer, mouse utilities, Peek, PowerRename, Shortcut Guide, Workspaces, and ZoomIt**.
In Full access, writes run automatically. The service preflights the proposed partial
change, takes an in-memory backup, applies it, and rereads it to verify. A returned backup
ID can be used by a restore action while the MCP session remains open. Keyboard Manager,
Hosts, Environment Variables, Registry Preview, and global App
settings remain read-only until explicitly reviewed and added to the allowlist.

OfficeCLI is an optional third local MCP server for disk-backed `.docx`, `.xlsx`, and
`.pptx` files. Build its self-contained Windows binary once with:

```powershell
.\build_officecli.ps1
```

The build requires the .NET 10 SDK; the published runtime does not require .NET on the
machine where DeskOrb runs. If the binary is in a non-default location, set
`DESKORB_AGENT_OFFICECLI_BINARY=C:\path\to\officecli.exe`. Set
`DESKORB_AGENT_OFFICECLI=0` to disable automatic discovery. Ordinary desktop tasks do
not start OfficeCLI; a matching Office file request loads it lazily. OfficeCLI generation
and update commands run automatically in Full access. Only an explicit OfficeCLI file-delete
command, if supported by the installed binary, requires confirmation. The
`DESKORB_AGENT_OFFICECLI_AUTO_APPROVE` setting is retained for compatibility and cannot
disable file-deletion confirmation.

OfficeCLI operates on files saved to disk. The existing Word/Excel attachment and COM
workflow remains the right path for active documents with unsaved edits. Do not ask
OfficeCLI to edit a file currently held by Word or Excel; save and close that document
first. If OfficeCLI reports a lock or sharing violation, follow that save/close step and
retry.

For layout-sensitive output, the initial delivery gate is `validate`, `view issues`
where applicable, and a PowerPoint screenshot via `view <file> screenshot --page N`.
The MCP screenshot result is returned as an image block.
Release packages should ship the binary alongside the retained
`OfficeCLI-main/LICENSE`, `NOTICE`, and `THIRD-PARTY-NOTICES.txt` files.

To use another trusted local MCP server, copy
[`mcp.servers.example.json`](mcp.servers.example.json), edit it, and set:

```text
DESKORB_AGENT_MCP_CONFIG=C:\path\to\mcp.servers.json
```

The file uses the standard `mcpServers` JSON shape (`command`, `args`, optional
`env`, `cwd`, and `enabled`). A custom file replaces the defaults, so keep the
`powertoys` and `officecli` entries if you want those integrations. Set
`DESKORB_AGENT_PLAYWRIGHT_MCP=0`
to disable the default Playwright server. Chrome DevTools MCP can be configured this
way when you explicitly want to attach to a Chrome instance started with remote
debugging; Playwright MCP is the default because it runs in a separate local browser
profile. `DESKORB_AGENT_BROWSER_START_TIMEOUT` controls the headed-browser startup
budget (default 12 seconds, bounded to 3–60 seconds); it is independent from the
longer provider and tool execution budgets.

For automatic routing of a custom integration, add the optional DeskOrb-only metadata
(other MCP clients safely ignore it):

```json
"deskorb": {
  "description": "Search the company knowledge base",
  "keywords": ["knowledge base", "wiki", "内部文档", "知识库"]
}
```

### Practical Windows desktop control

In Agent mode, DeskOrb can launch supported applications and use verified mouse,
keyboard, hotkey, and scrolling input. For reliable window operations it now lists
visible top-level windows first and returns a short-lived `window_id`; the agent then
uses that exact ID to focus, minimize, maximize, restore, snap left/right, move/resize,
or toggle topmost. This avoids guessing window titles or stale screen coordinates.

In Full access, ordinary navigation, window layout, clipboard reads, and desktop input
run automatically. Only file deletion needs a fresh confirmation.
After coordinate/keyboard/window actions, DeskOrb captures a fresh desktop observation
before continuing.

### Persistent Word/Excel attachment in Chat

Focus an editable Microsoft Word document, open DeskOrb Agent, select **Read current
Word** or **Read current Excel**, enter a question, and send it. DeskOrb reads the
document/workbook (including Word table text, Excel values/formulas, and unsaved
edits) only after that explicit action. The attachment remains available for later
questions until you click **Clear** or read another Office source.

Office-aware turns send no screenshot or pasted image. The Chat transcript shows
only the document name and character count. Each request receives a fresh private
snapshot plus the in-memory history of successful Apply operations; this supports
follow-ups such as “撤回刚才的修改”. A generated inverse plan still requires
**Apply changes** confirmation. Clear removes the attachment, pending plan, and
history from local memory. Normal Chat context and visible messages never receive
the Office text or history; any provider-side retention remains governed by the
selected Codex or API provider's policy.

### Word and Excel edit previews

Focus an editable Word document or Excel workbook before opening DeskOrb, then choose
**Read current Word** or **Read current Excel**. Excel reads every worksheet's used
range, subject to the configured limits. Ask for an analysis or a text/cell/formula
change. DeskOrb sends the private Office context in a no-tools planning request and
shows the model answer plus a local **Apply changes** / **Discard** choice when a
validated edit plan is available.

Apply is never automatic: it rereads the current Office source and rejects it if the
window, document identity, structure, value, or formula changed after the preview.
The first release edits only Word body/table text and Excel cell values/formulas.
Word inserted or replaced text inherits nearby formatting unless the request
specifies formatting; existing Excel cell formatting is retained. After a verified
Apply, DeskOrb attempts to select the changed Word text or Excel cell. If Office
cannot select it, the DeskOrb preview remains the change marker. Comments, charts,
macros, and workbook structure are not changed. DeskOrb never saves, closes, or
creates Office files. Save or undo from Office yourself.

When a request adds a new paragraph at the end of a Word document, the plan uses
the last non-empty body paragraph as a bounded anchor; trailing empty paragraphs
and table content do not change that body anchor. Invented or out-of-range
paragraph locators are rejected; the new paragraph remains previewable and
undoable through the same Apply/Undo flow.

### Known local environment issue: PowerShell 7

The maintained Agent-runtime regression `test_shell_runner_returns_bounded_evidence`
requires PowerShell 7 (`pwsh`). On a machine without it, that one test fails because
shell execution is intentionally unavailable; this is an environment prerequisite, not
an Office feature failure. Install PowerShell 7 or set `DESKORB_AGENT_PWSH` to its
executable path, then rerun the test suite.

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

In agent mode, Full access executes requested shell and desktop-input actions without
an approval prompt. Normal application launch, navigation,
typing, shortcuts, scrolling, and window-focus steps execute automatically.
The runtime pauses only before file deletion, including common PowerShell, `cmd`, Python,
.NET, and `git clean` deletion commands.

### Optional read-only MCP capabilities and local evaluation

The example MCP configuration also contains two opt-in, fail-closed adapters:
`document_mcp.py` reads one document inside an explicitly configured local root,
and `public_fetch_mcp.py` fetches only allowlisted public HTTPS pages. They reject
credentials, private or non-global DNS results, unsafe redirects, out-of-root paths,
oversized input/output, and non-text responses. Neither adapter writes files or
stores prompts, page bodies, credentials, or screenshots.

The deterministic regression control group remains available through
`tests/matrix_e2e_runner.py`. Capability checks can be run with
`tests/capability_e2e_runner.py`; the real local matrix is opt-in and fail-closed:

```powershell
python tests/local_real_e2e_runner.py --repetitions 1 --output artifacts/local-real-e2e.json
```

Use `--allow-public-network` and `--allow-current-desktop` only on the intended
machine. A missing interactive desktop, model, or human handoff is reported as
`blocked`, never as a successful task. Reports contain normalized counters and
failure categories only.

The test entrypoints and their resource boundaries are documented in
[`tests/README.md`](tests/README.md). The deterministic runner and the real
runner share one canonical 60-case dataset loader; the old runner function
imports remain available for compatibility.

## Attribution

Copyright (c) 2025 Shengyan Lin. Original project and UI licensed under the MIT
license in `LICENSE`. Codex-specific adapter changes are provided under the
same license.
