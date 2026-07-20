"""Read local provider settings without ever logging their values.

Supports the project's existing ``key:value`` .env format as well as ordinary
``KEY=value`` variables. Environment variables always take precedence.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _read_file() -> dict[str, str]:
    path = Path(__file__).resolve().parents[1] / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    return _parse_lines(lines)


def _parse_lines(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([^:=\s]+)\s*[:=]\s*(.*)$", line)
        if not match:
            continue
        key, value = match.groups()
        values[key.strip().lower()] = value.strip().strip('"').strip("'")
    return values


_VALUES = _read_file()


def api_key() -> str:
    return (os.environ.get("OPENAI_API_KEY", "").strip() or _VALUES.get("api-key", "")
            or _VALUES.get("openai_api_key", ""))


def api_base_url() -> str:
    return (os.environ.get("OPENAI_BASE_URL", "").strip() or _VALUES.get("url", "")
            or _VALUES.get("openai_base_url", ""))


def api_model(default: str) -> str:
    value = (os.environ.get("DESKORB_AGENT_API_MODEL", "").strip()
             or os.environ.get("CODEX_OVERLAY_API_MODEL", "").strip()
             or _VALUES.get("model_name", "")
             or _VALUES.get("codex_overlay_api_model", ""))
    # The supplied model_name was an endpoint URL, not a model ID. Do not pass
    # a URL to the model field; the tested interactive default is Terra.
    return default if not value or "://" in value else value
