"""Deterministic browser-task constraints derived from the user's request.

Page observations are deliberately not accepted here.  This module only turns
the original task text into bounded execution budgets and verification hints so
an untrusted web page cannot widen the task contract.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from browser_evidence import is_safe_public_browser_url, origin_from_url


_COUNT_TOKEN = r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})"
_NUMBERED_RESULT_PATTERNS = (
    re.compile(rf"(?:前|至少|top|first)\s*(?P<count>{_COUNT_TOKEN})", re.IGNORECASE),
    re.compile(
        rf"(?P<count>{_COUNT_TOKEN})\s*个\s*(?:(?:Agent|GitHub)\s*)*"
        r"(?:框架|项目|页面|issue|issues|结果|条目|候选|仓库|商品|tab|标签页)",
        re.IGNORECASE,
    ),
    re.compile(rf"(?:调研|选择|查看|找到|打开|保留)\s*(?P<count>{_COUNT_TOKEN})", re.IGNORECASE),
)
_TAB_LIMIT = re.compile(r"(?:最多|不超过|up to|at most)[^0-9]{0,20}(\d{1,3})\s*(?:个)?\s*(?:tab|页面|标签页|tabs?)?", re.IGNORECASE)
_YEAR_LIMIT = re.compile(r"(?:超过|大于|older than)\s*(?:一年|1\s*year|12\s*months?)", re.IGNORECASE)
_EXPLICIT_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Named sites in the user's request are explicit targets. Arbitrary page text
# is never added here; observed links are admitted later only after a fresh
# browser snapshot attests them.
_NAMED_SITE_ORIGINS = {
    "github": "https://github.com",
    "git hub": "https://github.com",
    "百度": "https://www.baidu.com",
    "baidu": "https://www.baidu.com",
    "bing": "https://www.bing.com",
    "google": "https://www.google.com",
    "wikipedia": "https://www.wikipedia.org",
    "openai": "https://openai.com",
    "python 官方": "https://www.python.org",
    "python official": "https://www.python.org",
}


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker.casefold() in text for marker in markers)


def _count_value(value: str) -> int:
    token = str(value or "").strip()
    if token.isdigit():
        return int(token)
    chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if len(token) == 1:
        return chinese.get(token, 0)
    if token == "十":
        return 10
    if token.startswith("十"):
        return 10 + chinese.get(token[1:], 0)
    if token.endswith("十"):
        return chinese.get(token[:-1], 0) * 10
    if len(token) == 2 and token[0] in chinese and token[1] in chinese:
        return chinese[token[0]] * 10 + chinese[token[1]]
    return 0


@dataclass(frozen=True)
class BrowserTaskSpec:
    """A privacy-safe, bounded contract for one browser task."""

    max_tabs: int = 6
    max_scrolls: int = 20
    max_navigation_retries: int = 1
    max_transport_recoveries: int = 1
    max_action_steps: int = 30
    hard_action_steps: int = 120
    minimum_results: int = 0
    required_evidence: tuple[str, ...] = ()
    confirmation_points: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = (
        "submit", "purchase", "checkout", "upload", "download", "credential_input",
    )
    navigation_mode: str = "research"
    allowed_origins: tuple[str, ...] = ()
    stale_age_years: int | None = None
    final_tab_mode: str = "any"
    minimum_tab_pairs: int = 0
    deep_research: bool = False
    search_discovery_required: bool = False

    @classmethod
    def from_goal(cls, goal: str) -> "BrowserTaskSpec":
        text = " ".join(str(goal or "").strip().casefold().split())
        if not text:
            return cls(navigation_mode="none")

        explicit_origins: set[str] = set()
        for match in _EXPLICIT_URL.finditer(str(goal or "")):
            candidate = match.group(0).rstrip(".,;:!?)]}>")
            if is_safe_public_browser_url(candidate):
                origin = origin_from_url(candidate)
                if origin:
                    explicit_origins.add(origin)
        for name, origin in _NAMED_SITE_ORIGINS.items():
            if name in text:
                explicit_origins.add(origin)

        explicit_tab = _TAB_LIMIT.search(text)
        max_tabs = 6
        if explicit_tab:
            try:
                max_tabs = max(1, min(12, int(explicit_tab.group(1))))
            except (TypeError, ValueError):
                max_tabs = 6

        minimum_results = 0
        for pattern in _NUMBERED_RESULT_PATTERNS:
            result_match = pattern.search(text)
            if not result_match:
                continue
            minimum_results = max(0, min(20, _count_value(result_match.group("count"))))
            if minimum_results:
                break

        deep_research = _has_any(text, (
            "每个框架", "每个项目", "五个", "三个", "三 个", "framework", "readme",
            "issue", "multi-agent", "mcp", "memory", "tool calling", "跨页面",
            "分别打开", "配对", "调研", "research",
        ))
        multi_page = deep_research or _has_any(text, (
            "多个页面", "多页面", "多 tab", "多tab", "标签页", "tab", "依次查看",
            "跨页", "返回上一层", "滚动",
        ))
        if deep_research:
            action_steps = 120
        elif multi_page:
            action_steps = 80
        else:
            action_steps = 30

        fields: list[str] = []
        if _has_any(text, ("标题", "名称", "title", "项目")):
            fields.append("title")
        if _has_any(text, ("star", "stars")):
            fields.append("stars")
        if _has_any(text, ("语言", "language")):
            fields.append("language")
        if _has_any(text, ("更新时间", "更新", "updated", "commit")):
            fields.append("updated_at")
        for marker, field in (
            (("mcp",), "mcp"),
            (("memory", "记忆"), "memory"),
            (("multi-agent", "multi agent", "多 agent", "多agent"), "multi_agent"),
            (("tool calling", "tool-calling", "工具调用"), "tool_calling"),
            (("installation", "install", "安装"), "installation"),
            (("issue", "问题"), "issue_title"),
            (("发布时间", "published"), "published_at"),
        ):
            if _has_any(text, marker):
                fields.append(field)
        if not fields and minimum_results:
            fields.extend(("title", "url"))
        if "url" not in fields and _has_any(text, ("链接", "url", "官网", "仓库")):
            fields.append("url")

        confirmation: list[str] = []
        if _has_any(text, ("登录", "login", "submit", "提交", "购买", "purchase")):
            confirmation.append("high_risk_external_action")

        final_tab_mode = "any"
        if _has_any(text, ("最终只保留搜索引擎", "最终只保留搜索页", "最终只保留搜索引擎页面")):
            final_tab_mode = "search_only"
        elif _has_any(text, ("最终只保留三个 github", "只保留三个 github", "只保留三个仓库")):
            final_tab_mode = "github_repositories"

        minimum_tab_pairs = 0
        if (final_tab_mode == "search_only"
                and _has_any(text, ("langgraph", "autogen", "crewai", "openai agents sdk", "pydanticai"))):
            # The tab-pressure acceptance contract names five framework pairs;
            # keep the count in the immutable task spec so final-tab state
            # alone cannot make a partial run look complete.
            named_frameworks = (
                "langgraph", "autogen", "crewai", "openai agents sdk", "pydanticai",
            )
            minimum_tab_pairs = sum(name in text for name in named_frameworks)

        return cls(
            max_tabs=max_tabs,
            max_scrolls=20,
            max_navigation_retries=1,
            max_transport_recoveries=1,
            max_action_steps=action_steps,
            hard_action_steps=120,
            minimum_results=minimum_results,
            required_evidence=tuple(dict.fromkeys(fields)),
            confirmation_points=tuple(dict.fromkeys(confirmation)),
            navigation_mode="research",
            allowed_origins=tuple(sorted(explicit_origins)),
            stale_age_years=1 if _YEAR_LIMIT.search(text) or "一年" in text else None,
            final_tab_mode=final_tab_mode,
            minimum_tab_pairs=minimum_tab_pairs,
            deep_research=deep_research,
            search_discovery_required=_has_any(text, (
                "使用搜索引擎", "打开搜索引擎", "搜索引擎", "search engine",
            )),
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "max_tabs": self.max_tabs,
            "max_scrolls": self.max_scrolls,
            "max_navigation_retries": self.max_navigation_retries,
            "max_transport_recoveries": self.max_transport_recoveries,
            "max_action_steps": self.max_action_steps,
            "hard_action_steps": self.hard_action_steps,
            "minimum_results": self.minimum_results,
            "required_evidence": list(self.required_evidence),
            "confirmation_points": list(self.confirmation_points),
            "forbidden_actions": list(self.forbidden_actions),
            "navigation_mode": self.navigation_mode,
            "allowed_origins": list(self.allowed_origins),
            "stale_age_years": self.stale_age_years,
            "final_tab_mode": self.final_tab_mode,
            "minimum_tab_pairs": self.minimum_tab_pairs,
            "deep_research": self.deep_research,
            "search_discovery_required": self.search_discovery_required,
        }


__all__ = ["BrowserTaskSpec"]
