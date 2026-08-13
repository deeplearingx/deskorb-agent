"""Deterministic task-stage planning for mixed browser and desktop work.

The plan is derived only from the original user goal.  Browser observations are
data, not instructions, and therefore never participate in plan construction.
"""
from __future__ import annotations

from dataclasses import dataclass


_DESKTOP_MARKERS = (
    "qq", "资源管理器", "文件管理器", "记事本", "计算器", "explorer", "notepad", "calculator",
)
_WEB_MARKERS = (
    "浏览器", "网页", "网站", "淘宝", "京东", "百度", "google", "browser", "website",
    "web page", "http://", "https://",
)
_SEARCH_MARKERS = ("搜索", "search", "研究", "research", "查找", "look up")
_FILE_MARKERS = ("保存", "写入文件", "写到文件", "文件", "report.txt", "save", "write to", "file")
_NOTEPAD_MARKERS = ("记事本", "notepad", "输入到桌面", "写入记事本")
_RISK_MARKERS = (
    "发送", "send", "发布", "publish", "购买", "buy", "purchase", "checkout", "结算",
    "删除", "delete", "上传", "upload", "登录", "login", "提交", "submit", "password", "密码",
)


@dataclass(frozen=True)
class TaskPlan:
    """A privacy-safe capability and evidence contract for one user goal."""

    primary_phase: str
    browser_required: bool
    follow_up_kind: str
    allowed_capabilities: tuple[str, ...]
    evidence_contract: tuple[str, ...]
    high_risk: bool

    @classmethod
    def from_goal(cls, goal: str) -> "TaskPlan":
        lowered = str(goal or "").strip().lower()
        desktop = any(marker in lowered for marker in _DESKTOP_MARKERS)
        explicit_web = any(marker in lowered for marker in _WEB_MARKERS)
        # An explicit web target wins even when the later stage mentions
        # Notepad or a file.  Desktop-only searches (QQ/Explorer) remain
        # desktop plans because they have no explicit web marker.
        browser = explicit_web or (not desktop and any(marker in lowered for marker in _SEARCH_MARKERS))
        follow_up = ""
        if browser and any(marker in lowered for marker in _NOTEPAD_MARKERS):
            follow_up = "desktop"
        elif browser and any(marker in lowered for marker in _FILE_MARKERS):
            follow_up = "file"

        high_risk = any(marker in lowered for marker in _RISK_MARKERS)
        if browser:
            capabilities = ("browser_action_batch",)
            evidence = ["browser_extract", "browser_verify"]
            if high_risk:
                evidence.append("confirmation")
            phase = "browser"
        elif desktop:
            capabilities = (
                "application_launch", "desktop_uia_observe", "desktop_uia_invoke",
                "desktop_uia_set_value", "desktop_verify_state",
            )
            evidence = ["desktop_uia_observe", "desktop_verify"]
            if high_risk:
                evidence.append("confirmation")
            phase = "desktop"
        else:
            capabilities = ()
            evidence = []
            phase = "general"
        return cls(
            primary_phase=phase,
            browser_required=browser,
            follow_up_kind=follow_up,
            allowed_capabilities=capabilities,
            evidence_contract=tuple(evidence),
            high_risk=high_risk,
        )

    def capabilities_for_stage(self, browser_verified: bool) -> tuple[str, ...]:
        """Return the minimal stage capability set, never including page data."""
        if self.primary_phase != "browser":
            return self.allowed_capabilities
        if not browser_verified:
            return ("browser_action_batch",)
        if self.follow_up_kind == "file":
            return ("filesystem_list", "filesystem_read_text", "filesystem_search_text", "filesystem_write")
        if self.follow_up_kind == "desktop":
            return (
                "application_launch", "desktop_uia_observe", "desktop_uia_invoke",
                "desktop_uia_set_value", "desktop_verify_state",
            )
        return ()

    def safe_dict(self) -> dict[str, object]:
        return {
            "primary_phase": self.primary_phase,
            "browser_required": self.browser_required,
            "follow_up_kind": self.follow_up_kind,
            "allowed_capabilities": list(self.allowed_capabilities),
            "evidence_contract": list(self.evidence_contract),
            "high_risk": self.high_risk,
        }


__all__ = ["TaskPlan"]
