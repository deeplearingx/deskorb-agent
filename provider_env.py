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


def api_key(provider: str | None = None) -> str:
    """Return the key for a provider without exposing it in diagnostics.

    A provider-specific key wins over the legacy generic key.  Calling this
    without a provider preserves the original priority used by the active
    connection.
    """
    name = str(provider or "").strip().lower().replace("_", "-")
    provider_keys = {
        "openai": ("OPENAI_API_KEY", "openai_api_key"),
        "responses": ("OPENAI_API_KEY", "openai_api_key"),
        "deepseek": ("DEEPSEEK_API_KEY", "deepseek_api_key"),
        "qwen": ("QWEN_API_KEY", "qwen_api_key", "DASHSCOPE_API_KEY", "dashscope_api_key"),
        "dashscope": ("DASHSCOPE_API_KEY", "dashscope_api_key", "QWEN_API_KEY", "qwen_api_key"),
    }
    if name in provider_keys:
        for key in provider_keys[name]:
            value = os.environ.get(key, "").strip() if key.isupper() else _VALUES.get(key, "")
            if value:
                return value
    candidates = (
        os.environ.get("OPENAI_API_KEY", "").strip(),
        os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        os.environ.get("DASHSCOPE_API_KEY", "").strip(),
        os.environ.get("QWEN_API_KEY", "").strip(),
        _VALUES.get("api-key", ""), _VALUES.get("openai_api_key", ""),
        _VALUES.get("deepseek_api_key", ""), _VALUES.get("dashscope_api_key", ""),
        _VALUES.get("qwen_api_key", ""),
    )
    return next((value for value in candidates if value), "")


def model_fallbacks_raw() -> str:
    return (os.environ.get("DESKORB_AGENT_MODEL_FALLBACKS", "").strip()
            or _VALUES.get("deskorb_agent_model_fallbacks", "")
            or _VALUES.get("model_fallbacks", ""))


def model_capabilities_raw() -> str:
    return (os.environ.get("DESKORB_AGENT_MODEL_CAPABILITIES", "").strip()
            or _VALUES.get("deskorb_agent_model_capabilities", "")
            or _VALUES.get("model_capabilities", ""))


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
