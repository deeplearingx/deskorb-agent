"""A deliberately narrow stdio MCP server for PowerToys DSC settings.

This server is local-only.  It invokes PowerToys.DSC.exe without a shell and
offers two writable modules (Awake and AlwaysOnTop) behind an MCP action tool.
The DeskOrb runtime adds its own user-confirmation gate before that tool runs.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# Reversible productivity settings that are useful in normal desktop tasks.
# Every entry still requires a fresh high-risk confirmation in DeskOrb.  Modules
# that can alter routing, keyboard input, the registry, or environment remain
# intentionally read-only (KeyboardManager, Hosts, EnvironmentVariables,
# RegistryPreview, and App).
WRITE_MODULES = {
    "AdvancedPaste", "AlwaysOnTop", "Awake", "ColorPicker", "CropAndLock",
    "FancyZones", "FindMyMouse", "ImageResizer", "MouseHighlighter", "MouseJump",
    "MousePointerCrosshairs", "Peek", "PowerAccent", "PowerOCR", "PowerRename",
    "ShortcutGuide", "Workspaces", "ZoomIt",
}
MAX_BACKUPS = 16
MAX_PROFILE_CHANGES = 8


class PowerToysError(RuntimeError):
    """An actionable, user-facing PowerToys DSC failure."""


def _json_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PowerToysError(f"{name} must be a JSON object.")
    return value


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _contains(actual[key], value)
                                                for key, value in expected.items())
    return actual == expected


class PowerToysDsc:
    """Safe adapter over the documented PowerToys.DSC.exe interface."""

    def __init__(self) -> None:
        self.executable = self._find_executable()
        self._modules: list[str] | None = None
        self._backups: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _find_executable() -> Path | None:
        override = os.environ.get("DESKORB_AGENT_POWERTOYS_DSC", "").strip()
        candidates = [Path(override)] if override else []
        local = os.environ.get("LOCALAPPDATA", "").strip()
        program_files = os.environ.get("ProgramFiles", "").strip()
        if local:
            candidates.append(Path(local) / "PowerToys" / "PowerToys.DSC.exe")
        if program_files:
            candidates.append(Path(program_files) / "PowerToys" / "PowerToys.DSC.exe")
        return next((path for path in candidates if path.is_file()), None)

    def status(self) -> dict[str, Any]:
        if not self.executable:
            return {"installed": False, "executable": None, "write_modules": sorted(WRITE_MODULES)}
        return {
            "installed": True,
            "executable": str(self.executable),
            "modules": self.modules(),
            "write_modules": sorted(WRITE_MODULES),
        }

    def modules(self) -> list[str]:
        if self._modules is None:
            output = self._run("modules", "--resource", "settings", parse_json=False)
            self._modules = sorted({line.strip() for line in output.splitlines() if line.strip()}, key=str.lower)
        return list(self._modules)

    def get_settings(self, module: str) -> dict[str, Any]:
        return self._run_json("get", "--resource", "settings", "--module", self._module(module))

    def get_schema(self, module: str) -> dict[str, Any]:
        return self._run_json("schema", "--resource", "settings", "--module", self._module(module))

    def test_settings(self, module: str, properties: dict[str, Any]) -> dict[str, Any]:
        name, _before, candidate = self._candidate(module, properties)
        result = self._run_json("test", "--resource", "settings", "--module", name,
                                "--input", json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
        return {"module": name, "candidate": candidate, "result": result}

    def apply_settings(self, module: str, properties: dict[str, Any]) -> dict[str, Any]:
        name, before, candidate = self._candidate(module, properties)
        if name not in WRITE_MODULES:
            raise PowerToysError(f"Writes are intentionally limited to: {', '.join(sorted(WRITE_MODULES))}.")
        # A DSC test is both a dry-run and a validation pass before changing a setting.
        preflight = self._run_json("test", "--resource", "settings", "--module", name,
                                   "--input", json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
        backup_id = self._save_backup(name, before)
        set_result = self._run_json("set", "--resource", "settings", "--module", name,
                                    "--input", json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
        after = self.get_settings(name)
        expected = candidate.get("settings", {}).get("properties", {})
        actual = after.get("settings", {}).get("properties", {}) if isinstance(after, dict) else {}
        verified = _contains(actual, expected)
        if not verified:
            raise PowerToysError(f"PowerToys applied {name}, but read-back verification failed. "
                                f"Use backup {backup_id} to restore the previous settings.")
        return {"module": name, "backup_id": backup_id, "preflight": preflight,
                "set_result": set_result, "settings": after, "verified": True}

    def apply_profile(self, changes: list[Any]) -> dict[str, Any]:
        """Apply a bounded group of independently preflighted setting changes.

        All candidates are tested before the first write. If a later write or
        verification fails, already-applied modules are restored from their
        in-memory backups before returning an error.
        """
        if not isinstance(changes, list) or not changes or len(changes) > MAX_PROFILE_CHANGES:
            raise PowerToysError(f"changes must contain between 1 and {MAX_PROFILE_CHANGES} items.")
        plans: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in changes:
            change = _json_object(item, "each profile change")
            name, before, candidate = self._candidate(str(change.get("module") or ""),
                                                      _json_object(change.get("properties"), "properties"))
            if name not in WRITE_MODULES:
                raise PowerToysError(f"{name} is read-only; allowed write modules: {', '.join(sorted(WRITE_MODULES))}.")
            if name in seen:
                raise PowerToysError(f"Profile contains duplicate module: {name}")
            seen.add(name)
            preflight = self._run_json("test", "--resource", "settings", "--module", name,
                                       "--input", json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
            plans.append({"module": name, "before": before, "candidate": candidate, "preflight": preflight})

        applied: list[dict[str, Any]] = []
        try:
            for plan in plans:
                backup_id = self._save_backup(plan["module"], plan["before"])
                set_result = self._run_json("set", "--resource", "settings", "--module", plan["module"],
                                            "--input", json.dumps(plan["candidate"], ensure_ascii=False,
                                                                  separators=(",", ":")))
                after = self.get_settings(plan["module"])
                applied.append({"module": plan["module"], "backup_id": backup_id,
                                "preflight": plan["preflight"], "set_result": set_result,
                                "settings": after, "verified": False})
                expected = plan["candidate"]["settings"]["properties"]
                actual = after.get("settings", {}).get("properties", {}) if isinstance(after, dict) else {}
                if not _contains(actual, expected):
                    raise PowerToysError(f"Read-back verification failed for {plan['module']}.")
                applied[-1]["verified"] = True
        except PowerToysError as exc:
            restored = self._rollback(applied)
            detail = ", ".join(restored) if restored else "none"
            raise PowerToysError(f"Profile application stopped: {exc} Automatic rollback restored: {detail}.") from exc
        return {"changes": applied, "verified": True}

    def restore_backup(self, backup_id: str) -> dict[str, Any]:
        backup = self._backups.get(str(backup_id or ""))
        if not backup:
            raise PowerToysError("Backup was not found in this MCP session.")
        name = str(backup["module"])
        restored = self._run_json("set", "--resource", "settings", "--module", name,
                                  "--input", json.dumps(backup["settings"], ensure_ascii=False,
                                                        separators=(",", ":")))
        after = self.get_settings(name)
        if after != backup["settings"]:
            raise PowerToysError(f"Restore completed for {name}, but read-back verification failed.")
        return {"module": name, "backup_id": backup_id, "set_result": restored,
                "settings": after, "verified": True}

    def list_backups(self) -> list[dict[str, Any]]:
        return [{"backup_id": backup_id, "module": item["module"], "created_at": item["created_at"]}
                for backup_id, item in self._backups.items()]

    def _candidate(self, module: str, properties: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        name = self._module(module)
        update = _json_object(properties, "properties")
        before = self.get_settings(name)
        settings = _json_object(before.get("settings"), "PowerToys settings")
        current = _json_object(settings.get("properties"), "PowerToys settings.properties")
        candidate = {"settings": dict(settings)}
        candidate["settings"]["properties"] = _deep_merge(current, update)
        return name, before, candidate

    def _module(self, module: str) -> str:
        wanted = str(module or "").strip()
        if not wanted:
            raise PowerToysError("module is required.")
        matches = [name for name in self.modules() if name.lower() == wanted.lower()]
        if not matches:
            raise PowerToysError(f"Unknown PowerToys module: {wanted}")
        return matches[0]

    def _save_backup(self, module: str, settings: dict[str, Any]) -> str:
        while len(self._backups) >= MAX_BACKUPS:
            oldest = next(iter(self._backups))
            del self._backups[oldest]
        backup_id = secrets.token_hex(6).upper()
        self._backups[backup_id] = {"module": module, "settings": settings, "created_at": time.time()}
        return backup_id

    def _rollback(self, applied: list[dict[str, Any]]) -> list[str]:
        restored: list[str] = []
        for item in reversed(applied):
            backup = self._backups.get(item["backup_id"])
            if not backup:
                continue
            try:
                self._run_json("set", "--resource", "settings", "--module", backup["module"],
                               "--input", json.dumps(backup["settings"], ensure_ascii=False, separators=(",", ":")))
                after = self.get_settings(backup["module"])
                if after == backup["settings"]:
                    restored.append(str(backup["module"]))
            except PowerToysError:
                continue
        return restored

    def _run_json(self, *args: str) -> dict[str, Any]:
        value = self._run(*args, parse_json=True)
        return _json_object(value, "PowerToys DSC response")

    def _run(self, *args: str, parse_json: bool) -> Any:
        if not self.executable:
            raise PowerToysError("PowerToys.DSC.exe is not installed. Install PowerToys first.")
        try:
            completed = subprocess.run([str(self.executable), *args], shell=False, capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", timeout=30,
                                       creationflags=CREATE_NO_WINDOW, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PowerToysError(f"PowerToys DSC command failed: {exc}") from exc
        stdout, stderr = completed.stdout.strip(), completed.stderr.strip()
        if completed.returncode:
            raise PowerToysError((stderr or stdout or f"PowerToys DSC exited with {completed.returncode}")[:1000])
        if not parse_json:
            return stdout
        try:
            # PowerToys.DSC.exe test (currently v0.100.x) writes its JSON result
            # followed by a harmless empty JSON array on a second line.  Accept
            # only that exact empty trailer; any other extra output remains an error.
            value, end = json.JSONDecoder().raw_decode(stdout)
            trailer = stdout[end:].strip()
            if trailer not in ("", "[]"):
                raise json.JSONDecodeError("unexpected trailing output", stdout, end)
            return value
        except json.JSONDecodeError as exc:
            raise PowerToysError(f"PowerToys DSC returned invalid JSON: {stdout[:500]}") from exc


class PowerToysMcpServer:
    """Minimal MCP JSON-RPC server; stdout is reserved for protocol messages."""

    def __init__(self) -> None:
        self.dsc = PowerToysDsc()

    def run(self) -> None:
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                response = self._handle(message)
                if response is not None:
                    self._emit(response)
            except Exception as exc:
                request_id = message.get("id") if isinstance(locals().get("message"), dict) else None
                if request_id is not None:
                    self._emit(self._error(request_id, -32603, str(exc)[:1000]))

    def _handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method, request_id = str(message.get("method") or ""), message.get("id")
        if method == "initialize":
            return self._result(request_id, {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "deskorb-powertoys", "version": "0.1.0"},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._result(request_id, {"tools": self._tools()})
        if method == "tools/call":
            params = _json_object(message.get("params") or {}, "tools/call parameters")
            result = self._call(str(params.get("name") or ""), _json_object(params.get("arguments") or {}, "tool arguments"))
            return self._result(request_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": not bool(result.get("ok")),
            })
        if request_id is None:
            return None
        return self._error(request_id, -32601, f"Unknown MCP method: {method}")

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "powertoys_status":
                return {"ok": True, **self.dsc.status()}
            if name == "powertoys_list_modules":
                return {"ok": True, "modules": self.dsc.modules()}
            if name == "powertoys_get_settings":
                return {"ok": True, "settings": self.dsc.get_settings(str(arguments.get("module") or ""))}
            if name == "powertoys_get_schema":
                return {"ok": True, "schema": self.dsc.get_schema(str(arguments.get("module") or ""))}
            if name == "powertoys_test_settings":
                return {"ok": True, **self.dsc.test_settings(str(arguments.get("module") or ""),
                                                               _json_object(arguments.get("properties"), "properties"))}
            if name == "powertoys_apply_settings":
                return {"ok": True, **self.dsc.apply_settings(str(arguments.get("module") or ""),
                                                                _json_object(arguments.get("properties"), "properties"))}
            if name == "powertoys_apply_profile":
                return {"ok": True, **self.dsc.apply_profile(arguments.get("changes"))}
            if name == "powertoys_list_backups":
                return {"ok": True, "backups": self.dsc.list_backups()}
            if name == "powertoys_restore_backup":
                return {"ok": True, **self.dsc.restore_backup(str(arguments.get("backup_id") or ""))}
            raise PowerToysError(f"Unknown PowerToys tool: {name}")
        except PowerToysError as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _tools() -> list[dict[str, Any]]:
        module = {"type": "string", "description": "Exact PowerToys module name."}
        properties = {"type": "object", "description": "Only the settings.properties fields to change."}
        changes = {"type": "array", "minItems": 1, "maxItems": MAX_PROFILE_CHANGES,
                   "items": {"type": "object", "properties": {"module": module, "properties": properties},
                             "required": ["module", "properties"], "additionalProperties": False}}
        return [
            {"name": "powertoys_status", "description": "Detect local PowerToys DSC support and allowed write modules.",
             "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "powertoys_list_modules", "description": "List locally installed PowerToys DSC modules.",
             "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "powertoys_get_settings", "description": "Read one PowerToys module's current settings.",
             "inputSchema": {"type": "object", "properties": {"module": module}, "required": ["module"]}},
            {"name": "powertoys_get_schema", "description": "Read the DSC JSON schema for one PowerToys module.",
             "inputSchema": {"type": "object", "properties": {"module": module}, "required": ["module"]}},
            {"name": "powertoys_test_settings", "description": "Dry-run a partial PowerToys setting update without changing it.",
             "inputSchema": {"type": "object", "properties": {"module": module, "properties": properties},
                             "required": ["module", "properties"]}},
            {"name": "powertoys_apply_settings", "description": "Apply a preflighted partial setting update. Only Awake and AlwaysOnTop are supported; this is a high-risk action requiring fresh user confirmation.",
             "inputSchema": {"type": "object", "properties": {"module": module, "properties": properties},
                             "required": ["module", "properties"]}},
            {"name": "powertoys_apply_profile", "description": "Atomically apply 1 to 8 preflighted changes for supported productivity modules. DeskOrb rolls back already-applied changes if a later one fails. This is a high-risk action requiring fresh user confirmation.",
             "inputSchema": {"type": "object", "properties": {"changes": changes}, "required": ["changes"]}},
            {"name": "powertoys_list_backups", "description": "List backups created by apply actions in this MCP session.",
             "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "powertoys_restore_backup", "description": "Restore a backup made by this MCP session. This is a high-risk action requiring fresh user confirmation.",
             "inputSchema": {"type": "object", "properties": {"backup_id": {"type": "string"}}, "required": ["backup_id"]}},
        ]

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _emit(message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    PowerToysMcpServer().run()
