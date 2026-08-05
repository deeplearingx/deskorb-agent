"""Classify structured browser observations without treating page text as policy."""
from __future__ import annotations

import json
from enum import Enum
from typing import Any


class BrowserState(str, Enum):
    READY = "ready"
    LOADING = "loading"
    HUMAN_HANDOFF = "human_handoff"
    BROKEN = "broken"
    EMPTY = "empty"
    ERROR = "error"


def classify_browser_result(tool_name: str, result: dict[str, Any]) -> BrowserState:
    if not isinstance(result, dict) or not result.get("ok"):
        text = str((result or {}).get("error") or "").lower() if isinstance(result, dict) else ""
        if any(marker in text for marker in ("target closed", "browser closed", "disconnected", "process exited")):
            return BrowserState.BROKEN
        return BrowserState.ERROR
    try:
        content = json.dumps(result.get("content", result), ensure_ascii=False).lower()
    except (TypeError, ValueError):
        content = str(result.get("content", "")).lower()
    if not content.strip() or content in {"[]", "{}", "null", '""'}:
        return BrowserState.EMPTY
    if any(marker in content for marker in ("captcha", "我是人类", "快速验证身份", "请登录后继续",
                                             "sms verification", "扫描二维码", "选择账号")):
        return BrowserState.HUMAN_HANDOFF
    if any(marker in content for marker in ("loading", "加载中", "still loading", "please wait")):
        return BrowserState.LOADING
    return BrowserState.READY
