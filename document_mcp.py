"""DeskOrb's narrow, read-only local document MCP adapter.

MarkItDown is used when installed for Office/PDF and other binary formats.
Plain text formats have a dependency-free fallback so the MCP remains useful
in a clean test environment.  The adapter never writes, scans directories, or
accepts remote document URLs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from mcp_security import validate_local_path


class DocumentError(RuntimeError):
    """A bounded, user-facing document adapter error."""


TEXT_SUFFIXES = frozenset({".txt", ".md", ".rst", ".csv", ".json", ".xml", ".html", ".htm", ".log"})
BINARY_SUFFIXES = frozenset({".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"})
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_CHARS = 120 * 1024


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DocumentError(f"{label} must be an object")
    return value


class DocumentAdapter:
    """Read one explicitly scoped document and return bounded text evidence."""

    def __init__(self, allowed_roots: Iterable[str | Path], *,
                 max_file_bytes: int = MAX_FILE_BYTES,
                 markitdown_factory: Callable[[], Any] | None = None) -> None:
        self.allowed_roots = tuple(Path(root).expanduser() for root in allowed_roots if str(root).strip())
        self.max_file_bytes = max(1, min(MAX_FILE_BYTES, int(max_file_bytes)))
        self._markitdown_factory = markitdown_factory
        self._markitdown_checked = markitdown_factory is not None
        self._markitdown: Any | None = None

    @property
    def markitdown_available(self) -> bool:
        return self._get_markitdown() is not None

    def inspect_file(self, path: str | Path) -> dict[str, Any]:
        source = self._validated_file(path)
        return {
            "ok": True,
            "format": source.suffix.lower().lstrip("."),
            "bytes": source.stat().st_size,
            "markitdown_available": self.markitdown_available,
        }

    def convert_file(self, path: str | Path, *, max_chars: int = MAX_OUTPUT_CHARS) -> dict[str, Any]:
        source = self._validated_file(path)
        limit = max(1, min(MAX_OUTPUT_CHARS, int(max_chars)))
        suffix = source.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            try:
                text = source.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise DocumentError("document could not be read") from exc
        elif suffix in BINARY_SUFFIXES:
            converter = self._get_markitdown()
            if converter is None:
                raise DocumentError("document dependency unavailable: install the pinned MarkItDown runtime")
            try:
                result = converter.convert(str(source))
                text = str(getattr(result, "text_content", "") or "")
            except Exception as exc:
                raise DocumentError("document conversion failed") from exc
        else:
            raise DocumentError("document format is not allowlisted")
        text = text.replace("\x00", "")
        return {
            "ok": True,
            "format": suffix.lstrip("."),
            "characters": len(text),
            "truncated": len(text) > limit,
            "text": text[:limit],
        }

    def _validated_file(self, path: str | Path) -> Path:
        try:
            source = validate_local_path(path, self.allowed_roots)
        except (OSError, ValueError) as exc:
            raise DocumentError(str(exc)) from exc
        if source.stat().st_size > self.max_file_bytes:
            raise DocumentError("document exceeds the configured size limit")
        if source.suffix.lower() not in TEXT_SUFFIXES | BINARY_SUFFIXES:
            raise DocumentError("document format is not allowlisted")
        return source

    def _get_markitdown(self) -> Any | None:
        if self._markitdown_checked:
            return self._markitdown
        self._markitdown_checked = True
        try:
            from markitdown import MarkItDown  # type: ignore[import-not-found]

            self._markitdown = MarkItDown()
        except Exception:
            self._markitdown = None
        return self._markitdown


class DocumentMcpServer:
    """Minimal stdio MCP server exposing only read-only document tools."""

    def __init__(self, allowed_roots: Iterable[str | Path] = ()) -> None:
        self.adapter = DocumentAdapter(allowed_roots)

    @classmethod
    def from_environment(cls) -> "DocumentMcpServer":
        raw = os.environ.get("DESKORB_AGENT_DOCUMENT_ROOTS", "")
        roots = [item for item in raw.split(os.pathsep) if item.strip()]
        return cls(roots)

    def run(self) -> None:
        for raw in sys.stdin:
            message: dict[str, Any] = {}
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                response = self._handle(message)
                if response is not None:
                    self._emit(response)
            except Exception as exc:
                request_id = message.get("id")
                if request_id is not None:
                    self._emit(self._error(request_id, -32603, str(exc)[:300]))

    def _handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method, request_id = str(message.get("method") or ""), message.get("id")
        if method == "initialize":
            return self._result(request_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "deskorb-documents", "version": "0.1.0"},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._result(request_id, {"tools": self._tools()})
        if method == "tools/call":
            params = _json_object(message.get("params") or {}, "tools/call parameters")
            arguments = _json_object(params.get("arguments") or {}, "tool arguments")
            result = self._call(str(params.get("name") or ""), arguments)
            return self._result(request_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "isError": not bool(result.get("ok")),
            })
        if request_id is None:
            return None
        return self._error(request_id, -32601, f"Unknown MCP method: {method}")

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "document_status":
                return {"ok": True, "configured": bool(self.adapter.allowed_roots),
                        "markitdown_available": self.adapter.markitdown_available}
            if name == "document_inspect":
                return self.adapter.inspect_file(str(arguments.get("path") or ""))
            if name == "document_convert_file":
                return self.adapter.convert_file(str(arguments.get("path") or ""),
                                                 max_chars=int(arguments.get("max_chars", MAX_OUTPUT_CHARS)))
            raise DocumentError(f"Unknown document tool: {name}")
        except (DocumentError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:300]}

    @staticmethod
    def _tools() -> list[dict[str, Any]]:
        path = {"type": "string", "description": "A file inside the configured document root."}
        read_only = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
        return [
            {"name": "document_status", "description": "Report local document adapter readiness.",
             "annotations": read_only,
             "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "document_inspect", "description": "Inspect one scoped local document without returning its contents.",
             "annotations": read_only,
             "inputSchema": {"type": "object", "properties": {"path": path}, "required": ["path"]}},
            {"name": "document_convert_file", "description": "Convert one scoped local document to bounded text evidence.",
             "annotations": read_only,
             "inputSchema": {"type": "object", "properties": {
                 "path": path, "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_CHARS}},
                 "required": ["path"]}},
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
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", action="append", default=[])
    args, _unknown = parser.parse_known_args()
    roots = args.root or [item for item in os.environ.get("DESKORB_AGENT_DOCUMENT_ROOTS", "").split(os.pathsep)
                          if item.strip()]
    DocumentMcpServer(roots).run()
