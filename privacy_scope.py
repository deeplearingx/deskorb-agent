"""Small, dependency-free privacy intent helpers shared by the overlay and runtime."""
from __future__ import annotations

import re


# This is deliberately a conservative detector for requests that explicitly ask for
# current desktop/diagnostic context.  It is not a general language-model intent
# classifier; the user-facing consent card remains the authoritative decision.
_CURRENT_CONTEXT_RE = re.compile(
    r"(?:当前(?:桌面|屏幕|窗口|状态)|可见状态|看(?:一下|下)?(?:我的)?(?:桌面|屏幕)|"
    r"分析(?:当前|可见)?(?:桌面|屏幕|窗口|状态)|读取(?:本地)?(?:desk?orb)?(?:调试)?日志|"
    r"查看(?:本地)?(?:desk?orb)?(?:调试)?日志|(?:current|active)\s+(?:desktop|screen|window|state)|"
    r"inspect\s+(?:the\s+)?(?:current|active)\s+(?:desktop|screen|window)|"
    r"(?:read|inspect|analy[sz]e)\s+(?:the\s+)?(?:local\s+)?(?:desk?orb\s+)?logs?)",
    re.IGNORECASE,
)


def requires_current_context_consent(text: object) -> bool:
    """Whether *text* explicitly requests current-screen or local diagnostic context."""
    return bool(_CURRENT_CONTEXT_RE.search(str(text or "")))
