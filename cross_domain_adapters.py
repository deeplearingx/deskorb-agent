"""Safe handoff helpers between verified browser evidence and local targets."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class CrossDomainAdapters:
    """Pass only runtime-verified, allowlisted fields into a local stage."""

    ALLOWED_FIELDS = ("title", "source", "price", "rating", "url")

    def __init__(self, runtime: Any):
        self.runtime = runtime

    def write_file(self, fields: dict[str, Any], path: str,
                   *, evidence_verified: bool) -> dict[str, Any]:
        normalized = self._verified_fields(fields, evidence_verified)
        if normalized is None:
            return self._failure("cross_domain_evidence_required",
                                 "The browser evidence contract was not verified.")
        target = self._safe_path(path)
        if target is None:
            return self._failure("cross_domain_path_invalid",
                                 "The cross-domain file target must stay below the temporary working directory.")
        rendered = self._render(normalized)
        relative = str(target.relative_to(Path(self.runtime.working_dir).resolve()))
        written = self._local_tool("filesystem_write", {
            "path": relative, "text": rendered, "overwrite": True,
        })
        if not isinstance(written, dict) or not written.get("ok"):
            return self._failure(str((written or {}).get("failure_kind") or "file_write_failed"),
                                 "The local file stage could not write the verified fields.")
        readback = self._local_tool("filesystem_read_text", {
            "path": relative, "max_chars": 4096,
        })
        observed = str(readback.get("text") or "") if isinstance(readback, dict) and readback.get("ok") else ""
        passed = bool(isinstance(readback, dict) and readback.get("ok")
                      and self._digest(observed) == self._digest(rendered))
        return {
            "ok": passed, "verified": passed, "postcondition_passed": passed,
            "postcondition_kind": "file_readback", "field_count": len(normalized),
            "characters": len(rendered),
            **({} if passed else {
                "failure_kind": "file_verification_failure",
                "error": "The written file did not match the verified field handoff.",
            }),
        }

    def write_notepad(self, fields: dict[str, Any], *, evidence_verified: bool,
                      window_handle: int | None = None) -> dict[str, Any]:
        normalized = self._verified_fields(fields, evidence_verified)
        if normalized is None:
            return self._failure("cross_domain_evidence_required",
                                 "The browser evidence contract was not verified.")
        adapters = getattr(self.runtime, "desktop_adapters", None)
        if adapters is None or not callable(getattr(adapters, "notepad", None)):
            return self._failure("desktop_backend_unavailable",
                                 "The Notepad semantic adapter is unavailable.")
        previous = bool(getattr(self.runtime, "_cross_domain_handoff_active", False))
        if hasattr(self.runtime, "_cross_domain_handoff_active"):
            self.runtime._cross_domain_handoff_active = True
        try:
            result = adapters.notepad(self._render(normalized), window_handle=window_handle)
        finally:
            if hasattr(self.runtime, "_cross_domain_handoff_active"):
                self.runtime._cross_domain_handoff_active = previous
        if not isinstance(result, dict):
            return self._failure("desktop_value_readback_failed",
                                 "Notepad did not return a structured readback result.")
        return {
            "ok": bool(result.get("ok")), "verified": bool(result.get("verified")),
            "postcondition_passed": bool(result.get("postcondition_passed", result.get("verified"))),
            "postcondition_kind": "uia_value_readback", "field_count": len(normalized),
            "characters": len(self._render(normalized)),
            **({} if result.get("ok") else {
                "failure_kind": str(result.get("failure_kind") or "desktop_value_readback_failed")[:80],
                "error": "The Notepad semantic adapter did not verify the field handoff.",
            }),
        }

    @classmethod
    def _verified_fields(cls, fields: Any, evidence_verified: bool) -> dict[str, str] | None:
        if not evidence_verified or not isinstance(fields, dict):
            return None
        result: dict[str, str] = {}
        for key in cls.ALLOWED_FIELDS:
            value = fields.get(key)
            if value is None:
                continue
            text = str(value).replace("\x00", "").strip()
            if text:
                result[key] = text[:400]
        return result or None

    @staticmethod
    def _render(fields: dict[str, str]) -> str:
        return "\n".join(f"{key}: {fields[key]}" for key in CrossDomainAdapters.ALLOWED_FIELDS if key in fields)

    def _safe_path(self, value: str) -> Path | None:
        root = Path(self.runtime.working_dir).resolve()
        candidate = Path(str(value or "")).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def _local_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Bypass the model dispatcher for the internally-owned file handoff."""
        tools = getattr(self.runtime, "tools", None)
        if name == "filesystem_write" and tools is not None:
            writer = getattr(tools, "write_text", None)
            if callable(writer):
                return writer(arguments)
        if name == "filesystem_read_text" and tools is not None:
            reader = getattr(tools, "call", None)
            if callable(reader):
                return reader(name, arguments)
        dispatcher = getattr(self.runtime, "_run_local_tool", None)
        if not callable(dispatcher):
            return {"ok": False, "failure_kind": "file_stage_unavailable"}
        return dispatcher(name, arguments)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _failure(failure_kind: str, error: str) -> dict[str, Any]:
        return {"ok": False, "failure_kind": str(failure_kind or "cross_domain_failed")[:80],
                "error": str(error)[:240]}


__all__ = ["CrossDomainAdapters"]
