"""Read local provider settings without ever logging their values.

Supports the project's existing ``key:value`` .env format as well as ordinary
``KEY=value`` variables. Environment variables always take precedence.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent
_PARENT_ENV_PATH = _PROJECT_ROOT.parent / ".env"


def _resolve_env_path() -> Path:
    """Resolve the local provider file without exposing its contents."""
    explicit = os.environ.get("DESKORB_AGENT_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    for candidate in (_PROJECT_ROOT / "volcengine.env", _PROJECT_ROOT / ".env", _PARENT_ENV_PATH):
        if candidate.is_file():
            return candidate.resolve()
    return _PARENT_ENV_PATH


_ENV_PATH = _resolve_env_path()


def _file_mtime_ns() -> int | None:
    try:
        return _ENV_PATH.stat().st_mtime_ns
    except Exception:
        return None


def _read_file() -> dict[str, str]:
    try:
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()
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
_ENV_MTIME_NS = _file_mtime_ns()
_VALUES_LOCK = threading.RLock()


def _values() -> dict[str, str]:
    global _VALUES, _ENV_MTIME_NS
    current_mtime = _file_mtime_ns()
    with _VALUES_LOCK:
        if current_mtime != _ENV_MTIME_NS:
            _VALUES = _read_file()
            _ENV_MTIME_NS = current_mtime
        return _VALUES


def _explicit_key_names(provider: str | None) -> tuple[str, ...]:
    name = str(provider or "").strip().lower().replace("_", "-")
    return {
        "openai": ("OPENAI_API_KEY", "openai_api_key"),
        "responses": ("OPENAI_API_KEY", "openai_api_key"),
        "deepseek": ("DEEPSEEK_API_KEY", "deepseek_api_key"),
        "qwen": ("QWEN_API_KEY", "qwen_api_key", "DASHSCOPE_API_KEY", "dashscope_api_key"),
        "dashscope": ("DASHSCOPE_API_KEY", "dashscope_api_key", "QWEN_API_KEY", "qwen_api_key"),
        "openai-compatible": ("OPENAI_API_KEY", "openai_api_key"),
    }.get(name, ())


def api_key(provider: str | None = None) -> str:
    values = _values()
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
            value = os.environ.get(key, "").strip() if key.isupper() else values.get(key, "")
            if value:
                return value
        if name in {"deepseek", "qwen", "dashscope"}:
            return ""
    if name == "openai-compatible":
        # An OpenAI-compatible endpoint must not silently inherit a vendor
        # key such as DEEPSEEK_API_KEY.  This matters for Ark and other
        # compatible gateways, where the wrong vendor key is rejected as an
        # invalid key even though a valid gateway key exists in the project
        # env file.
        candidates = (
            os.environ.get("OPENAI_API_KEY", "").strip(),
            values.get("openai_api_key", ""),
            values.get("api-key", ""),
        )
        return next((value for value in candidates if value), "")
    candidates = (
        os.environ.get("OPENAI_API_KEY", "").strip(),
        os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        os.environ.get("DASHSCOPE_API_KEY", "").strip(),
        os.environ.get("QWEN_API_KEY", "").strip(),
        values.get("api-key", ""), values.get("openai_api_key", ""),
        values.get("deepseek_api_key", ""), values.get("dashscope_api_key", ""),
        values.get("qwen_api_key", ""),
    )
    return next((value for value in candidates if value), "")


def explicit_api_key(provider: str | None = None) -> str:
    values = _values()
    for key in _explicit_key_names(provider):
        value = os.environ.get(key, "").strip() if key.isupper() else values.get(key, "")
        if value:
            return value
    return ""


def api_base_url() -> str:
    values = _values()
    return (os.environ.get("OPENAI_BASE_URL", "").strip() or values.get("url", "")
            or values.get("openai_base_url", ""))


def api_model(default: str) -> str:
    values = _values()
    value = (os.environ.get("DESKORB_AGENT_API_MODEL", "").strip()
             or os.environ.get("CODEX_OVERLAY_API_MODEL", "").strip()
             or values.get("model_name", "")
             or values.get("codex_overlay_api_model", ""))
    # The supplied model_name was an endpoint URL, not a model ID. Do not pass
    # a URL to the model field; the tested interactive default is Terra.
    return default if not value or "://" in value else value


def model_fallbacks_raw() -> str:
    values = _values()
    return (os.environ.get("DESKORB_AGENT_MODEL_FALLBACKS", "").strip()
            or values.get("deskorb_agent_model_fallbacks", "")
            or values.get("model_fallbacks", ""))


def model_capabilities_raw() -> str:
    values = _values()
    return (os.environ.get("DESKORB_AGENT_MODEL_CAPABILITIES", "").strip()
            or values.get("deskorb_agent_model_capabilities", "")
            or values.get("model_capabilities", ""))


def model_provider(default: str = "auto") -> str:
    values = _values()
    return (os.environ.get("DESKORB_AGENT_PROVIDER", "").strip()
            or values.get("deskorb_agent_provider", "")
            or values.get("provider", "")
            or default)
