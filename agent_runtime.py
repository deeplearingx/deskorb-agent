"""Independent, API-backed desktop-agent runtime for the floating overlay."""
from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from config import (API_CONTEXT_RECENT_TURNS, API_CONTEXT_TOKEN_BUDGET, API_REQUEST_RETRIES, API_TIMEOUT,
                    MCP_CONFIG_PATH, MCP_TIMEOUT_SECONDS, OFFICECLI_AUTO_APPROVE, OFFICECLI_BINARY,
                    OFFICECLI_ENABLED, OFFICECLI_MAX_TOOL_ROUNDS, OFFICECLI_TIMEOUT_SECONDS,
                    API_MAX_TOOL_ROUNDS,
                    MODEL_PROVIDER,
                    PLAYWRIGHT_MCP_ENABLED,
                    SYSTEM_APPEND, WORKING_DIR)
from agent_policy import ApprovalManager, Risk, ToolPolicy
from desktop_tools import DesktopTools
from mcp_client import MCPError, MCPToolBridge
from model_adapter import ModelAdapter
from conversation_context import ConversationContext
from credential_store import get_api_key
from desktop_activity_indicator import ACTIVITY_TOOLS, BROWSER_ACTIVITY_TOOLS, DesktopActivityEvent, DESKTOP_ACTIVITY_TOOLS
from browser_actions import STATE_CHANGING_ACTIONS
from browser_runtime import BrowserExecutionSession, PlaywrightMCPBackend
from responses_tool_protocol import continue_input, function_call_output, function_calls
from runtime_task_state import RuntimeTaskState
from task_runtime import InMemoryTaskJournal, classify_failure
from win32utils import foreground_capture_window, window_bbox, window_process_name, window_title


class ReadOnlyTools:
    """Local observations constrained to the configured working directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {
                "type": "function", "name": "desktop_get_active_window", "strict": True,
                "description": "Get metadata for the current foreground desktop window.",
                "parameters": {"type": "object", "properties": {}, "required": [],
                               "additionalProperties": False},
            },
            {
                "type": "function", "name": "filesystem_list", "strict": True,
                "description": "List files and folders below the configured working directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "max_entries": {"type": "integer"}},
                    "required": ["path", "max_entries"], "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "filesystem_read_text", "strict": True,
                "description": "Read a UTF-8-compatible text file below the configured working directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}},
                    "required": ["path", "max_chars"], "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "filesystem_search_text", "strict": True,
                "description": "Search text files below the configured working directory for a literal query.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "query": {"type": "string"},
                                   "max_results": {"type": "integer"}},
                    "required": ["path", "query", "max_results"], "additionalProperties": False,
                },
            },
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "desktop_get_active_window":
                hwnd = foreground_capture_window()
                return {"ok": True, "window": {
                    "title": window_title(hwnd) if hwnd else "",
                    "capturable": bool(hwnd and window_bbox(hwnd)),
                }}
            if name == "filesystem_list":
                path = self._path(arguments["path"])
                limit = self._limit(arguments["max_entries"], 1, 200)
                if not path.is_dir():
                    return {"ok": False, "error": "Path is not a directory."}
                entries = []
                for entry in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:limit]:
                    try:
                        entries.append({"path": str(entry), "name": entry.name, "kind": "directory" if entry.is_dir() else "file",
                                        "size": None if entry.is_dir() else entry.stat().st_size})
                    except OSError:
                        continue
                return {"ok": True, "root": str(self.root), "entries": entries}
            if name == "filesystem_read_text":
                path = self._path(arguments["path"])
                limit = self._limit(arguments["max_chars"], 1, 200_000)
                if not path.is_file():
                    return {"ok": False, "error": "Path is not a file."}
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read(limit + 1)
                return {"ok": True, "path": str(path), "truncated": len(text) > limit, "text": text[:limit]}
            if name == "filesystem_search_text":
                return self._search(arguments)
            return {"ok": False, "error": f"Unknown read-only tool: {name}"}
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"Invalid tool arguments: {exc}"}
        except OSError as exc:
            return {"ok": False, "error": f"Filesystem error: {exc}"}

    def _path(self, value: Any) -> Path:
        candidate = Path(str(value)).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path is outside the configured working directory.") from exc
        return candidate

    @staticmethod
    def _limit(value: Any, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, int(value)))

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root = self._path(arguments["path"])
        query = str(arguments["query"])
        limit = self._limit(arguments["max_results"], 1, 100)
        if not root.is_dir() or not query:
            return {"ok": False, "error": "Search requires a directory and non-empty query."}
        results: list[dict[str, Any]] = []
        scanned = 0
        for current, directories, files in os.walk(root):
            directories[:] = [item for item in directories if item not in {".git", ".venv", "node_modules", "__pycache__"}]
            for filename in files:
                if scanned >= 1500 or len(results) >= limit:
                    break
                scanned += 1
                path = Path(current) / filename
                try:
                    if path.stat().st_size > 1_000_000:
                        continue
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        for line_no, line in enumerate(handle, 1):
                            if query in line:
                                results.append({"path": str(path), "line": line_no, "text": line.strip()[:500]})
                                if len(results) >= limit:
                                    break
                except (OSError, UnicodeError):
                    continue
            if scanned >= 1500 or len(results) >= limit:
                break
        return {"ok": True, "results": results, "scanned_files": scanned, "truncated": scanned >= 1500}


class ControlledTools(ReadOnlyTools):
    """Local tools with bounded write and command execution primitives."""

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [*ReadOnlyTools.schemas(),
            {"type": "function", "name": "filesystem_write", "strict": True,
             "description": "Write UTF-8 text to a file below the configured working directory.",
             "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}, "overwrite": {"type": "boolean"}}, "required": ["path", "text", "overwrite"], "additionalProperties": False}},
            {"type": "function", "name": "shell_run", "strict": True,
             "description": "Run a PowerShell 7 command in the configured working directory. Confirmation is required only when the command deletes files.",
             "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, "required": ["command", "timeout_seconds"], "additionalProperties": False}},
            {"type": "function", "name": "application_launch", "strict": True,
             "description": "Launch an installed desktop application by its common name. Use this for requests such as opening Chrome, Edge, or QQ; never tell the user to click an icon when this tool can launch it.",
             "parameters": {"type": "object", "properties": {"application": {"type": "string", "enum": ["chrome", "edge", "firefox", "qq", "explorer", "notepad", "calculator"]}}, "required": ["application"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_capture_state", "strict": True,
             "description": "Capture a short-lived desktop state snapshot before a coordinate action.",
             "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
            {"type": "function", "name": "desktop_list_windows", "strict": True,
             "description": "List visible top-level Windows windows and return short-lived window IDs for safe window control.",
             "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
            {"type": "function", "name": "window_control", "strict": False,
             "description": "Control a window selected from a fresh desktop_list_windows result. Execute the requested window action automatically.",
             "parameters": {"type": "object", "properties": {"window_id": {"type": "integer"}, "action": {"type": "string", "enum": ["focus", "minimize", "maximize", "restore", "snap_left", "snap_right", "move_resize", "toggle_topmost", "close"]}, "x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer"}, "height": {"type": "integer"}}, "required": ["window_id", "action"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_clipboard_read_text", "strict": True,
             "description": "Read Unicode text currently in the Windows clipboard when requested.",
             "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
            {"type": "function", "name": "desktop_click", "strict": True,
             "description": "Click screen coordinates from a fresh desktop snapshot. Use count=2 only for an intentional double-click.",
             "parameters": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "count": {"type": "integer", "enum": [1, 2]}, "risk_level": {"type": "string", "enum": ["normal", "high"]}, "risk_reason": {"type": "string"}}, "required": ["snapshot_id", "x", "y", "button", "count", "risk_level", "risk_reason"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_type", "strict": True,
             "description": "Type Unicode text into the current target.",
             "parameters": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "text": {"type": "string"}, "risk_level": {"type": "string", "enum": ["normal", "high"]}, "risk_reason": {"type": "string"}}, "required": ["snapshot_id", "text", "risk_level", "risk_reason"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_hotkey", "strict": True,
             "description": "Press an allowlisted keyboard shortcut.",
             "parameters": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "keys": {"type": "array", "items": {"type": "string"}}, "risk_level": {"type": "string", "enum": ["normal", "high"]}, "risk_reason": {"type": "string"}}, "required": ["snapshot_id", "keys", "risk_level", "risk_reason"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_scroll", "strict": True,
             "description": "Scroll from a fresh snapshot. Positive is up/right; negative is down/left.",
             "parameters": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "delta": {"type": "integer"}, "axis": {"type": "string", "enum": ["vertical", "horizontal"]}}, "required": ["snapshot_id", "delta", "axis"], "additionalProperties": False}},
            {"type": "function", "name": "window_focus", "strict": True,
             "description": "Focus a window by its exact title.",
             "parameters": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_verify_state", "strict": True,
             "description": "Compare the current desktop state against a prior snapshot after an action.",
             "parameters": {"type": "object", "properties": {"snapshot_id": {"type": "string"}}, "required": ["snapshot_id"], "additionalProperties": False}},
        ]

    def write_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._path(arguments["path"])
        if path.exists() and not bool(arguments["overwrite"]):
            return {"ok": False, "error": "Target already exists; overwrite is false."}
        path.parent.mkdir(parents=True, exist_ok=True)
        text = str(arguments["text"])
        path.write_text(text, encoding="utf-8")
        verified = path.read_text(encoding="utf-8")
        return {"ok": verified == text, "path": str(path), "bytes": path.stat().st_size,
                "verified": verified == text}

    def run_shell(self, arguments: dict[str, Any]) -> dict[str, Any]:
        timeout = self._limit(arguments["timeout_seconds"], 1, 60)
        command = str(arguments["command"]).strip()
        if not command:
            return {"ok": False, "error": "Command is empty."}
        executable = self._powershell_7_executable()
        if not executable:
            return {"ok": False, "error": "PowerShell 7 executable 'pwsh' was not found in PATH. Install PowerShell 7 or set DESKORB_AGENT_PWSH."}
        flags = 0x08000000 if os.name == "nt" else 0
        completed = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                                   cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=timeout, creationflags=flags)
        cap = 32 * 1024
        return {"ok": completed.returncode == 0, "exit_code": completed.returncode,
                "stdout": completed.stdout[:cap], "stderr": completed.stderr[:cap],
                "output_truncated": len(completed.stdout) > cap or len(completed.stderr) > cap}

    def launch_application(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = str(arguments.get("application", "")).strip().lower()
        executable = self._resolve_application(name)
        if not executable:
            return {"ok": False, "error": f"Application is not installed or could not be located: {name}"}
        if os.name == "nt" and executable.lower().endswith(".lnk"):
            os.startfile(executable)
            return {"ok": True, "application": name, "pid": None, "via": "shortcut"}
        flags = 0x08000000 if os.name == "nt" else 0
        process = subprocess.Popen([executable], cwd=self.root, close_fds=True,
                                   creationflags=flags, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        return {"ok": True, "application": name, "pid": process.pid}

    @staticmethod
    def _resolve_application(name: str) -> str | None:
        aliases = {
            "chrome": ("chrome.exe", [
                ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
                ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
                ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
            ]),
            "edge": ("msedge.exe", [
                ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
                ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
                ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
            ]),
            "firefox": ("firefox.exe", [
                ("PROGRAMFILES", "Mozilla Firefox/firefox.exe"),
                ("PROGRAMFILES(X86)", "Mozilla Firefox/firefox.exe"),
            ]),
            "qq": ("QQ.exe", [
                ("PROGRAMFILES", "Tencent/QQNT/QQ.exe"),
                ("PROGRAMFILES(X86)", "Tencent/QQNT/QQ.exe"),
                ("LOCALAPPDATA", "Programs/QQ/QQ.exe"),
            ]),
            "explorer": ("explorer.exe", []),
            "notepad": ("notepad.exe", []),
            "calculator": ("calc.exe", []),
        }
        spec = aliases.get(name)
        if not spec:
            return None
        command, locations = spec
        found = shutil.which(command)
        if found:
            return found
        for env_name, relative in locations:
            base = os.environ.get(env_name, "").strip()
            if base:
                candidate = Path(base) / Path(relative)
                if candidate.is_file():
                    return str(candidate)
        if name == "qq" and os.name == "nt":
            roots = [
                Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
                Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
                Path.home() / "Desktop",
            ]
            for root in roots:
                if not root.is_dir():
                    continue
                try:
                    shortcut = next(root.rglob("QQ.lnk"), None)
                except OSError:
                    shortcut = None
                if shortcut:
                    return str(shortcut)
        return None

    @staticmethod
    def _powershell_7_executable() -> str | None:
        configured = os.environ.get("DESKORB_AGENT_PWSH", "").strip()
        candidates = [configured, shutil.which("pwsh"), shutil.which("pwsh.exe"),
                      shutil.which("powershell"), shutil.which("powershell.exe")]
        for candidate in candidates:
            if candidate:
                return candidate
        return None


class AgentRuntime:
    """One local task loop using a Responses-compatible HTTPS provider."""

    MAX_TOOL_ROUNDS = API_MAX_TOOL_ROUNDS
    # Long OfficeCLI tasks can spend several minutes in a single model turn
    # while the runtime prepares and verifies a document. Keep this aligned
    # with the configurable API timeout instead of aborting after 45 seconds.
    REQUEST_TIMEOUT = 180
    TASK_AUTHORIZATION_SECONDS = 600
    TRANSIENT_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
    HUMAN_VERIFICATION_TIMEOUT_SECONDS = 900
    BROWSER_HANDOFF_TIMEOUT_SECONDS = 120
    HUMAN_VERIFICATION_CONTINUE = "__deskorb_human_verification_complete__"
    HUMAN_VERIFICATION_CANCEL = "__deskorb_human_verification_cancel__"
    CAPTCHA_MARKERS = (
        "快速验证身份", "我是人类", "人机验证", "滑块验证", "安全验证", "验证码",
        "captcha", "verify you are human", "verify you're human", "security verification",
    )
    DESKTOP_ACTION_TOOLS = {"application_launch", "desktop_click", "desktop_type",
                            "desktop_hotkey", "desktop_scroll", "window_focus", "window_control"}
    BROWSER_TASK_MARKERS = (
        "浏览器", "网页", "网站", "搜索", "淘宝", "京东", "百度", "google", "browser",
        "website", "web page", "search", "http://", "https://",
    )

    def __init__(self, ui_queue, model: str, api_base_url: str, api_proxy_url: str = "",
                 working_dir: str | Path = WORKING_DIR, full_access: bool = True,
                 context_tokens: int = API_CONTEXT_TOKEN_BUDGET,
                 recent_turns: int = API_CONTEXT_RECENT_TURNS,
                 model_provider: str | None = None,
                 task_journal: Any | None = None):
        self.ui = ui_queue
        self.model = model
        self.model_provider = model_provider or MODEL_PROVIDER
        self.adapter = ModelAdapter(self.model_provider, api_base_url)
        self.api_base_url = self.adapter.profile.base_url
        self.api_proxy_url = api_proxy_url.rstrip("/")
        self.context = ConversationContext(token_budget=context_tokens, recent_turns=recent_turns)
        self.tools = ControlledTools(working_dir)
        self.policy = ToolPolicy()
        self.approvals = ApprovalManager()
        self.desktop = DesktopTools()
        try:
            self.mcp = MCPToolBridge(MCP_CONFIG_PATH, enable_playwright=PLAYWRIGHT_MCP_ENABLED,
                                     enable_officecli=OFFICECLI_ENABLED,
                                     officecli_binary=OFFICECLI_BINARY,
                                     timeout_seconds=MCP_TIMEOUT_SECONDS,
                                     officecli_timeout_seconds=OFFICECLI_TIMEOUT_SECONDS)
            self._mcp_configuration_error = ""
        except MCPError as exc:
            self.mcp = None
            self._mcp_configuration_error = str(exc)
        self.full_access = bool(full_access)
        self._cancelled = threading.Event()
        self._active_response = None
        self._response_lock = threading.Lock()
        self._pending_execution: tuple[Any, list[dict[str, Any]], str] | None = None
        # The transcript is kept in memory only while the user completes a CAPTCHA in
        # the already-open, local MCP browser.  It never contains CAPTCHA answers.
        self._pending_human_verification: dict[str, Any] | None = None
        self._human_handoff_timer: threading.Timer | None = None
        self._task_authorized_until = 0.0
        self._task_mcp_servers: set[str] = set()
        self.task_journal = task_journal if task_journal is not None else InMemoryTaskJournal()
        self._task_state: RuntimeTaskState | None = None
        self._desktop_target_launches = 0
        self._browser_recovery_attempts = 0
        self._browser_reobservation_required = False
        self._browser_session: BrowserExecutionSession | None = None
        self._browser_activity_ids: dict[str, int] = {}
        self._next_desktop_activity_id = 0

    def configure(self, model: str, api_base_url: str, api_proxy_url: str = "",
                  model_provider: str | None = None):
        self.model = model
        if model_provider is not None:
            self.model_provider = model_provider
        self.adapter = ModelAdapter(self.model_provider, api_base_url)
        self.api_base_url = self.adapter.profile.base_url
        self.api_proxy_url = api_proxy_url.rstrip("/")
        self.context.clear()
        self.approvals.pending = None
        self._pending_execution = None
        self._pending_human_verification = None
        self._cancel_human_handoff_timer()
        self._task_authorized_until = 0.0
        self._task_mcp_servers.clear()
        self._task_state = None
        self._desktop_target_launches = 0
        self._browser_recovery_attempts = 0
        self._browser_reobservation_required = False
        self._browser_session = None
        self._browser_activity_ids.clear()
        self.desktop.clear_target_window()

    def reset(self):
        self.context.clear()
        self.approvals.pending = None
        self._pending_execution = None
        self._pending_human_verification = None
        self._cancel_human_handoff_timer()
        self._task_authorized_until = 0.0
        self._task_mcp_servers.clear()
        self._task_state = None
        self._desktop_target_launches = 0
        self._browser_recovery_attempts = 0
        self._browser_reobservation_required = False
        self._browser_session = None
        self._browser_activity_ids.clear()
        self.desktop.clear_target_window()

    def compact(self, force: bool = True) -> dict[str, int] | None:
        """Summarize older turns while retaining recent dialogue verbatim."""
        api_key = get_api_key(self.model_provider)
        if not api_key:
            raise RuntimeError("API Key is not configured")
        return self._compact_context(api_key, force=force)

    def set_permission_mode(self, mode: str):
        self.full_access = str(mode) != "plan"
        if not self.full_access:
            self.approvals.pending = None
            self._pending_execution = None
            self._pending_human_verification = None
            self._task_authorized_until = 0.0

    def request_approval(self, tool_name: str, arguments: dict[str, Any], risk: Risk, summary: str):
        """Create a chat-native approval request for a future high-risk tool."""
        request = self.approvals.create(tool_name, arguments, risk, summary)
        self.ui.put(("approval", request.prompt()))
        return request

    def interrupt(self):
        self._cancelled.set()
        self._pending_human_verification = None
        self._cancel_human_handoff_timer()
        self._browser_activity_ids.clear()
        self._task_authorized_until = 0.0
        with self._response_lock:
            response = self._active_response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def run_turn(self, text: str, image_paths: list[str]):
        if not str(text or "").strip():
            self._block_empty_input()
            return
        try:
            return self._run_turn(text, image_paths)
        except BaseException as exc:
            self._task_authorized_until = 0.0
            self._finish_task("failed", failure_kind=self._runtime_failure_kind(exc))
            raise

    def set_desktop_target_window(self, hwnd: int) -> dict[str, Any]:
        """Constrain this runtime to a disposable foreground window."""
        result = self.desktop.set_target_window(hwnd)
        if result.get("ok"):
            self._desktop_target_launches = 0
        return result

    def clear_desktop_target_window(self) -> None:
        self.desktop.clear_target_window()
        self._desktop_target_launches = 0

    def run_ephemeral_turn(self, text: str, image_paths: list[str]):
        """Run one request without reading or updating normal conversation context."""
        if not str(text or "").strip():
            self._block_empty_input()
            return
        try:
            return self._run_turn(text, image_paths, ephemeral=True)
        except BaseException as exc:
            self._task_authorized_until = 0.0
            self._finish_task("failed", failure_kind=self._runtime_failure_kind(exc))
            raise

    def run_office_plan_turn(self, text: str):
        """Generate an ephemeral Office plan with no local or MCP tool access."""
        if not str(text or "").strip():
            self._block_empty_input()
            return
        try:
            return self._run_turn(text, [], ephemeral=True, allow_tools=False)
        except BaseException as exc:
            self._task_authorized_until = 0.0
            self._finish_task("failed", failure_kind=self._runtime_failure_kind(exc))
            raise

    def run_office_context_turn(self, question: str, office_prompt: str | None = None,
                                event_token: int | None = None):
        """Answer a persistent Office follow-up without storing Office text in context."""
        prompt = office_prompt if office_prompt is not None else question
        if not str(prompt or "").strip():
            self._block_empty_input()
            return
        try:
            self._office_event_token = event_token
            return self._run_turn(
                prompt,
                [], ephemeral=True, allow_tools=False,
            )
        except BaseException as exc:
            self._task_authorized_until = 0.0
            self._finish_task("failed", failure_kind=self._runtime_failure_kind(exc))
            raise
        finally:
            self._office_event_token = None

    def _runtime_failure_kind(self, error: Any) -> str:
        """Map an exception to the privacy-safe task failure taxonomy."""
        return classify_failure(str(error))

    def _task_requires_contract(self, text: str) -> bool:
        """Return whether a user request claims an external or local effect."""
        lowered = str(text or "").lower()
        markers = (
            "搜索", "查找", "研究", "检查", "诊断", "验证", "结果", "商品", "网页", "网站",
            "文件", "文档", "打开", "启动", "输入", "点击", "运行", "创建", "写入", "保存",
            "browser", "search", "research", "verify", "file", "document", "open", "launch",
            "type", "click", "run", "create", "write", "save",
        )
        return self._execution_requested(lowered) or any(marker in lowered for marker in markers)

    def _ensure_task_state(self, goal: str) -> RuntimeTaskState:
        if self._task_state is None:
            self._task_state = RuntimeTaskState.start(
                self.task_journal,
                goal,
                requires_action=self._task_requires_contract(goal),
            )
        return self._task_state

    def _finish_task(self, terminal: str, *, failure_kind: str | None = None) -> dict[str, Any] | None:
        state = self._task_state
        if state is None:
            return None
        progress = state.finish(terminal, failure_kind=failure_kind)
        self.ui.put(("task_progress", progress))
        self._task_state = None
        return progress

    def _publish_tool_result(self, name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        """Publish bounded tool telemetry without arguments or free-form output."""
        payload = {
            "tool": str(name),
            "ok": bool(isinstance(result, dict) and result.get("ok")),
            "verified": bool(isinstance(result, dict) and result.get("verified")),
            "high_risk": bool(self._high_risk_call(name, arguments)),
        }
        if not payload["ok"]:
            explicit = str(result.get("failure_kind") or "").strip() if isinstance(result, dict) else ""
            if explicit:
                payload["failure_kind"] = explicit[:80]
            else:
                category = classify_failure(result.get("error") if isinstance(result, dict) else None)
                payload["failure_kind"] = "tool_failure" if category == "unknown" else category
        if isinstance(result, dict) and isinstance(result.get("exit_code"), int):
            payload["exit_code"] = int(result["exit_code"])
        if name == "browser_action_batch":
            actions = arguments.get("actions") if isinstance(arguments, dict) else []
            payload.update({
                "action_types": [str(item.get("action") or "") for item in actions
                                if isinstance(item, dict)],
                "state_changed": bool(isinstance(result, dict) and result.get("state_changed")),
                "extraction_count": int(bool(isinstance(result, dict) and result.get("extraction"))),
                "verification_passed": bool(
                    isinstance(result, dict)
                    and isinstance(result.get("verification"), dict)
                    and result["verification"].get("passed")
                ),
                "failure_kind": (str(result.get("failure_kind")) if isinstance(result, dict)
                                  and result.get("failure_kind") else payload.get("failure_kind")),
                "action_steps": int(result.get("action_steps") or 0) if isinstance(result, dict) else 0,
            })
        self.ui.put(("tool_result", payload))

    def _block_empty_input(self) -> None:
        state = RuntimeTaskState.start(self.task_journal, "Desktop task", requires_action=False)
        progress = state.finish("blocked", failure_kind="empty_model_input")
        self.ui.put(("task_progress", progress))

    def recoverable_tasks(self) -> list[dict[str, Any]]:
        """Expose only journal checkpoints; authorization is never restored."""
        recoverable = getattr(self.task_journal, "recoverable", None)
        return list(recoverable()) if callable(recoverable) else []

    def task_evidence(self, task_id: str | None = None) -> dict[str, Any]:
        """Return the journal's bounded structural evidence projection."""
        wanted = task_id or (self._task_state.task_id if self._task_state else None)
        snapshot = getattr(self.task_journal, "evidence_snapshot", None)
        return snapshot(wanted) if callable(snapshot) else {"ok": False, "error": "Task journal is unavailable."}

    def _run_turn(self, text: str, image_paths: list[str], ephemeral: bool = False,
                  allow_tools: bool = True):
        api_key = get_api_key(self.model_provider)
        if not api_key:
            raise RuntimeError("API Key is not configured")
        self._cancelled.clear()
        if not allow_tools:
            return self._run_no_tools_ephemeral_turn(api_key, text)
        verification_status, continuation = self._resolve_human_verification(text)
        if verification_status == "cancelled":
            self._task_authorized_until = 0.0
            self.ui.put(("system", "Browser handoff cancelled. The browser was left unchanged."
                        if continuation and continuation.get("kind") == "browser_no_progress"
                        else "Captcha handoff cancelled. The browser was left unchanged."))
            return
        if verification_status == "expired":
            self._task_authorized_until = 0.0
            if continuation and continuation.get("kind") == "browser_no_progress":
                self.ui.put(("system", "Browser handoff expired. Please start the task again."))
                self._finish_task("blocked", failure_kind="browser_handoff_timeout")
            else:
                self.ui.put(("system", "Captcha handoff expired. Please start the task again."))
            return
        if verification_status == "waiting":
            self.ui.put(("system", "A browser handoff is waiting. Complete the current page selection, then click ‘我已完成选择，继续’."
                        if continuation and continuation.get("kind") == "browser_no_progress"
                        else "A CAPTCHA handoff is waiting. Complete it in the browser, then click ‘我已完成验证，继续’.") )
            return
        if verification_status == "resume" and continuation:
            if self._task_state is not None:
                self._task_state.resumed_by_human()
            transcript = list(continuation["transcript"])
            if continuation.get("kind") == "browser_no_progress":
                if self._browser_session is not None:
                    self._browser_session.resume_after_handoff()
                transcript.append({"role": "user", "content": [{
                    "type": "input_text",
                    "text": ("The user states that they completed the current page selection manually. "
                             "Do not assume success: first call browser_action_batch with a fresh snapshot. "
                             "All previous browser refs and evidence are invalid; do not use desktop coordinates."),
                }]})
                self.ui.put(("system", "✓ Browser handoff acknowledged. Rechecking the page and continuing the task."))
            else:
                transcript.append({"role": "user", "content": [{
                    "type": "input_text",
                    "text": ("The user states that they completed the CAPTCHA manually in the existing browser. "
                             "Do not assume success: first take a fresh page snapshot, then continue the original task. "
                             "Never ask for or reproduce a CAPTCHA solution."),
                }]})
                self.ui.put(("system", "✓ Manual verification acknowledged. Rechecking the page and continuing the task."))
            return self._run_task_loop(api_key, transcript, str(continuation["original_text"]),
                                       bool(continuation["ephemeral"]))
        approval_status, approval = self.approvals.resolve(text)
        if approval_status in {"cancelled", "expired"}:
            self._pending_execution = None
            self._task_authorized_until = 0.0
            self.ui.put(("system", "Pending action cancelled." if approval_status == "cancelled" else "Pending action expired."))
            return
        if approval_status == "pending":
            self.ui.put(("system", f"A confirmation is still pending. Reply exactly: 确认 {approval.token}, or reply 取消."))
            return
        if approval_status == "none":
            # A new user task gets a new, minimal MCP selection.  Approval and
            # CAPTCHA continuations retain their selected server(s).
            self._task_mcp_servers.clear()
            self._browser_recovery_attempts = 0
            self._browser_reobservation_required = False
            self._browser_session = None
            if self._task_state is None:
                self._desktop_target_launches = 0
        if not ephemeral:
            self._maybe_compact_context(api_key)
        self.ui.put(("status", "agent inspecting…"))
        content: list[dict[str, Any]] = [{
            "type": "input_text", "text": text if ephemeral else self.context.build_input(text),
        }]
        for raw_path in image_paths:
            path = Path(raw_path).expanduser()
            if path.is_file():
                mime = mimetypes.guess_type(str(path))[0] or "image/png"
                content.append({"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"})
        transcript: list[dict[str, Any]] = [{"role": "user", "content": content}]
        original_text = "Answer the attached Word question" if ephemeral else self._clean_task_text(text)
        if not ephemeral:
            self._ensure_task_state(original_text)
        if approval_status == "approved" and self._pending_execution:
            call, transcript, original_text = self._pending_execution
            self._pending_execution = None
            self._ensure_task_state(original_text)
            arguments: dict[str, Any] = {}
            if self._is_task_scoped(call.name):
                self._task_authorized_until = time.monotonic() + self.TASK_AUTHORIZATION_SECONDS
                self.ui.put(("system", "✓ Task authorized. Normal steps will continue without further confirmation."))
            try:
                arguments = json.loads(call.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
                result = self._run_desktop_action(call.name, arguments)
            except (json.JSONDecodeError, ValueError) as exc:
                result = {"ok": False, "error": f"Invalid confirmed function call: {exc}"}
            self._task_state.record_tool_result(call.name, result)
            self._publish_tool_result(call.name, arguments if isinstance(arguments, dict) else {}, result)
            transcript.append(function_call_output(call.call_id, json.dumps(result, ensure_ascii=False)))
            transcript = self._append_desktop_observation(transcript, call.name)
        return self._run_task_loop(api_key, transcript, original_text, ephemeral)

    def _run_no_tools_ephemeral_turn(self, api_key: str, text: str) -> None:
        """One isolated text response; function calls are an error, never dispatched."""
        payload = {
            "model": self.model,
            "instructions": SYSTEM_APPEND,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
            "tools": [],
            "stream": False,
        }
        response = self._request(payload, api_key)
        if function_calls(response):
            raise RuntimeError("Office planning responses must not contain tool calls")
        answer = self._extract_text(response)
        if not answer:
            raise RuntimeError("Office planning response contained no text")
        token = getattr(self, "_office_event_token", None)
        self.ui.put(("office_delta", (token, answer)) if token is not None else ("delta", answer))

    def _run_task_loop(self, api_key: str, transcript: list[dict[str, Any]], original_text: str,
                       ephemeral: bool) -> None:
        """Run (or resume) an agent task against its existing tool transcript."""
        if not ephemeral:
            self._ensure_task_state(original_text)
        instructions = SYSTEM_APPEND + (
            "\nYou are the independent DeskOrb Agent Runtime. You may inspect the active window and files below the configured working directory. "
            "When Full access is enabled and the user explicitly asks for a local change, filesystem_write may be used and its result is verified by rereading the file. "
            "When the user asks to open or launch Chrome, Edge, Firefox, QQ, Explorer, Notepad, or Calculator, call application_launch immediately with the matching application name. Never substitute a different application, claim you cannot open it, or tell the user to click its desktop icon. When the user explicitly asks to run a shell command, call shell_run immediately; never ask for confirmation in prose, because the runtime itself handles confirmation. To manage windows, first call desktop_list_windows and then use window_control with the returned short-lived window_id; prefer this over guessing coordinates. Before the first coordinate or keyboard action, call desktop_capture_state and use its snapshot ID. After every desktop action, the runtime automatically supplies a fresh screenshot and snapshot ID so you can inspect the result and continue the whole task. "
            "For browser tasks, use only browser_action_batch. It exposes a bounded semantic contract over the isolated local Playwright MCP backend; raw mcp_playwright_* tools are internal and unavailable. Start with a separate snapshot, then use the returned observation_id and ref for one state action at a time. A state action is automatically followed by a fresh snapshot. If the page does not change, relocate once from the fresh snapshot; do not repeat the same input or fall back to screen coordinates, the address bar, or desktop tools. Use extract followed by verify for structured completion evidence. If the runtime requests human handoff, ask the user to complete the current page selection and then continue only after a fresh snapshot. If mcp_enable_server is available and the request matches a listed integration, call it before attempting that integration; it only enables schemas for one trusted local server and does not perform the user's action. A single task authorization covers normal application launch, clicking, typing, hotkeys, scrolling, window focus, and normal browser actions for that task. High-risk steps and every shell command require a fresh confirmation. If a browser snapshot or result shows a CAPTCHA, ‘快速验证身份’, ‘我是人类’, or similar human-verification screen, do not solve, bypass, or repeatedly retry it. The runtime will pause and request a manual handoff. Continue autonomously until the requested outcome is verified, then answer concisely with what you completed."
            "Maintain a compact action ledger from tool results. Do not repeat an identical successful observation or verification command unless a state-changing action occurred; never loop on verification. Once the required postcondition and evidence are satisfied, stop calling tools and return the final answer."
            "If the task or evaluation names required semantic steps, treat them as hard acceptance conditions: map filesystem_write to an actual filesystem_write call and shell_verify to one non-destructive shell_run verification command; do not substitute a file reread for shell verification."
            "In Full access, execute requested actions automatically. Ask for confirmation only before deleting files; the runtime detects common deletion commands inside shell_run. Do not ask for confirmation in prose. "
        )
        for _ in range(self._tool_round_limit(original_text)):
            if self._cancelled.is_set():
                self._task_authorized_until = 0.0
                self._finish_task("failed", failure_kind="cancelled")
                self.ui.put(("system", "stopped."))
                return
            response = self._request({"model": self.model, "instructions": instructions, "input": transcript,
                                      "tools": self._available_schemas(original_text), "parallel_tool_calls": False, "stream": False}, api_key)
            calls = function_calls(response)
            if not calls:
                answer = self._extract_text(response)
                if not answer:
                    raise RuntimeError("Agent response contained neither text nor a function call")
                if not ephemeral:
                    self.context.add_turn(original_text, answer)
                self._task_authorized_until = 0.0
                self.ui.put(("delta", answer))
                if not ephemeral:
                    self.ui.put(("ctx", self.context.usage_percent()))
                    self._finish_task("completed")
                return
            outputs = []
            state_action_seen = False
            for call in calls:
                arguments: dict[str, Any] = {}
                try:
                    arguments = json.loads(call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                    self.ui.put(("tool", (self._tool_label(call.name), arguments)))
                    if call.name == "browser_action_batch":
                        batch_actions = arguments.get("actions") or []
                        state_actions = [
                            item for item in batch_actions
                            if isinstance(item, dict) and str(item.get("action") or "") in STATE_CHANGING_ACTIONS
                        ]
                        if state_action_seen or len(state_actions) > 1:
                            result = {
                                "ok": False,
                                "failure_kind": "multiple_browser_state_actions",
                                "error": ("Only one browser state-changing action is allowed per model response. "
                                          "Retry immediately with a fresh snapshot in its own batch, then "
                                          "perform exactly one final state action in a later batch."),
                                "requires_reobservation": True,
                            }
                            self._task_state.record_tool_result(call.name, result) if self._task_state else None
                            self._publish_tool_result(call.name, arguments, result)
                            outputs.append(function_call_output(call.call_id, json.dumps(result, ensure_ascii=False)))
                            continue
                        state_action_seen = bool(state_actions)
                    if self._officecli_auto_approval(call.name, arguments):
                        result = self._run_desktop_action(call.name, arguments)
                    else:
                        high_risk = self._high_risk_call(call.name, arguments)
                        decision = self.policy.decide(
                            self._policy_name(call.name, arguments),
                            execution_requested=self._execution_requested(original_text),
                            full_access=self.full_access,
                            task_authorized=self._task_authorized(),
                            high_risk=high_risk,
                        )
                        if decision.kind.value == "deny":
                            result = {"ok": False, "error": decision.reason}
                        elif decision.kind.value == "confirm":
                            summary = self._summary(call.name, arguments)
                            if self._is_task_scoped(call.name, arguments) and not high_risk and not self._task_authorized():
                                summary = "Authorize task: " + original_text[:180]
                            request = self.request_approval(call.name, arguments, decision.risk, summary)
                            if self._task_state is not None:
                                self._task_state.record_confirmation()
                            self._pending_execution = (call, continue_input(transcript, response, []), original_text)
                            return
                        else:
                            result = self._run_desktop_action(call.name, arguments)
                except (json.JSONDecodeError, ValueError) as exc:
                    result = {"ok": False, "error": f"Invalid function call: {exc}"}
                if not ephemeral and self._task_state is not None:
                    self._task_state.record_tool_result(call.name, result)
                    self._publish_tool_result(call.name, arguments, result)
                outputs.append(function_call_output(call.call_id, json.dumps(result, ensure_ascii=False)))
                browser_handoff = self._browser_handoff_reason(call.name, result)
                if browser_handoff:
                    if not ephemeral and self._task_state is not None:
                        self._task_state.waiting_for_human(browser_handoff)
                    continuation = continue_input(transcript, response, outputs)
                    self._pause_for_human_handoff(
                        continuation, original_text, ephemeral, browser_handoff, arguments,
                    )
                    return
                captcha_marker = self._captcha_marker(call.name, result)
                if captcha_marker:
                    if not ephemeral and self._task_state is not None:
                        self._task_state.waiting_for_human(captcha_marker)
                    continuation = continue_input(transcript, response, outputs)
                    self._pause_for_human_verification(continuation, original_text, ephemeral,
                                                       captcha_marker, arguments)
                    return
            transcript = continue_input(transcript, response, outputs)
            if calls:
                transcript = self._append_desktop_observation(transcript, calls[-1].name)
        self._task_authorized_until = 0.0
        self._finish_task("failed", failure_kind="tool_round_limit")
        raise RuntimeError("Agent exceeded the tool round limit")

    def _tool_round_limit(self, task_text: str) -> int:
        """Give OfficeCLI document tasks enough turns for batch edits and validation."""
        if "officecli" in self._mcp_servers_for_task(task_text):
            return max(self.MAX_TOOL_ROUNDS, OFFICECLI_MAX_TOOL_ROUNDS)
        return self.MAX_TOOL_ROUNDS

    def _resolve_human_verification(self, text: str) -> tuple[str, dict[str, Any] | None]:
        pending = self._pending_human_verification
        if not pending:
            return "none", None
        if time.monotonic() > float(pending["expires_at"]):
            self._pending_human_verification = None
            self._cancel_human_handoff_timer()
            return "expired", pending
        normalized = str(text or "").strip().lower()
        if normalized in {self.HUMAN_VERIFICATION_CANCEL, "取消", "cancel"}:
            self._pending_human_verification = None
            self._cancel_human_handoff_timer()
            return "cancelled", pending
        if normalized in {
            self.HUMAN_VERIFICATION_CONTINUE, "我已完成验证", "验证完成", "已完成验证",
            "我已完成选择，继续",
            "captcha complete", "verification complete",
        }:
            self._pending_human_verification = None
            self._cancel_human_handoff_timer()
            return "resume", pending
        return "waiting", pending

    def _pause_for_human_verification(self, transcript: list[dict[str, Any]], original_text: str,
                                      ephemeral: bool, marker: str, arguments: dict[str, Any]) -> None:
        self._cancel_human_handoff_timer()
        self._pending_human_verification = {
            "transcript": transcript,
            "original_text": original_text,
            "ephemeral": ephemeral,
            "kind": "captcha",
            "expires_at": time.monotonic() + self.HUMAN_VERIFICATION_TIMEOUT_SECONDS,
        }
        url = str(arguments.get("url") or arguments.get("target") or "当前浏览器页面")[:240]
        self.ui.put(("human_verification", {
            "marker": marker,
            "page": url,
            "timeout_seconds": self.HUMAN_VERIFICATION_TIMEOUT_SECONDS,
        }))

    def _pause_for_human_handoff(self, transcript: list[dict[str, Any]], original_text: str,
                                 ephemeral: bool, reason: str, arguments: dict[str, Any]) -> None:
        """Pause a browser task for a bounded, non-CAPTCHA manual handoff."""
        timeout = self.BROWSER_HANDOFF_TIMEOUT_SECONDS
        self._cancel_human_handoff_timer()
        pending = {
            "transcript": transcript,
            "original_text": original_text,
            "ephemeral": ephemeral,
            "kind": "browser_no_progress",
            "reason": reason,
            "expires_at": time.monotonic() + timeout,
        }
        self._pending_human_verification = pending
        page = str(arguments.get("url") or arguments.get("target") or "当前浏览器页面")[:240]
        self.ui.put(("human_handoff", {
            "reason": reason,
            "page": page,
            "timeout_seconds": timeout,
        }))
        timer = threading.Timer(timeout, self._expire_browser_handoff, args=(pending,))
        timer.daemon = True
        self._human_handoff_timer = timer
        timer.start()

    def _expire_browser_handoff(self, pending: dict[str, Any]) -> None:
        if self._pending_human_verification is not pending:
            return
        self._pending_human_verification = None
        self._human_handoff_timer = None
        self._task_authorized_until = 0.0
        self.ui.put(("human_handoff_timeout", {"reason": "browser_no_progress", "terminal": "blocked"}))
        self._finish_task("blocked", failure_kind="browser_handoff_timeout")

    def _cancel_human_handoff_timer(self) -> None:
        timer = self._human_handoff_timer
        self._human_handoff_timer = None
        if timer is not None:
            timer.cancel()

    @staticmethod
    def _browser_handoff_reason(tool_name: str, result: dict[str, Any]) -> str | None:
        if tool_name != "browser_action_batch" or not isinstance(result, dict):
            return None
        if result.get("handoff_required"):
            return str(result.get("failure_kind") or "browser_no_progress")
        return None

    def _captcha_marker(self, tool_name: str, result: dict[str, Any]) -> str | None:
        """Return a detected CAPTCHA marker only from a successful MCP browser result."""
        is_semantic_browser = tool_name == "browser_action_batch"
        if not ((self.mcp and self.mcp.owns(tool_name)) or is_semantic_browser) \
                or not isinstance(result, dict) or not result.get("ok"):
            return None
        try:
            evidence = json.dumps(result.get("content", result), ensure_ascii=False).lower()
        except (TypeError, ValueError):
            evidence = str(result.get("content", "")).lower()
        return next((marker for marker in self.CAPTCHA_MARKERS if marker.lower() in evidence), None)

    def _task_authorized(self) -> bool:
        return self.full_access and time.monotonic() < self._task_authorized_until

    @staticmethod
    def _clean_task_text(text: str) -> str:
        """Remove overlay-generated attachment notes from user-facing task summaries/history."""
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"^(?:\[Attached:[^\n]*\]\s*)+", "", cleaned, flags=re.IGNORECASE)
        return " ".join(cleaned.split()) or "Desktop task"

    def _officecli_auto_approval(self, name: str, arguments: dict[str, Any]) -> bool:
        """Fast-path known non-delete OfficeCLI operations without policy work."""
        if not (OFFICECLI_AUTO_APPROVE and self.full_access and self.mcp):
            return False
        checker = getattr(self.mcp, "is_auto_approvable", None)
        return bool(checker and checker(name, arguments))

    def _high_risk_call(self, name: str, arguments: dict[str, Any]) -> bool:
        if name == "shell_run":
            return self._shell_deletes_file(arguments.get("command"))
        if name == "filesystem_delete":
            return True
        if name == "window_control" and str(arguments.get("action") or "").lower() == "close":
            return True
        if self.mcp and self.mcp.owns(name):
            server_name = getattr(self.mcp, "server_name", lambda _name: None)(name)
            if server_name == "officecli" and self._officecli_deletes_file(arguments.get("command")):
                return True
            read_only = getattr(self.mcp, "is_read_only_call", None)
            if callable(read_only) and read_only(name, arguments):
                return False
        risk_level = str(arguments.get("_deskorb_risk_level") or arguments.get("risk_level") or "").lower()
        return risk_level == "high" and (name in self.DESKTOP_ACTION_TOOLS or
                                          bool(self.mcp and self.mcp.owns(name)))

    @staticmethod
    def _shell_deletes_file(command: Any) -> bool:
        """Detect common file-deletion commands before allowing shell execution."""
        text = str(command or "").strip()
        if not text:
            return False
        patterns = (
            r"(?i)(?:^|[;&|]\s*|&\s*)(?:remove-item|ri|rm|del|erase|rmdir|rd)\b",
            r"(?i)\b(?:cmd(?:\.exe)?\s+/c\s+)(?:del|erase|rmdir|rd)\b",
            r"(?i)\b(?:file|directory)\.delete\s*\(",
            r"(?i)\[(?:system\.)?io\.(?:file|directory)\]::delete\s*\(",
            r"(?i)\b(?:os\.(?:remove|unlink)|shutil\.rmtree)\s*\(",
            r"(?i)\bgit\s+clean\b",
        )
        return any(re.search(pattern, text) for pattern in patterns)

    @staticmethod
    def _officecli_deletes_file(command: Any) -> bool:
        """Recognize explicit OfficeCLI file-delete verbs without treating remove as content editing."""
        if isinstance(command, (list, tuple)):
            verb = str(command[0]).strip() if command else ""
        else:
            match = re.match(r"\s*(?:\"([^\"]+)\"|'([^']+)'|(\S+))", str(command or ""))
            verb = next((value for value in match.groups() if value is not None), "") if match else ""
        return verb.lower().removesuffix(".exe") in {"delete", "delete-file", "remove-file"}

    def _available_schemas(self, task_text: str) -> list[dict[str, Any]]:
        schemas = self.tools.schemas()
        browser_task = self._browser_task_requested(task_text)
        if browser_task:
            # A browser task must not give the model a second path through
            # desktop coordinates, address-bar typing, or generic filesystem
            # tools. The isolated browser backend owns startup and navigation.
            schemas = []
            # Keep a fail-closed semantic entry available even if the local MCP
            # installation is missing; the dispatcher then returns a bounded
            # backend-unavailable result instead of allowing coordinate fallback.
            schemas.append(self._browser_action_batch_schema())
        if self.mcp:
            servers = set(self._mcp_servers_for_task(task_text)) | self._task_mcp_servers
            if "playwright" in servers:
                # Discover the trusted raw backend, but never put its
                # overlapping Playwright functions in the model prompt.  The
                # semantic runtime below is the only browser entry point.
                self.mcp.schemas(("playwright",))
                schemas = [item for item in schemas if item.get("name") != "browser_action_batch"]
                schemas.append(self._browser_action_batch_schema())
            other_servers = servers - {"playwright"}
            if other_servers:
                schemas.extend(self.mcp.schemas(other_servers))
            discovery = self._mcp_discovery_schema()
            if discovery:
                schemas.append(discovery)
        return schemas

    @classmethod
    def _browser_task_requested(cls, text: str) -> bool:
        lowered = str(text or "").lower()
        return any(marker in lowered for marker in cls.BROWSER_TASK_MARKERS)

    @staticmethod
    def _browser_action_batch_schema() -> dict[str, Any]:
        common = {
            "observation_id": {"type": "string", "description": "Latest opaque observation token."},
            "ref": {"type": "string", "description": "Current accessibility ref from that observation."},
        }
        action_variants = [
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["navigate"]},
                    "arguments": {"type": "object", "properties": {
                        "url": {"type": "string"},
                    }, "required": ["url"], "additionalProperties": False},
                }, "required": ["action", "arguments"], "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["snapshot"]},
                    "arguments": {"type": "object", "additionalProperties": False},
                }, "required": ["action", "arguments"], "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["fill_ref"]},
                    "arguments": {"type": "object", "properties": {
                        **common, "value": {"type": "string"}, "text": {"type": "string"},
                    }, "required": ["ref", "observation_id", "value"], "additionalProperties": True},
                }, "required": ["action", "arguments"], "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["click_ref"]},
                    "arguments": {"type": "object", "properties": {
                        **common,
                    }, "required": ["ref", "observation_id"], "additionalProperties": True},
                }, "required": ["action", "arguments"], "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["select_ref"]},
                    "arguments": {"type": "object", "properties": {
                        **common, "values": {"type": "array", "items": {"type": "string"}},
                    }, "required": ["ref", "observation_id", "values"], "additionalProperties": True},
                }, "required": ["action", "arguments"], "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["wait"]},
                    "arguments": {"type": "object", "properties": {
                        "seconds": {"type": "number", "minimum": 0, "maximum": 30},
                        "time": {"type": "number", "minimum": 0, "maximum": 30},
                        "ms": {"type": "number", "minimum": 0, "maximum": 30000},
                        "duration_ms": {"type": "number", "minimum": 0, "maximum": 30000},
                    }, "additionalProperties": True},
                }, "required": ["action", "arguments"], "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["switch_tab"]},
                    "arguments": {"type": "object", "properties": {
                        **common, "index": {"type": "integer", "minimum": 0},
                    }, "required": ["observation_id", "index"], "additionalProperties": True},
                }, "required": ["action", "arguments"], "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["extract"]},
                    "arguments": {"type": "object", "properties": {
                        **common,
                        "fields": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "selectors": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    }, "required": ["ref", "observation_id", "fields"], "additionalProperties": True},
                }, "required": ["action", "arguments"], "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["verify"]},
                    "arguments": {"type": "object", "properties": {
                        "required_fields": {"type": "array", "items": {"type": "string"}},
                        "contains": {"type": ["string", "array"], "items": {"type": "string"}},
                        "expected": {}, "price_min": {"type": "number"}, "price_max": {"type": "number"},
                    }, "required": ["required_fields"], "additionalProperties": True},
                }, "required": ["action", "arguments"], "additionalProperties": False,
            },
        ]
        return {
            "type": "function",
            "name": "browser_action_batch",
            "strict": False,
                                "description": (
                "Perform one bounded semantic browser batch through the isolated local browser. "
                "Use a separate snapshot call first and bind click_ref, fill_ref, select_ref, "
                "and extract to its current observation_id. A batch may contain at most one "
                "state-changing action, and that action must be last; never put a state action "
                "before another state action such as wait. After it, the runtime "
                "automatically observes the page. Use extract and then verify for completion "
                "evidence; never use screen coordinates or an address-bar fallback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"oneOf": action_variants},
                    },
                },
                "required": ["actions"],
                "additionalProperties": False,
            },
        }

    def _mcp_servers_for_task(self, text: str) -> tuple[str, ...]:
        """Route a task to its minimum MCP set before any process is spawned."""
        if not self.mcp:
            return ()
        lowered = str(text or "").lower()
        selected: list[str] = []
        browser_markers = self.BROWSER_TASK_MARKERS
        powertoys_markers = (
            "powertoys", "保持唤醒", "不休眠", "置顶", "always on top", "awake", "fancyzones", "键盘管理器",
        )
        available = set(getattr(self.mcp, "available_servers", ()))
        if "playwright" in available and any(marker in lowered for marker in browser_markers):
            selected.append("playwright")
        if "powertoys" in available and any(marker in lowered for marker in powertoys_markers):
            selected.append("powertoys")
        # Custom servers are opt-in per task: mentioning the configured server
        # name makes it available without booting every configured connector.
        for server in sorted(available - {"playwright", "powertoys"}):
            if server.lower() in lowered:
                selected.append(server)
        catalog = getattr(self.mcp, "server_catalog", lambda: [])()
        for item in catalog:
            if not isinstance(item, dict):
                continue
            server = str(item.get("name") or "")
            if server not in available or server in {"playwright", "powertoys"}:
                continue
            keywords = item.get("keywords") or []
            if any(str(keyword).strip().lower() in lowered for keyword in keywords if str(keyword).strip()):
                selected.append(server)
        return tuple(selected)

    def _mcp_discovery_schema(self) -> dict[str, Any] | None:
        if not self.mcp:
            return None
        catalog = getattr(self.mcp, "server_catalog", lambda: [])()
        choices: list[dict[str, str]] = []
        for item in catalog:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            description = str(item.get("description") or "").strip()
            keywords = [str(keyword).strip() for keyword in item.get("keywords") or [] if str(keyword).strip()]
            if name and name not in {"playwright", "powertoys"} and (description or keywords):
                hint = description or ", ".join(keywords)
                choices.append({"name": name, "hint": hint[:240]})
        if not choices:
            return None
        lines = "; ".join(f"{item['name']}: {item['hint']}" for item in choices)
        return {
            "type": "function", "name": "mcp_enable_server", "strict": True,
            "description": "Select one configured local MCP integration that matches the user request. "
                           "This only loads its tools; it does not perform an external action. Available: " + lines,
            "parameters": {"type": "object", "properties": {
                "server_name": {"type": "string", "enum": [item["name"] for item in choices]},
                "reason": {"type": "string"},
            }, "required": ["server_name", "reason"], "additionalProperties": False},
        }

    def _browser_mcp_requested(self, text: str) -> bool:
        """Compatibility helper for callers that only need a yes/no answer."""
        return bool(self._mcp_servers_for_task(text))

    def _policy_name(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if name == "mcp_enable_server":
            return name
        if self.mcp and self.mcp.owns(name):
            if self._high_risk_call(name, arguments or {}):
                return "filesystem_delete"
            classifier = getattr(self.mcp, "is_read_only_call", None)
            read_only = classifier(name, arguments or {}) if classifier else not self.mcp.is_action(name)
            if read_only:
                return "mcp_read_only"
            return "application_launch" if self.mcp.is_action(name) else "desktop_capture_state"
        return name

    def _is_task_scoped(self, name: str, arguments: dict[str, Any] | None = None) -> bool:
        return name in self.policy.TASK_SCOPED_TOOLS or bool(
            self.mcp and self.mcp.owns(name) and self.mcp.is_action(name)
            and not (getattr(self.mcp, "is_read_only_call", lambda _name, _arguments: False)(
                name, arguments or {})))

    def _append_desktop_observation(self, transcript: list[dict[str, Any]], tool_name: str):
        """Give the model a fresh visual/state observation after every desktop step."""
        if tool_name not in self.DESKTOP_ACTION_TOOLS | {"desktop_capture_state"}:
            return transcript
        if tool_name == "application_launch":
            time.sleep(0.8)
        elif tool_name != "desktop_capture_state":
            time.sleep(0.25)
        if tool_name == "desktop_capture_state" and self.desktop.snapshot:
            snapshot = self.desktop.snapshot
            state = {
                "ok": True,
                "snapshot_id": snapshot.snapshot_id,
                "cursor": {"x": snapshot.cursor_x, "y": snapshot.cursor_y},
                "active_window": snapshot.active_title,
                "screen_digest": snapshot.screen_digest,
            }
        else:
            state = self.desktop.capture_state()
        image_url = self.desktop.capture_image_data_url()
        content: list[dict[str, Any]] = [{
            "type": "input_text",
            "text": ("Fresh desktop observation after " + tool_name + ". Use this as tool evidence, "
                     "not as a new user request. For the next desktop action use this snapshot_id: "
                     + json.dumps(state, ensure_ascii=False, separators=(",", ":"))),
        }]
        if image_url:
            content.append({"type": "input_image", "image_url": image_url})
        return [*transcript, {"role": "user", "content": content}]

    def _maybe_compact_context(self, api_key: str):
        if not self.context.compaction_candidate():
            return
        try:
            self._compact_context(api_key, force=False)
            self.ui.put(("ctx", self.context.usage_percent()))
        except Exception:
            # A failed background summary must never block the user's next turn.
            return

    def _compact_context(self, api_key: str, force: bool) -> dict[str, int] | None:
        candidate = self.context.compaction_candidate(force=force)
        if candidate is None:
            return None
        source = {
            "previous_summary": candidate.previous_summary or None,
            "dialogue_to_compact": [
                {"role": message.role, "content": message.text}
                for message in candidate.messages
            ],
        }
        prompt = (
            "Summarize this older conversation for a future assistant. Preserve user goals, "
            "decisions, constraints, completed work, unresolved tasks, exact names/paths, and "
            "important results. Omit transient wording and obsolete detail. Never invent facts. "
            "Return only a compact structured Markdown memory, not a reply to the user.\n\n"
            + json.dumps(source, ensure_ascii=False, separators=(",", ":"))
        )
        response = self._request({
            "model": self.model,
            "instructions": "You are a precise conversation-memory compactor.",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            "stream": False,
        }, api_key)
        summary = self._extract_text(response)
        if not summary:
            raise RuntimeError("Agent context compaction returned no summary")
        if not self.context.apply_compaction(candidate, summary):
            raise RuntimeError("Agent context changed while compaction was running")
        return {"pre_tokens": candidate.pre_tokens, "post_tokens": self.context.estimated_tokens()}

    def _request(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        body = self.adapter.prepare_request(payload)
        request = urllib.request.Request(self.adapter.endpoint, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "deskorb-agent/0.2"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": self.api_proxy_url, "https": self.api_proxy_url})) if self.api_proxy_url else None
        timeout = min(API_TIMEOUT, self.REQUEST_TIMEOUT)
        transient_error: BaseException | None = None
        for attempt in range(API_REQUEST_RETRIES + 1):
            try:
                response = opener.open(request, timeout=timeout) if opener else urllib.request.urlopen(request, timeout=timeout)
                with self._response_lock:
                    self._active_response = response
                try:
                    result = json.loads(response.read(4 * 1024 * 1024).decode("utf-8", "replace"))
                finally:
                    response.close()
                    with self._response_lock:
                        self._active_response = None
                if isinstance(result, dict) and result.get("error"):
                    raise RuntimeError(str(result["error"]))
                return self.adapter.normalize_response(result)
            except urllib.error.HTTPError as exc:
                detail = exc.read(64 * 1024).decode("utf-8", "replace")[:500]
                if exc.code in self.TRANSIENT_HTTP_STATUS and attempt < API_REQUEST_RETRIES:
                    # Upstream gateways commonly use 429/5xx for short overloads. Do
                    # not retry other 4xx responses: those are request/auth/config bugs.
                    retry_after = 0.0
                    try:
                        retry_after = float(exc.headers.get("Retry-After", "0")) if exc.headers else 0.0
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    time.sleep(max(0.5 * (attempt + 1), min(10.0, retry_after)))
                    continue
                if exc.code == 401 and "/api/coding/v3" in self.api_base_url.lower():
                    detail += (
                        " Coding Plan requires a valid Ark Coding Plan API Key and a Coding Plan model ID; "
                        "check the api-key, url, and model_name entries in volcengine.env."
                    )
                raise RuntimeError(f"API HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                transient_error = exc
                if attempt < API_REQUEST_RETRIES:
                    time.sleep(0.5 * (attempt + 1))
                    continue
        detail = getattr(transient_error, "reason", transient_error)
        raise RuntimeError(f"API network connection failed after {API_REQUEST_RETRIES + 1} attempt(s): {str(detail)[:300]}") from transient_error

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        return "".join(str(part.get("text") or "") for item in response.get("output") or []
                       if isinstance(item, dict) for part in item.get("content") or []
                       if isinstance(part, dict) and part.get("type") in {"output_text", "text"})

    def _tool_label(self, name: str) -> str:
        if name == "mcp_enable_server":
            return "Select MCP integration"
        if name == "browser_action_batch":
            return "Browser action"
        if name.startswith("mcp_"):
            server_name = getattr(self.mcp, "server_name", lambda _name: None) if self.mcp else None
            if server_name and server_name(name) == "officecli":
                return "MCP Office tool"
            return "MCP browser tool"
        return {"desktop_get_active_window": "Active window", "filesystem_list": "List files",
                "filesystem_read_text": "Read file", "filesystem_search_text": "Search files"}.get(name, name)

    def _is_browser_mcp_tool(self, name: str) -> bool:
        if not self.mcp or not self.mcp.owns(name):
            return False
        server_name = getattr(self.mcp, "server_name", lambda _name: None)(name)
        return server_name == "playwright" or str(name).startswith("mcp_playwright_")

    @staticmethod
    def _is_browser_observation_tool(name: str) -> bool:
        lowered = str(name).lower()
        return any(marker in lowered for marker in ("snapshot", "observe", "content"))

    @staticmethod
    def _is_browser_connection_failure(result: dict[str, Any]) -> bool:
        if not isinstance(result, dict) or result.get("ok"):
            return False
        text = str(result.get("error") or result.get("message") or "").lower()
        return any(marker in text for marker in (
            "target closed", "browser closed", "page closed", "context closed",
            "connection closed", "disconnected", "broken pipe", "transport",
        ))

    def _reconnect_browser_mcp(self) -> None:
        close = getattr(self.mcp, "close", None) if self.mcp else None
        if callable(close):
            try:
                close()
            except Exception:
                # The failed tool result remains the source of truth.  A
                # subsequent fresh snapshot is still required before action.
                pass

    def _run_local_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "mcp_enable_server":
            server = str(arguments.get("server_name") or "").strip()
            available = set(getattr(self.mcp, "available_servers", ())) if self.mcp else set()
            if server not in available:
                return {"ok": False, "error": "Unknown configured MCP server."}
            self._task_mcp_servers.add(server)
            return {"ok": True, "server": server,
                    "message": "Integration selected. Its tool schemas are available on the next step."}
        if name == "browser_action_batch":
            return self._run_browser_action_batch(arguments)
        if self.mcp and self.mcp.owns(name):
            is_browser = self._is_browser_mcp_tool(name)
            if is_browser and self._browser_reobservation_required \
                    and not self._is_browser_observation_tool(name):
                return {"ok": False,
                        "error": "Fresh browser observation is required before retrying this action.",
                        "requires_reobservation": True}
            result = self.mcp.call(name, arguments)
            if is_browser and self._is_browser_connection_failure(result):
                self._browser_reobservation_required = True
                if self._browser_recovery_attempts < 1:
                    self._browser_recovery_attempts += 1
                    self._reconnect_browser_mcp()
                return {**result, "requires_reobservation": True,
                        "recovery_attempts": self._browser_recovery_attempts}
            if is_browser and self._is_browser_observation_tool(name) and isinstance(result, dict) and result.get("ok"):
                self._browser_reobservation_required = False
            return result
        if name == "filesystem_write":
            return self.tools.write_text(arguments)
        if name == "application_launch":
            target = self.desktop.target_window
            application = str(arguments.get("application") or "").strip().lower()
            if target and application == "notepad":
                process_name = Path(str(window_process_name(target) or "")).name.casefold()
                if process_name not in {"notepad.exe", "notepad"}:
                    return {"ok": False,
                            "error": "Configured desktop target is not an available Notepad window."}
                if self._desktop_target_launches:
                    return {"ok": True, "application": "notepad", "pid": None,
                            "window_handle": target, "reused_target_window": True,
                            "already_open": True}
                focused = self.desktop.focus_target(target)
                if not focused.get("ok"):
                    return {"ok": False, "error": str(focused.get("error") or "Target Notepad could not be focused.")}
                self._desktop_target_launches += 1
                return {"ok": True, "application": "notepad", "pid": None,
                        "window_handle": target, "reused_target_window": True}
            return self.tools.launch_application(arguments)
        if name == "shell_run":
            try:
                return self.tools.run_shell(arguments)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "Command timed out."}
        if name == "desktop_capture_state":
            return self.desktop.capture_state()
        if name == "desktop_list_windows":
            return self.desktop.list_windows()
        if name == "window_control":
            return self.desktop.control_window(arguments.get("window_id", 0), str(arguments.get("action", "")),
                                               arguments.get("x"), arguments.get("y"),
                                               arguments.get("width"), arguments.get("height"))
        if name == "desktop_clipboard_read_text":
            return self.desktop.clipboard_text()
        if name == "desktop_click":
            return self.desktop.click(str(arguments.get("snapshot_id", "")), arguments.get("x", 0), arguments.get("y", 0),
                                      str(arguments.get("button", "")), arguments.get("count", 1))
        if name == "desktop_type":
            return self.desktop.type_text(str(arguments.get("snapshot_id", "")), str(arguments.get("text", "")))
        if name == "desktop_hotkey":
            return self.desktop.hotkey(str(arguments.get("snapshot_id", "")), list(arguments.get("keys") or []))
        if name == "desktop_scroll":
            return self.desktop.scroll(str(arguments.get("snapshot_id", "")), arguments.get("delta", 0),
                                       str(arguments.get("axis", "vertical")))
        if name == "window_focus":
            return self.desktop.focus_window(str(arguments.get("title", "")))
        if name == "desktop_verify_state":
            return self.desktop.verify_state(str(arguments.get("snapshot_id", "")))
        return self.tools.call(name, arguments)

    def _run_browser_action_batch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run the single model-facing browser contract on the trusted MCP backend."""
        if not self.mcp:
            return {"ok": False, "failure_kind": "browser_backend_unavailable",
                    "error": self._mcp_configuration_error or "The local browser backend is unavailable."}
        isolated_check = getattr(self.mcp, "is_browser_isolated", None)
        if callable(isolated_check) and not isolated_check():
            return {"ok": False, "failure_kind": "browser_not_isolated",
                    "error": "The browser backend must use an isolated Chromium profile."}
        try:
            # Direct callers and focused tests may invoke the dispatcher
            # without first asking for schemas. Discovery remains scoped to
            # Playwright and does not expose raw tools to the model.
            self.mcp.schemas(("playwright",))
        except Exception as exc:
            return {"ok": False, "failure_kind": "browser_backend_unavailable",
                    "error": f"The local browser backend could not be prepared: {exc}"}
        if self._browser_session is None:
            self._browser_session = BrowserExecutionSession(
                PlaywrightMCPBackend(self.mcp),
                max_action_steps=20,
                handoff_timeout_seconds=self.BROWSER_HANDOFF_TIMEOUT_SECONDS,
                on_state_action=self._publish_browser_activity,
            )
        return self._browser_session.execute(arguments.get("actions"))

    def _publish_browser_activity(self, phase: str, action: str) -> None:
        labels = {
            "navigate": "browser_navigate",
            "click_ref": "browser_click",
            "fill_ref": "browser_input",
            "select_ref": "browser_select",
        }
        tool = labels.get(action)
        if tool not in BROWSER_ACTIVITY_TOOLS:
            return
        if phase == "begin":
            self._next_desktop_activity_id += 1
            self._browser_activity_ids[action] = self._next_desktop_activity_id
            activity_id = self._next_desktop_activity_id
        else:
            activity_id = self._browser_activity_ids.pop(action, None)
            if activity_id is None:
                return
        self._publish_activity_event(phase, tool, activity_id)

    def _run_desktop_action(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool and publish a privacy-bounded desktop activity lifecycle.

        Only the seven local desktop actions get UI activity events.  The event
        publisher is best effort so an unavailable indicator can never change the
        tool's execution or result semantics.
        """
        if name not in DESKTOP_ACTIVITY_TOOLS:
            return self._run_local_tool(name, arguments)

        self._next_desktop_activity_id += 1
        activity_id = self._next_desktop_activity_id
        self._publish_desktop_activity("begin", name, activity_id)
        try:
            return self._run_local_tool(name, arguments)
        finally:
            self._publish_desktop_activity("end", name, activity_id)

    def _publish_desktop_activity(self, phase: str, tool: str, activity_id: int) -> None:
        """Publish only the safe fields needed by the local UI indicator."""
        self._publish_activity_event(phase, tool, activity_id)

    def _publish_activity_event(self, phase: str, tool: str, activity_id: int) -> None:
        """Publish the shared bounded activity lifecycle for desktop or browser actions."""
        try:
            event = DesktopActivityEvent(phase, tool, activity_id)
            if phase not in {"begin", "end"} or tool not in ACTIVITY_TOOLS:
                return
            self.ui.put(("desktop_activity", event.to_payload()))
        except Exception:
            # The indicator is advisory and must never block a real desktop action.
            return

    @staticmethod
    def _execution_requested(text: str) -> bool:
        lowered = text.lower()
        return any(word in lowered for word in ("修改", "写入", "创建", "修复", "执行", "运行", "保存", "删除", "帮我",
                                                   "打开", "启动", "开启",
                                                   "点击", "输入", "打字", "按下", "快捷键", "读取剪贴板", "窗口", "最小化", "最大化", "置顶", "关闭",
                                                   "write", "create", "fix", "run", "save", "clipboard", "window", "minimize", "maximize", "close",
                                                   "open", "launch", "start", "click", "type", "press", "hotkey"))

    def _summary(self, name: str, arguments: dict[str, Any]) -> str:
        if self.mcp and self.mcp.owns(name) and hasattr(self.mcp, "command_summary"):
            return self.mcp.command_summary(name, arguments)
        if name == "shell_run":
            prefix = "Delete file command: " if self._shell_deletes_file(arguments.get("command")) else "Run command: "
            return prefix + str(arguments.get("command", ""))[:180]
        if name == "application_launch":
            return "Launch application: " + str(arguments.get("application", ""))[:80]
        if name == "desktop_click":
            return (f"Click {arguments.get('button')} {arguments.get('count', 1)} time(s) at "
                    f"({arguments.get('x')}, {arguments.get('y')})")
        if name == "desktop_type":
            return "Type text: " + str(arguments.get("text", ""))[:120]
        if name == "desktop_hotkey":
            return "Press hotkey: " + "+".join(map(str, arguments.get("keys") or []))
        if name == "desktop_scroll":
            return f"Scroll {arguments.get('delta')} step(s) {arguments.get('axis', 'vertical')}"
        if name == "window_focus":
            return "Focus window: " + str(arguments.get("title", ""))[:120]
        if name == "window_control":
            return f"Window action: {arguments.get('action')} on {arguments.get('window_id')}"
        if name == "desktop_clipboard_read_text":
            return "Read text from clipboard"
        return name + " requested"
