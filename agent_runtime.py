"""Independent, API-backed desktop-agent runtime for the floating overlay."""
from __future__ import annotations

import base64
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

from config import (API_CONTEXT_RECENT_TURNS, API_CONTEXT_TOKEN_BUDGET, API_TIMEOUT,
                    SYSTEM_APPEND, WORKING_DIR)
from agent_policy import ApprovalManager, Risk, ToolPolicy
from desktop_tools import DesktopTools
from conversation_context import ConversationContext
from credential_store import get_api_key
from responses_tool_protocol import continue_input, function_call_output, function_calls
from win32utils import foreground_capture_window, window_bbox, window_title


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
             "description": "Run a PowerShell 7 command in the configured working directory. Always requires user confirmation.",
             "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, "required": ["command", "timeout_seconds"], "additionalProperties": False}},
            {"type": "function", "name": "application_launch", "strict": True,
             "description": "Launch an installed desktop application by its common name. Use this for requests such as opening Chrome, Edge, or QQ; never tell the user to click an icon when this tool can launch it.",
             "parameters": {"type": "object", "properties": {"application": {"type": "string", "enum": ["chrome", "edge", "firefox", "qq", "explorer", "notepad", "calculator"]}}, "required": ["application"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_capture_state", "strict": True,
             "description": "Capture a short-lived desktop state snapshot before a coordinate action.",
             "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}},
            {"type": "function", "name": "desktop_click", "strict": True,
             "description": "Click screen coordinates from a fresh desktop snapshot. Set risk_level=high for purchases, sending, deletion, permission changes, or other consequential effects.",
             "parameters": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right"]}, "risk_level": {"type": "string", "enum": ["normal", "high"]}, "risk_reason": {"type": "string"}}, "required": ["snapshot_id", "x", "y", "button", "risk_level", "risk_reason"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_type", "strict": True,
             "description": "Type Unicode text into the current target. Set risk_level=high for secrets, personal data, messages, forms, or consequential submissions.",
             "parameters": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "text": {"type": "string"}, "risk_level": {"type": "string", "enum": ["normal", "high"]}, "risk_reason": {"type": "string"}}, "required": ["snapshot_id", "text", "risk_level", "risk_reason"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_hotkey", "strict": True,
             "description": "Press an allowlisted keyboard shortcut. Set risk_level=high when it submits, sends, deletes, purchases, or changes permissions.",
             "parameters": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "keys": {"type": "array", "items": {"type": "string"}}, "risk_level": {"type": "string", "enum": ["normal", "high"]}, "risk_reason": {"type": "string"}}, "required": ["snapshot_id", "keys", "risk_level", "risk_reason"], "additionalProperties": False}},
            {"type": "function", "name": "desktop_scroll", "strict": True,
             "description": "Scroll from a fresh snapshot. Positive is up; negative is down. Always requires confirmation.",
             "parameters": {"type": "object", "properties": {"snapshot_id": {"type": "string"}, "delta": {"type": "integer"}}, "required": ["snapshot_id", "delta"], "additionalProperties": False}},
            {"type": "function", "name": "window_focus", "strict": True,
             "description": "Focus a window by its exact title. Always requires confirmation.",
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
        candidates = [configured, shutil.which("pwsh"), shutil.which("pwsh.exe")]
        for candidate in candidates:
            if candidate:
                return candidate
        return None


class AgentRuntime:
    """One local task loop using a Responses-compatible HTTPS provider."""

    MAX_TOOL_ROUNDS = 20
    REQUEST_TIMEOUT = 45
    TASK_AUTHORIZATION_SECONDS = 600
    DESKTOP_ACTION_TOOLS = {"application_launch", "desktop_click", "desktop_type",
                            "desktop_hotkey", "desktop_scroll", "window_focus"}

    def __init__(self, ui_queue, model: str, api_base_url: str, api_proxy_url: str = "",
                 working_dir: str | Path = WORKING_DIR, full_access: bool = True,
                 context_tokens: int = API_CONTEXT_TOKEN_BUDGET,
                 recent_turns: int = API_CONTEXT_RECENT_TURNS):
        self.ui = ui_queue
        self.model = model
        self.api_base_url = api_base_url.rstrip("/")
        self.api_proxy_url = api_proxy_url.rstrip("/")
        self.context = ConversationContext(token_budget=context_tokens, recent_turns=recent_turns)
        self.tools = ControlledTools(working_dir)
        self.policy = ToolPolicy()
        self.approvals = ApprovalManager()
        self.desktop = DesktopTools()
        self.full_access = bool(full_access)
        self._cancelled = threading.Event()
        self._active_response = None
        self._response_lock = threading.Lock()
        self._pending_execution: tuple[Any, list[dict[str, Any]], str] | None = None
        self._task_authorized_until = 0.0

    def configure(self, model: str, api_base_url: str, api_proxy_url: str = ""):
        self.model = model
        self.api_base_url = api_base_url.rstrip("/")
        self.api_proxy_url = api_proxy_url.rstrip("/")
        self.context.clear()
        self.approvals.pending = None
        self._pending_execution = None
        self._task_authorized_until = 0.0

    def reset(self):
        self.context.clear()
        self.approvals.pending = None
        self._pending_execution = None
        self._task_authorized_until = 0.0

    def compact(self, force: bool = True) -> dict[str, int] | None:
        """Summarize older turns while retaining recent dialogue verbatim."""
        api_key = get_api_key()
        if not api_key:
            raise RuntimeError("API Key is not configured")
        return self._compact_context(api_key, force=force)

    def set_permission_mode(self, mode: str):
        self.full_access = str(mode) != "plan"
        if not self.full_access:
            self.approvals.pending = None
            self._pending_execution = None
            self._task_authorized_until = 0.0

    def request_approval(self, tool_name: str, arguments: dict[str, Any], risk: Risk, summary: str):
        """Create a chat-native approval request for a future high-risk tool."""
        request = self.approvals.create(tool_name, arguments, risk, summary)
        self.ui.put(("approval", request.prompt()))
        return request

    def interrupt(self):
        self._cancelled.set()
        self._task_authorized_until = 0.0
        with self._response_lock:
            response = self._active_response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def run_turn(self, text: str, image_paths: list[str]):
        try:
            return self._run_turn(text, image_paths)
        except BaseException:
            self._task_authorized_until = 0.0
            raise

    def run_ephemeral_turn(self, text: str, image_paths: list[str]):
        """Run one request without reading or updating normal conversation context."""
        try:
            return self._run_turn(text, image_paths, ephemeral=True)
        except BaseException:
            self._task_authorized_until = 0.0
            raise

    def _run_turn(self, text: str, image_paths: list[str], ephemeral: bool = False):
        api_key = get_api_key()
        if not api_key:
            raise RuntimeError("API Key is not configured")
        self._cancelled.clear()
        approval_status, approval = self.approvals.resolve(text)
        if approval_status in {"cancelled", "expired"}:
            self._pending_execution = None
            self._task_authorized_until = 0.0
            self.ui.put(("system", "Pending action cancelled." if approval_status == "cancelled" else "Pending action expired."))
            return
        if approval_status == "pending":
            self.ui.put(("system", f"A confirmation is still pending. Reply exactly: 确认 {approval.token}, or reply 取消."))
            return
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
        if approval_status == "approved" and self._pending_execution:
            call, transcript, original_text = self._pending_execution
            self._pending_execution = None
            if call.name in self.policy.TASK_SCOPED_TOOLS:
                self._task_authorized_until = time.monotonic() + self.TASK_AUTHORIZATION_SECONDS
                self.ui.put(("system", "✓ Task authorized. Normal steps will continue without further confirmation."))
            try:
                arguments = json.loads(call.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
                result = self._run_local_tool(call.name, arguments)
            except (json.JSONDecodeError, ValueError) as exc:
                result = {"ok": False, "error": f"Invalid confirmed function call: {exc}"}
            transcript.append(function_call_output(call.call_id, json.dumps(result, ensure_ascii=False)))
            transcript = self._append_desktop_observation(transcript, call.name)
        instructions = SYSTEM_APPEND + (
            "\nYou are the independent DeskOrb Agent Runtime. You may inspect the active window and files below the configured working directory. "
            "When Full access is enabled and the user explicitly asks for a local change, filesystem_write may be used and its result is verified by rereading the file. "
            "When the user asks to open or launch Chrome, Edge, Firefox, QQ, Explorer, Notepad, or Calculator, call application_launch immediately with the matching application name. Never substitute a different application, claim you cannot open it, or tell the user to click its desktop icon. When the user explicitly asks to run a shell command, call shell_run immediately; never ask for confirmation in prose, because the runtime itself handles confirmation. Before the first coordinate or keyboard action, call desktop_capture_state and use its snapshot ID. After every desktop action, the runtime automatically supplies a fresh screenshot and snapshot ID so you can inspect the result and continue the whole task. "
            "A single task authorization covers normal application launch, clicking, typing, hotkeys, scrolling, and window focus for that task. Mark desktop_click, desktop_type, and desktop_hotkey risk_level=high only when the specific step sends or publishes content, purchases or transfers value, exposes secrets or personal data, deletes data, changes permissions/security, or accepts an irreversible prompt; give a concise risk_reason. High-risk steps and every shell command require a fresh confirmation. Use risk_level=normal with a short reason for ordinary navigation and search. Continue autonomously until the requested outcome is verified, then answer concisely with what you completed."
        )
        for _ in range(self.MAX_TOOL_ROUNDS):
            if self._cancelled.is_set():
                self._task_authorized_until = 0.0
                self.ui.put(("system", "stopped."))
                return
            response = self._request({"model": self.model, "instructions": instructions, "input": transcript,
                                      "tools": self.tools.schemas(), "parallel_tool_calls": False, "stream": False}, api_key)
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
                return
            outputs = []
            for call in calls:
                try:
                    arguments = json.loads(call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be an object")
                    self.ui.put(("tool", (self._tool_label(call.name), arguments)))
                    high_risk = self._high_risk_call(call.name, arguments)
                    decision = self.policy.decide(
                        call.name,
                        execution_requested=self._execution_requested(original_text),
                        full_access=self.full_access,
                        task_authorized=self._task_authorized(),
                        high_risk=high_risk,
                    )
                    if decision.kind.value == "deny":
                        result = {"ok": False, "error": decision.reason}
                    elif decision.kind.value == "confirm":
                        summary = self._summary(call.name, arguments)
                        if call.name in self.policy.TASK_SCOPED_TOOLS and not high_risk and not self._task_authorized():
                            summary = "Authorize task: " + original_text[:180]
                        request = self.request_approval(call.name, arguments, decision.risk, summary)
                        self._pending_execution = (call, continue_input(transcript, response, []), original_text)
                        return
                    else:
                        result = self._run_local_tool(call.name, arguments)
                except (json.JSONDecodeError, ValueError) as exc:
                    result = {"ok": False, "error": f"Invalid function call: {exc}"}
                outputs.append(function_call_output(call.call_id, json.dumps(result, ensure_ascii=False)))
            transcript = continue_input(transcript, response, outputs)
            if calls:
                transcript = self._append_desktop_observation(transcript, calls[-1].name)
        self._task_authorized_until = 0.0
        raise RuntimeError("Agent exceeded the tool round limit")

    def _task_authorized(self) -> bool:
        return self.full_access and time.monotonic() < self._task_authorized_until

    @staticmethod
    def _clean_task_text(text: str) -> str:
        """Remove overlay-generated attachment notes from user-facing task summaries/history."""
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"^(?:\[Attached:[^\n]*\]\s*)+", "", cleaned, flags=re.IGNORECASE)
        return " ".join(cleaned.split()) or "Desktop task"

    @staticmethod
    def _high_risk_call(name: str, arguments: dict[str, Any]) -> bool:
        if name == "shell_run":
            return True
        return (name in {"desktop_click", "desktop_type", "desktop_hotkey"}
                and str(arguments.get("risk_level", "normal")).lower() == "high")

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
        endpoint = self.api_base_url if self.api_base_url.endswith("/responses") else self.api_base_url + "/responses"
        request = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "deskorb-agent/0.2"})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": self.api_proxy_url, "https": self.api_proxy_url})) if self.api_proxy_url else None
            timeout = min(API_TIMEOUT, self.REQUEST_TIMEOUT)
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
            return result
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"API HTTP {exc.code}: {exc.read(64 * 1024).decode('utf-8', 'replace')[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"API network connection failed: {str(exc.reason)[:300]}") from exc

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        return "".join(str(part.get("text") or "") for item in response.get("output") or []
                       if isinstance(item, dict) for part in item.get("content") or []
                       if isinstance(part, dict) and part.get("type") in {"output_text", "text"})

    @staticmethod
    def _tool_label(name: str) -> str:
        return {"desktop_get_active_window": "Active window", "filesystem_list": "List files",
                "filesystem_read_text": "Read file", "filesystem_search_text": "Search files"}.get(name, name)

    def _run_local_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "filesystem_write":
            return self.tools.write_text(arguments)
        if name == "application_launch":
            return self.tools.launch_application(arguments)
        if name == "shell_run":
            try:
                return self.tools.run_shell(arguments)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "Command timed out."}
        if name == "desktop_capture_state":
            return self.desktop.capture_state()
        if name == "desktop_click":
            return self.desktop.click(str(arguments.get("snapshot_id", "")), arguments.get("x", 0), arguments.get("y", 0), str(arguments.get("button", "")))
        if name == "desktop_type":
            return self.desktop.type_text(str(arguments.get("snapshot_id", "")), str(arguments.get("text", "")))
        if name == "desktop_hotkey":
            return self.desktop.hotkey(str(arguments.get("snapshot_id", "")), list(arguments.get("keys") or []))
        if name == "desktop_scroll":
            return self.desktop.scroll(str(arguments.get("snapshot_id", "")), arguments.get("delta", 0))
        if name == "window_focus":
            return self.desktop.focus_window(str(arguments.get("title", "")))
        if name == "desktop_verify_state":
            return self.desktop.verify_state(str(arguments.get("snapshot_id", "")))
        return self.tools.call(name, arguments)

    @staticmethod
    def _execution_requested(text: str) -> bool:
        lowered = text.lower()
        return any(word in lowered for word in ("修改", "写入", "创建", "修复", "执行", "运行", "保存", "删除", "帮我",
                                                   "打开", "启动", "开启",
                                                   "点击", "输入", "打字", "按下", "快捷键", "write", "create", "fix", "run", "save",
                                                   "open", "launch", "start", "click", "type", "press", "hotkey"))

    @staticmethod
    def _summary(name: str, arguments: dict[str, Any]) -> str:
        if name == "shell_run":
            return "Run command: " + str(arguments.get("command", ""))[:180]
        if name == "application_launch":
            return "Launch application: " + str(arguments.get("application", ""))[:80]
        if name == "desktop_click":
            return f"Click {arguments.get('button')} at ({arguments.get('x')}, {arguments.get('y')})"
        if name == "desktop_type":
            return "Type text: " + str(arguments.get("text", ""))[:120]
        if name == "desktop_hotkey":
            return "Press hotkey: " + "+".join(map(str, arguments.get("keys") or []))
        if name == "desktop_scroll":
            return f"Scroll {arguments.get('delta')} step(s)"
        if name == "window_focus":
            return "Focus window: " + str(arguments.get("title", ""))[:120]
        return name + " requested"

