# -*- coding: utf-8 -*-
"""Configuration constants, theme palette, and the system-prompt append for
DeskOrb Agent. Pure data + small env helpers; no project imports (leaf module),
so anything may import it without a circular-import risk."""

import os
from pathlib import Path

from provider_env import (api_base_url as _provider_api_base_url, api_model as _provider_api_model,
                          model_capabilities_raw as _provider_model_capabilities,
                          model_fallbacks_raw as _provider_model_fallbacks)
from model_registry import parse_capability_overrides, parse_fallback_targets

__version__ = "0.2.0"

def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        val = int(os.environ.get(name, ""))
    except Exception:
        return default
    return max(min_value, min(max_value, val))



def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from the environment so a machine can override a committed
    default WITHOUT editing source (used by STRICT_MCP_CONFIG below). Unset OR blank
    (empty / whitespace-only) -> default, so a stray space can't silently flip the
    flag; "0"/"false"/"no"/"off" (any case) -> False; anything else -> True."""
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip().lower()
    if v == "":
        return default
    return v not in ("0", "false", "no", "off")

WORKING_DIR = os.environ.get("DESKORB_AGENT_WORKING_DIR", os.environ.get("CODEX_OVERLAY_WORKING_DIR", str(Path.home())))
# Model IDs are FAMILY ALIASES ("opus"/"sonnet"/"haiku"), not pinned versions, so the
# overlay always runs the LATEST model of each family — when Anthropic ships a new one
# (e.g. a future Sonnet 5), it's picked up automatically with NO code change. The CLI
# documents these as "an alias for the latest model" (`claude --help`, see --model), and
# the "[1m]" 1M-context suffix composes with the alias too ("opus[1m]" → the latest Opus
# at 1M context). Don't use model=None: the Agent SDK resolves None to an OLDER model
# (SDK 0.2.87 → opus-4-7), not the CLI default, so we always pass an explicit alias.
# CAVEAT (measured 2026-07, CLI 2.1.156 / SDK 0.2.87): over the SDK's *streaming*
# transport — which is exactly how the overlay talks to the CLI — the CLI resolves a bare
# alias to a VERSION-BEHIND model (streaming "opus" → 4-7) even though its one-shot `-p`
# mode resolves the same alias to the latest (4-8). So we keep the alias HERE (to preserve
# auto-update on new releases) but the worker resolves it to the concrete latest id at
# startup via modelresolve.resolve_model (which probes the CLI's honest `-p` path once and
# caches the result, re-probing only when the CLI itself changes). The statusline shows the
# concrete version each alias resolved to
# (e.g. "claude-opus-4-8"), so you can always see what you're on.
MODEL = os.environ.get("DESKORB_AGENT_MODEL", os.environ.get("CODEX_OVERLAY_MODEL", "gpt-5.6-sol"))
MODELS = [("GPT-5.6 Sol", "gpt-5.6-sol"),
          ("GPT-5.6 Terra", "gpt-5.6-terra"),
          ("GPT-5.6 Luna", "gpt-5.6-luna")]
CONNECTION_BACKEND = os.environ.get("DESKORB_AGENT_BACKEND", os.environ.get("CODEX_OVERLAY_BACKEND", "agent")).strip().lower()
if CONNECTION_BACKEND not in ("auto", "codex", "api", "agent"):
    CONNECTION_BACKEND = "auto"
API_BASE_URL = (os.environ.get("OPENAI_BASE_URL", "").strip() or _provider_api_base_url()
                or "https://api.openai.com/v1").rstrip("/")
# ``auto`` detects DeepSeek and DashScope/Qwen from their official base URLs;
# unknown custom endpoints retain the project's existing Responses-compatible
# behaviour.  Set openai-compatible for Ollama, vLLM and similar chat APIs.
MODEL_PROVIDER = os.environ.get("DESKORB_AGENT_PROVIDER", "auto").strip().lower() or "auto"
API_PROXY_URL = os.environ.get("DESKORB_AGENT_API_PROXY", os.environ.get("CODEX_OVERLAY_API_PROXY", "")).strip()
API_MODEL = _provider_api_model("gpt-5.6-terra")
try:
    MODEL_FALLBACKS = parse_fallback_targets(_provider_model_fallbacks())
except Exception:
    MODEL_FALLBACKS = []
try:
    MODEL_CAPABILITY_OVERRIDES = parse_capability_overrides(_provider_model_capabilities())
except Exception:
    MODEL_CAPABILITY_OVERRIDES = []
API_TIMEOUT = _env_int("DESKORB_AGENT_API_TIMEOUT", 180, 15, 900)
API_REQUEST_RETRIES = _env_int("DESKORB_AGENT_API_REQUEST_RETRIES", 2, 0, 4)
API_CONTEXT_TOKEN_BUDGET = _env_int("DESKORB_AGENT_CONTEXT_TOKENS", 24_000, 4_000, 200_000)
API_CONTEXT_RECENT_TURNS = _env_int("DESKORB_AGENT_RECENT_TURNS", 6, 2, 24)
API_CONTEXT_SUMMARY_TOKENS = _env_int("DESKORB_AGENT_SUMMARY_TOKENS", 1_200, 256, 4_096)
# Local MCP bridge. With no config file DeskOrb starts Microsoft's Playwright MCP server
# through npx on demand. Set DESKORB_AGENT_MCP_CONFIG to a standard mcpServers JSON file
# to replace or add trusted local servers; set PLAYWRIGHT_MCP=0 to disable the default.
MCP_CONFIG_PATH = os.environ.get("DESKORB_AGENT_MCP_CONFIG", "").strip()
PLAYWRIGHT_MCP_ENABLED = _env_bool("DESKORB_AGENT_PLAYWRIGHT_MCP", True)
BROWSER_BACKEND = os.environ.get("DESKORB_AGENT_BROWSER_BACKEND", "isolated-playwright").strip().lower()
if BROWSER_BACKEND not in {"isolated-playwright", "connected-playwright"}:
    BROWSER_BACKEND = "isolated-playwright"
MCP_TIMEOUT_SECONDS = _env_int("DESKORB_AGENT_MCP_TIMEOUT", 30, 5, 120)
PERMISSION_MODE = "workspace-write"
                                 # the STARTUP permission mode; flip it at run time with the
                                 # status-bar "Read-only" toggle (◉ = "plan", a read-only agent
                                 # that can look and answer but not edit/run anything; ○ = back
                                 # to this configured mode). "plan" here starts the overlay
                                 # locked read-only. See the security note in README.md.
# Tools the overlay must NEVER let the model call, because they need an interactive UI
# this app can't provide. AskUserQuestion (Codex Code's structured multiple-choice
# question tool) is the one that bites: when the model calls it, the CLI blocks waiting
# for an answer that a GUI-with-no-TTY has no way to supply — so the turn hangs until the
# 30-min TOOL_IDLE_TIMEOUT fires (the "overlay froze on a question" bug). Disallowing it
# removes it from the tool schema entirely, so the model can't call it and instead asks
# its question inline as plain text — which the chat renders and you just type a reply to
# (the behaviour the overlay had before a CLI update started actively invoking the tool).
# Belt-and-suspenders: worker._allow_tool also DENIES it at run time, so even if it ever
# leaks back in (a skill, a future CLI that ignores this list) the turn can't hang.
DISALLOWED_TOOLS = ["AskUserQuestion"]
# Lean by default: do NOT inherit the user's ~/.claude MCP servers. The overlay is a
# lightweight screen-chat that only needs the core Codex Code tools; inheriting every
# MCP server the user has configured (Atlassian, Figma, M365, ...) injects their tool
# schemas into the context - easily 50-70K+ tokens, a third of a 200K window, gone
# before you type. Override per-machine WITHOUT editing source (so a release never has
# to toggle this constant): set DESKORB_AGENT_STRICT_MCP=0 to inherit your MCP
# servers/connectors (incl. claude.ai Microsoft 365) for calendar/Outlook etc.
STRICT_MCP_CONFIG = _env_bool("DESKORB_AGENT_STRICT_MCP", True)

SKILLS = "all"                    # which Agent SDK skills to enable in the overlay. Default None
                                  # means the overlay discovers NO skills (the SDK only wires up
                                  # skill discovery when this is set). A list enables ONLY those
                                  # skills by name — lean, just their description lands in context.
                                  # "all" enables every discovered skill (heavier: every skill's
                                  # description is injected). Setting this also makes the CLI load
                                  # ~/.claude settings (setting_sources defaults to user+project),
                                  # but NOT MCP servers (STRICT_MCP_CONFIG still blocks those).
                                  # None → skills off entirely.
AUTO_SCREENSHOT_DEFAULT = True
# Enables the Codex computer-use tool already provided by this desktop install.
# It is intentionally opt-out (set CODEX_OVERLAY_COMPUTER_USE=0 to disable): the
# model is still instructed to use it only after a direct user request.
COMPUTER_USE_ENABLED = _env_bool("CODEX_OVERLAY_COMPUTER_USE", True)
SHOW_IN_SCREEN_SHARE_DEFAULT = _env_bool("DESKORB_AGENT_SHOW_IN_SCREEN_SHARE", True)
# The default avoids WDA_EXCLUDEFROMCAPTURE, which prevents this Tk window from
# rendering in some remote or overlay desktop environments. Set the environment
# variable to 0 only after confirming the private screen-share mode remains visible.
HIDE_SCREENSHOT_TOOL = True       # hide the noisy "⚙ Read …shot_*.png" lines every turn
HOTKEY = "ctrl+alt+space"
THEME = "light"                  # "light" (Codex paper) or "dark" (warm dark)
WINDOW_ALPHA = 1.0
CORNER_RADIUS = 18
TASKBAR_BUTTON = True            # show a real, clickable Windows taskbar button (with the
                                 # Clawd icon), like a normal app — alt-tab target, click to
                                 # focus/raise, see at a glance that it's running. The frameless
                                 # (overrideredirect) window gets NO taskbar button by default;
                                 # this forces one via WS_EX_APPWINDOW. False → the pure
                                 # no-taskbar floating overlay (original behaviour).
FRAMELESS_WINDOW = _env_bool("DESKORB_AGENT_FRAMELESS_WINDOW", False)
# Standard decorations avoid an invisible Tk window on some desktops. The matching
# custom Win32 window region is also opt-in, because it clips native title bars.
CUSTOM_WINDOW_REGION = _env_bool("DESKORB_AGENT_CUSTOM_WINDOW_REGION", FRAMELESS_WINDOW)
APP_ICON = ""  # use the Tk default unless a local icon is supplied
                                 # script (or absolute). "" → no custom icon (Tk default).
APP_ID = "deeplearingx.deskorb-agent"  # explicit Windows AppUserModelID. Without it a pythonw
                                 # app shows pythonw's icon in the taskbar and groups with other
                                 # Python apps; setting it makes the taskbar use APP_ICON instead.
ORB_SIZE = 56                    # diameter (logical px) of the collapsed Codex orb
ORB_IMAGE = ""  # collapsed-orb artwork. "" → procedural glossy
                                 # terracotta sphere (original look). A path (relative
                                 # to this script, or absolute) to a PNG/ICO renders that
                                 # image instead: it's auto-scaled + centred so the whole
                                 # opaque shape fits inside the circular orb. RGBA with a
                                 # transparent background works best.
ORB_IMAGE_MARGIN = 0.04          # fraction of the radius kept clear around the artwork
                                 # (0 = touches the circle edge; 0.04 = tiny breathing room)
ORB_FLOAT = True                 # True + an ORB_IMAGE → the collapsed orb is clipped to the
                                 # artwork's own silhouette (a free-floating pixel sprite, no
                                 # circular frame; clicks outside the shape pass through).
                                 # False → the classic circular orb. Ignored without artwork.
ORB_ALPHA_THRESHOLD = 110        # pixels at/above this alpha (0-255) count as "solid" when
                                 # building the silhouette — higher = tighter, crisper edge
# Fonts. Noto Sans/Serif TC cover Chinese + English in one family (closest free
# stand-in for Codex's proprietary Styrene/Copernicus). First available wins.
FONT_SANS = ["Noto Sans TC", "Inter", "Segoe UI Variable Text", "Segoe UI"]
FONT_SERIF = ["Noto Serif TC", "Georgia", "Cambria"]   # the "Codex" wordmark
FONT_MONO = ["Consolas", "Cascadia Mono", "Courier New"]
SHOT_DIR = Path(os.environ.get("TEMP", str(Path.home()))) / "deskorb_agent_shots"
KEEP_SHOTS = 24                  # retain a few captures worth (one file per monitor)
SHOT_MAX_EDGE = 1568             # downscale captures to this long edge before sending.
SHOT_FORMAT = os.environ.get("DESKORB_AGENT_SHOT_FORMAT", "auto").strip().lower()
                                 # "auto" saves PNG + JPEG and keeps the smaller payload;
                                 # "png" preserves old behavior; "jpeg" favors upload speed.
SHOT_JPEG_QUALITY = _env_int("DESKORB_AGENT_SHOT_JPEG_QUALITY", 82, 50, 95)
                                 # Codex downsamples larger images internally anyway, so
                                 # bigger files only cost upload time + vision tokens.
SHOT_SCOPE = os.environ.get("DESKORB_AGENT_SHOT_SCOPE", "screens").strip().lower()
                                 # what a screenshot covers — the STARTUP default; flip it live
                                 # via the status-bar "Window-only" toggle. "screens" (default):
                                 # one image per monitor, Codex sees everything you see.
                                 # "window": ONLY the active (foreground) window — more private
                                 # and far cheaper in vision tokens, but Codex can't see other
                                 # windows/monitors. When you're typing IN the overlay, "active"
                                 # means the last window you worked in before it (tracked live);
                                 # when no usable window exists (fresh launch, desktop focused,
                                 # window minimized) it falls back to full-screen capture rather
                                 # than sending nothing. Any other value → "screens".
SHOT_SCOPE_FORCED = "DESKORB_AGENT_SHOT_SCOPE" in os.environ
                                 # an EXPLICIT env var is a per-launch decision, so it beats the
                                 # remembered toggle state below; unset → last toggle choice wins
STATE_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "deskorb-agent" / "state.json"
                                 # tiny per-machine store for UI-toggle state the user expects to
                                 # survive a relaunch (Window-only and Read-only). Deliberately
                                 # OUTSIDE the app folder: machine state must not dirty the git
                                 # clone or ride along in updates. SHOT_SCOPE / PERMISSION_MODE
                                 # seed the very first launch; after that the remembered toggle
                                 # wins — and because a restored Read-only choice is a SAFETY
                                 # state, the overlay announces it in-chat whenever it differs
                                 # from the configured default.
IMAGE_INPUT = "inline"           # "inline" → attach screenshots as base64 image blocks
                                 # (no per-turn Read round-trip); "read" → legacy path:
                                 # save PNG + ask Codex to Read it. Flip to "read" if a
                                 # future CLI rejects inline images.
PRECAPTURE_ON_TYPING = True      # grab the screen ~as you type (off the send path) so
                                 # send latency excludes the capture.
PRECAPTURE_MAX_AGE = 6.0         # seconds a pre-captured frame stays reusable; older than
                                 # this at send time → re-grab fresh (bounds staleness).
MAX_BUFFER_SIZE = 64 * 1024 * 1024   # the SDK aborts a turn with CLIJSONDecodeError when a
                                 # single stream-json line exceeds this (default 1MB). Inline
                                 # screenshots (base64, ×monitors) blow past 1MB easily and
                                 # used to crash the worker — 64MB gives huge headroom.
# A hang is NOT an exception, so the reconnect / bounded-restart guards (which only fire
# on a raised error) can't preempt an SDK call that never resolves — a wedged transport
# (broken corporate TLS, half-open socket, CLI waiting on a prompt with no TTY) would pin
# the worker forever. Bound every SDK lifecycle call so a hang degrades to a clean
# reconnect instead of a permanent freeze.
CONNECT_TIMEOUT = 30        # connect() that hasn't resolved by here ⇒ wedged transport
QUERY_TIMEOUT = 60          # sending the request is near-instant; bound it anyway
DISCONNECT_TIMEOUT = 10     # don't let a stuck disconnect hang shutdown/reconnect
RECV_IDLE_TIMEOUT = 300     # no stream activity for this long ⇒ treat the transport as dead
                            # (generous: a long-running tool can legitimately go quiet a while)
MAX_INLINE_IMAGE_BYTES = 16 * 1024 * 1024   # never base64-inline a local file bigger than this
                            # (a multi-GB file with an image extension would otherwise be read
                            # whole into RAM and explode the query payload)
MAX_CHAT_LINES = 4000       # cap the rendered transcript; prune oldest lines past this so a
                            # very long session doesn't slow Tk layout / leak embedded canvases
MAX_CHAT_CHARS = 350_000    # also cap by characters — one giant whitespace-free assistant
                            # line counts as 1 line and would otherwise bypass the line cap
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
TOOL_IDLE_TIMEOUT = 1800    # once a tool call is in flight, allow a much longer silent gap
                            # (a long build/test can legitimately stream nothing for minutes)
COMPACT_IDLE_TIMEOUT = 600  # /compact is one big summarization round-trip that streams nothing
                            # for a while (≈30s even on a small context); bound it generously
MAX_PASTE_SOURCES = 8       # cap how many files one paste fans out into
MAX_PENDING_IMAGES = 16     # cap total queued attachments (a hostile clipboard can't pile up)
MAX_PASTE_PIXELS = 32_000_000   # reject a pasted image above this pixel count BEFORE decode/
                            # thumbnail — a "decompression bomb" PNG decodes to a huge bitmap
                            # (Pillow only *warns*, doesn't raise, below ~178M px)
MAX_INLINE_IMAGES = 16      # cap images per turn (count) ...
MAX_INLINE_TOTAL_BYTES = 32 * 1024 * 1024   # ... and aggregate bytes (the per-file cap alone
                            # doesn't bound many-attachment memory blow-up)
MAX_UPDATE_BODY = 1 * 1024 * 1024   # cap the update-check response body before json.loads
MAX_UPDATE_TAGS = 300       # and the number of tags parsed
CLI_UPDATE_CHECK = False
                            # the `claude` CLI the overlay drives is behind the latest npm release
                            # and, if so, show a one-click Update notice in the chat (see
                            # cliupdate.py). The overlay and the CLI update independently: keeping
                            # the overlay current never advances the CLI, and an old CLI silently
                            # runs an older model. Set DESKORB_AGENT_CLI_UPDATE_CHECK=0 to disable
                            # (e.g. a locked-down box where global npm installs aren't allowed)

SYSTEM_APPEND = (
    "You are running as an always-on-top floating overlay assistant on the user's "
    "Windows 11 desktop. The user talks to you without leaving their current app. "
    "Messages may include live screenshots of the user's screen — attached directly "
    "as images, or (legacy) as an [ATTACHMENTS] path you open with the Read tool. "
    "Use them to see what the user is looking at, then help. "
    + (
        "The user has granted this overlay permission to use the available computer-use tool "
        "to control the Windows mouse and keyboard, but ONLY when the user explicitly asks "
        "you to perform a desktop action. State the intended action before acting; never make "
        "purchases, send messages, disclose secrets, or accept irreversible prompts without a "
        "fresh confirmation. "
        if COMPUTER_USE_ENABLED else
        "Computer-use is disabled for this overlay session. "
    )
    + "Keep replies concise and skimmable since they render in a small floating window; "
    "expand only when asked. "
    "If you need to ask the user something, ask it inline as plain text and wait for "
    "their typed reply. This overlay is a plain chat with no interactive question UI, so "
    "never use a structured multiple-choice question tool — such a tool has no way to be "
    "answered here and would just stall the turn. "
    "When automating Office (PowerPoint/Excel/Word) via PowerShell+COM, optimize for "
    "speed: a NEW PowerShell process runs per tool call and COM state does NOT persist "
    "across calls, and every property access is a slow cross-process round-trip. So: "
    "(1) BATCH — do all inspection in ONE script (return what you need, e.g. as JSON), "
    "then apply ALL edits in ONE script; never one tool call per shape/cell/slide. "
    "(2) Within a script cache COM references in variables (grab the slide/shape/table "
    "once) instead of re-walking the object model, and don't re-read everything to verify "
    "after each write. (3) For Excel bulk writes, set Application.ScreenUpdating=$false, "
    "Calculation=xlManual and EnableEvents=$false around them, then restore. (4) For large "
    "purely-textual edits where the live open document isn't needed, python-pptx/openpyxl "
    "on the file is far faster than COM — but only when the file is NOT open in Office."
)

THEMES = {
    "light": {
        "bg": "#FAF9F5", "field": "#FFFFFF", "user_card": "#EFEBE1",
        "text": "#28261F", "muted": "#73706A", "faint": "#A9A59B",
        "accent": "#D97757", "accent_hi": "#C25E40", "on_accent": "#FFFFFF",
        "border": "#E6E2D8", "tool_bg": "#F2EFE7", "err": "#B4413A",
        "sel": "#EADDD3", "hover": "#EFEBE1",
    },
    "dark": {
        "bg": "#262624", "field": "#1F1E1D", "user_card": "#34332F",
        "text": "#ECEAE3", "muted": "#9B978D", "faint": "#6F6C64",
        "accent": "#D97757", "accent_hi": "#E68A6C", "on_accent": "#FFFFFF",
        "border": "#3A3934", "tool_bg": "#2E2D2A", "err": "#E0897D",
        "sel": "#3A3934", "hover": "#30302E",
    },
}
T = THEMES.get(THEME, THEMES["light"])
