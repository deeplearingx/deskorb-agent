"""Small, dependency-free stdio MCP client used by DeskOrb local tools.

The bridge intentionally keeps MCP processes local.  It accepts the common
``mcpServers`` JSON shape so users can replace the default Playwright server
with another trusted local browser server without changing DeskOrb code.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable
from typing import Any


class MCPError(RuntimeError):
    """A user-facing error from a local MCP process."""


@dataclass(frozen=True)
class MCPServerSpec:
    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    cwd: str | None = None
    intent_keywords: tuple[str, ...] = ()
    description: str = ""


def resolve_officecli_binary(explicit: str | Path | None = None) -> str | None:
    """Find the optional self-contained OfficeCLI executable without starting it."""
    root = Path(__file__).resolve().parent
    configured = str(explicit or os.environ.get("DESKORB_AGENT_OFFICECLI_BINARY", "")).strip()
    candidates: list[Path] = []
    if configured:
        configured_path = Path(configured).expanduser()
        candidates.append(configured_path if configured_path.is_absolute() else root / configured_path)
    candidates.extend([
        root / "tools" / "officecli" / "officecli.exe",
        root / "OfficeCLI-main" / "build" / "release" / "officecli-win-x64.exe",
    ])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    for command in ("officecli.exe", "officecli"):
        found = shutil.which(command)
        if found:
            return str(Path(found).resolve())
    return None


def load_mcp_servers(config_path: str | Path | None, *, enable_playwright: bool = True,
                     enable_officecli: bool = True,
                     officecli_binary: str | Path | None = None) -> list[MCPServerSpec]:
    """Load trusted local servers from a standard ``mcpServers`` JSON file."""
    value = str(config_path or "").strip()
    if value:
        path = Path(value).expanduser()
        if not path.is_file():
            raise MCPError(f"MCP config file was not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MCPError(f"MCP config could not be read: {exc}") from exc
        servers = data.get("mcpServers", data)
        if not isinstance(servers, dict):
            raise MCPError("MCP config must contain an object named mcpServers.")
        result = [_parse_server(name, spec, path.parent) for name, spec in servers.items()]
        return [spec for spec in result if spec is not None]
    root = Path(__file__).resolve().parent
    interpreter = Path(sys.executable)
    # The overlay is commonly launched by pythonw.exe.  Use python.exe for the
    # stdio server when it is present so pipes behave consistently on Windows.
    console_python = interpreter.with_name("python.exe") if interpreter.name.lower() == "pythonw.exe" else interpreter
    if not console_python.is_file():
        console_python = interpreter
    result: list[MCPServerSpec] = []
    if enable_playwright:
        result.append(MCPServerSpec("playwright", "npx", ("-y", "@playwright/mcp@latest"), {}, None))
    result.append(MCPServerSpec("powertoys", str(console_python), (str(root / "powertoys_mcp.py"),), {}, str(root)))
    if enable_officecli:
        binary = resolve_officecli_binary(officecli_binary)
        if binary:
            result.append(MCPServerSpec(
                name="officecli",
                command=binary,
                args=("mcp",),
                env={
                    "OFFICECLI_SKIP_UPDATE": "1",
                    "OFFICECLI_NO_AUTO_RESIDENT": "1",
                },
                cwd=str(root),
                intent_keywords=(
                    "officecli", "docx", ".docx", "word document",
                    "word 文档", "word文档", "word 文件", "word文件",
                    "创建 word", "创建word", "生成 word", "生成word",
                    "xlsx", ".xlsx", "excel workbook",
                    "excel 文档", "excel文档", "excel 文件", "excel文件", "excelfile",
                    "创建 excel", "创建excel", "生成 excel", "生成excel",
                    "pptx", ".pptx", "powerpoint",
                    "ppt", "ppt 演示文稿", "ppt演示文稿",
                    "创建 ppt", "创建ppt", "生成 ppt", "生成ppt",
                    "powerpoint 演示文稿",
                    "文档生成", "演示文稿", "工作簿",
                ),
                description="Create, read, modify, validate, and render Office files.",
            ))
    return result


def _parse_server(name: Any, value: Any, base: Path) -> MCPServerSpec | None:
    if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
        raise MCPError("Each MCP server requires a name and an object configuration.")
    if value.get("enabled") is False:
        return None
    command = str(value.get("command") or "").strip()
    if not command:
        raise MCPError(f"MCP server {name!r} is missing command.")
    command_path = Path(command)
    if not command_path.is_absolute() and ("/" in command or "\\" in command):
        candidate = base / command_path
        if candidate.is_file():
            command = str(candidate.resolve())
    raw_args = value.get("args", [])
    if not isinstance(raw_args, list) or not all(isinstance(item, (str, int, float)) for item in raw_args):
        raise MCPError(f"MCP server {name!r} args must be an array of strings.")
    raw_env = value.get("env", {})
    if not isinstance(raw_env, dict) or not all(isinstance(key, str) for key in raw_env):
        raise MCPError(f"MCP server {name!r} env must be an object.")
    cwd = str(value.get("cwd") or "").strip() or None
    if cwd and not Path(cwd).is_absolute():
        cwd = str((base / cwd).resolve())
    # ``deskorb`` is optional metadata ignored by standard MCP clients.  It
    # lets DeskOrb route by user intent before it launches a custom process.
    metadata = value.get("deskorb", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise MCPError(f"MCP server {name!r} deskorb metadata must be an object.")
    raw_keywords = metadata.get("keywords", [])
    if not isinstance(raw_keywords, list) or not all(isinstance(item, str) for item in raw_keywords):
        raise MCPError(f"MCP server {name!r} deskorb.keywords must be an array of strings.")
    keywords = tuple(item.strip() for item in raw_keywords if item.strip())
    description = str(metadata.get("description") or "").strip()
    return MCPServerSpec(name.strip(), command, tuple(str(item) for item in raw_args),
                         {key: str(item) for key, item in raw_env.items()}, cwd, keywords, description)


class StdioMCPClient:
    """Serial JSON-RPC client for one local MCP stdio server."""

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, spec: MCPServerSpec, *, timeout_seconds: int = 30):
        self.spec = spec
        self.timeout_seconds = max(5, int(timeout_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._lock = threading.RLock()
        self._next_id = 1

    def start(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                return
            env = os.environ.copy()
            env.update(self.spec.env)
            flags = 0x08000000 if os.name == "nt" else 0
            command = self.spec.command
            if os.name == "nt" and not Path(command).suffix:
                command = shutil.which(command) or shutil.which(command + ".cmd") or command
            try:
                self._process = subprocess.Popen(
                    [command, *self.spec.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
                    cwd=self.spec.cwd, env=env, creationflags=flags,
                )
            except OSError as exc:
                raise MCPError(f"Could not start MCP server {self.spec.name}: {exc}") from exc
            threading.Thread(target=self._read_stdout, daemon=True, name=f"mcp-{self.spec.name}").start()
            self._request("initialize", {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "deskorb-agent", "version": "0.2.0"},
            })
            self._notify("notifications/initialized", {})

    def list_tools(self) -> list[dict[str, Any]]:
        self.start()
        result = self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if not isinstance(tools, list):
            raise MCPError(f"MCP server {self.spec.name} returned an invalid tool list.")
        return [tool for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.start()
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        return result if isinstance(result, dict) else {"content": result}

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            if not process:
                return
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def _read_stdout(self) -> None:
        process = self._process
        if not process or not process.stdout:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self._messages.put(message)

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        process = self._process
        if not process or not process.stdin:
            raise MCPError(f"MCP server {self.spec.name} is not running.")
        request_id = self._next_id
        self._next_id += 1
        try:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                                             "params": params}, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise MCPError(f"MCP server {self.spec.name} stopped while sending {method}.") from exc
        while True:
            try:
                message = self._messages.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                raise MCPError(f"MCP server {self.spec.name} timed out while running {method}.") from exc
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message.get("error") or {}
                raise MCPError(f"MCP {self.spec.name} {method} failed: {error.get('message', error)}")
            return message.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        process = self._process
        if not process or not process.stdin:
            return
        try:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
            process.stdin.flush()
        except OSError:
            return


class MCPToolBridge:
    """Expose trusted MCP tools as Responses-compatible local functions."""

    READ_ONLY_WORDS = ("snapshot", "screenshot", "console", "network", "find", "inspect", "list", "get",
                       "status", "schema", "test", "export", "version")
    BLOCKED_WORDS = ("unsafe",)
    HIGH_RISK_WORDS = ("upload", "drop", "handle_dialog", "apply", "restore")

    def __init__(self, config_path: str | Path | None, *, enable_playwright: bool = True,
                 enable_officecli: bool = True, officecli_binary: str | Path | None = None,
                 timeout_seconds: int = 30):
        self.specs = load_mcp_servers(config_path, enable_playwright=enable_playwright,
                                      enable_officecli=enable_officecli,
                                      officecli_binary=officecli_binary)
        self.timeout_seconds = timeout_seconds
        self.clients = {spec.name: StdioMCPClient(spec, timeout_seconds=timeout_seconds) for spec in self.specs}
        self._tools: dict[str, tuple[str, str, dict[str, Any], bool]] = {}
        self.diagnostics: list[str] = []
        # Discovery starts a process, so it must be per server rather than an
        # all-or-nothing operation.  A browser request should not boot the
        # PowerToys adapter (and vice versa) just to obtain its schemas.
        self._discovered_servers: set[str] = set()

    @property
    def available_servers(self) -> tuple[str, ...]:
        """Configured trusted server names, without starting any process."""
        return tuple(self.clients)

    def schemas(self, server_names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Return schemas for selected servers, starting only those servers.

        ``None`` retains the public bridge's legacy behaviour and discovers all
        configured servers.  Runtime callers should pass the servers relevant
        to the current task.
        """
        selected = self._selected_servers(server_names)
        self._discover(selected)
        schemas: list[dict[str, Any]] = []
        for exposed, (server, original, schema, action) in self._tools.items():
            if server not in selected:
                continue
            parameters = _json_schema_object(schema)
            if action and server != "officecli":
                properties = dict(parameters.get("properties") or {})
                properties["_deskorb_risk_level"] = {"type": "string", "enum": ["normal", "high"]}
                properties["_deskorb_risk_reason"] = {"type": "string"}
                parameters["properties"] = properties
                required = [item for item in parameters.get("required", []) if item in properties]
                parameters["required"] = [*required, "_deskorb_risk_level", "_deskorb_risk_reason"]
            schemas.append({"type": "function", "name": exposed, "strict": False,
                            "description": self._description(server, original, action),
                            "parameters": parameters})
        return schemas

    def server_catalog(self) -> list[dict[str, Any]]:
        """Safe, process-free capability hints for intent routing.

        Commands, arguments, environment variables, and complete MCP schemas
        are deliberately excluded.  Only user-provided intent metadata is
        shown to the model before a server is selected.
        """
        return [
            {"name": spec.name, "description": spec.description[:240],
             "keywords": list(spec.intent_keywords[:64])}
            for spec in self.specs
        ]

    def owns(self, name: str) -> bool:
        return name in self._tools

    def is_action(self, name: str) -> bool:
        item = self._tools.get(name)
        return bool(item and item[3])

    def server_name(self, exposed_name: str) -> str | None:
        item = self._tools.get(exposed_name)
        return item[0] if item else None

    def is_read_only_call(self, exposed_name: str, arguments: dict[str, Any]) -> bool:
        """Classify a call whose risk depends on its OfficeCLI command verb."""
        item = self._tools.get(exposed_name)
        if not item:
            return False
        server, _original, _schema, action = item
        if server != "officecli":
            return not action
        return _officecli_command_verb(arguments.get("command")) in OFFICECLI_READ_ONLY_VERBS

    def is_auto_approvable(self, exposed_name: str, arguments: dict[str, Any]) -> bool:
        """Return whether an OfficeCLI generation/update verb may skip confirmation."""
        item = self._tools.get(exposed_name)
        if not item or item[0] != "officecli":
            return False
        return _officecli_command_verb(arguments.get("command")) in OFFICECLI_AUTO_APPROVE_VERBS

    def call(self, exposed_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        item = self._tools.get(exposed_name)
        if not item:
            return {"ok": False, "error": "Unknown MCP tool."}
        server, original, _schema, _action = item
        clean = {key: value for key, value in arguments.items() if not key.startswith("_deskorb_")}
        try:
            result = self.clients[server].call_tool(original, clean)
        except MCPError as exc:
            message = str(exc)
            if server == "officecli":
                message = self._officecli_error_message(exposed_name, arguments, message)
            return {"ok": False, "error": message}
        content = result.get("content", result)
        rendered = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        cap = 64 * 1024
        if server == "officecli" and result.get("isError"):
            return {"ok": False, "server": server, "tool": original,
                    "content": content if len(rendered) <= cap else rendered[:cap],
                    "truncated": len(rendered) > cap,
                    "error": self._officecli_error_message(exposed_name, arguments, rendered)}
        return {"ok": not bool(result.get("isError")), "server": server, "tool": original,
                "content": content if len(rendered) <= cap else rendered[:cap],
                "truncated": len(rendered) > cap}

    def close(self) -> None:
        for client in self.clients.values():
            client.close()

    def _selected_servers(self, server_names: Iterable[str] | None) -> set[str]:
        if server_names is None:
            return set(self.clients)
        requested = {str(name).strip() for name in server_names if str(name).strip()}
        return set(self.clients).intersection(requested)

    def _discover(self, servers: Iterable[str]) -> None:
        for server in servers:
            if server in self._discovered_servers:
                continue
            client = self.clients.get(server)
            if client is None:
                continue
            # Mark the attempt before talking to the process.  This avoids a
            # failing optional integration imposing its full timeout on every
            # model round; a new AgentRuntime gets a fresh attempt.
            self._discovered_servers.add(server)
            try:
                for tool in client.list_tools():
                    original = str(tool["name"])
                    if any(word in original.lower() for word in self.BLOCKED_WORDS):
                        continue
                    exposed = _exposed_tool_name(server, original, self._tools)
                    self._tools[exposed] = (server, original, dict(tool.get("inputSchema") or {}),
                                            self._is_action(original))
            except MCPError as exc:
                self.diagnostics.append(str(exc))

    def _is_action(self, tool_name: str) -> bool:
        lowered = tool_name.lower()
        return not any(word in lowered for word in self.READ_ONLY_WORDS)

    def is_high_risk(self, name: str, arguments: dict[str, Any] | None = None) -> bool:
        item = self._tools.get(name)
        if item and item[0] == "officecli":
            return not self.is_read_only_call(name, arguments or {})
        return bool(item and any(word in item[1].lower() for word in self.HIGH_RISK_WORDS))

    def command_summary(self, exposed_name: str, arguments: dict[str, Any]) -> str:
        """Return a bounded, user-facing summary for a tool call."""
        command = arguments.get("command")
        if self.server_name(exposed_name) != "officecli":
            return exposed_name + " requested"
        if isinstance(command, list):
            rendered = " ".join(str(item) for item in command)
        elif isinstance(command, str):
            rendered = command.strip()
        else:
            rendered = "<missing command>"
        return ("OfficeCLI file operation: " + rendered)[:240]

    def _officecli_error_message(self, exposed_name: str, arguments: dict[str, Any], message: str) -> str:
        lowered = message.lower()
        if any(marker in lowered for marker in (
                "file_locked", "file locked", "file is locked", "sharing violation",
                "being used by another process", "cannot access the file", "file is in use")):
            return (self.command_summary(exposed_name, arguments)
                    + ". The file appears to be locked. Save and close it in Word or Excel, "
                      "then try again. OfficeCLI reported: " + message[:400])
        return message[:1024]

    @staticmethod
    def _description(server: str, original: str, action: bool) -> str:
        if server == "officecli":
            prefix = "Office file action" if action else "Read-only Office file operation"
            return f"{prefix} from trusted local MCP server {server}: {original}."
        prefix = "Browser/local MCP action" if action else "Read-only browser/local MCP observation"
        suffix = (" Supply _deskorb_risk_level=high only for a consequential action such as submitting, "
                  "sending, purchasing, deleting, uploading private data, or changing permissions." if action else "")
        return f"{prefix} from trusted local MCP server {server}: {original}." + suffix


OFFICECLI_READ_ONLY_VERBS = frozenset({
    "help", "load_skill", "view", "get", "query", "validate", "dump",
})

OFFICECLI_AUTO_APPROVE_VERBS = frozenset({
    "create", "set", "add", "swap", "batch", "merge", "import", "raw-set",
    "add-part", "save", "refresh",
})


def _officecli_command_verb(command: Any) -> str | None:
    if isinstance(command, (list, tuple)):
        tokens = [str(item) for item in command]
    elif isinstance(command, str):
        tokens = _split_command(command)
    else:
        return None
    while tokens and tokens[0].lower() in {"officecli", "officecli.exe"}:
        tokens.pop(0)
    if not tokens:
        return None
    return tokens[0].strip().lower() or None


def _split_command(command: str) -> list[str]:
    """Split only enough to find the first verb, preserving Windows paths."""
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in command.strip():
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
        elif char in {"'", '"'}:
            quote = char
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def _exposed_tool_name(server: str, original: str, existing: dict[str, Any]) -> str:
    base = "mcp_" + re.sub(r"[^A-Za-z0-9_-]+", "_", server + "_" + original).strip("_")
    candidate = base[:60]
    suffix = 2
    while candidate in existing:
        candidate = base[:55] + "_" + str(suffix)
        suffix += 1
    return candidate


def _json_schema_object(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only a permissive root object; provider compatibility beats schema cleverness."""
    if not isinstance(value, dict):
        value = {}
    result = {key: item for key, item in value.items() if key not in {"$schema", "additionalProperties"}}
    result["type"] = "object"
    result["properties"] = dict(value.get("properties") or {})
    result["required"] = [item for item in value.get("required", []) if item in result["properties"]]
    result["additionalProperties"] = False
    return result
