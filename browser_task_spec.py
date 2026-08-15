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
_QUOTED_TEXT = re.compile(r"[\"“「『](?P<value>[^\"”」』\r\n]{2,120})[\"”」』]")
_SEARCH_QUERY = re.compile(
    r"(?:搜索|查找|检索|search(?:\s+for)?)\s*[\"“「『]?"
    # An unquoted comma commonly separates the search step from the next
    # action in Chinese requests (for example, "搜索4399，打开它"). Quoted
    # queries still keep commas because the closing quote terminates them.
    r"(?P<value>[^\"”」』，,。！？；;\r\n]{2,120}?)(?:[\"”」』，,。！？；;\r\n]|$)",
    re.IGNORECASE,
)
_ENTRY_TARGET = re.compile(
    r"(?:找到|定位|find|locate)\s*[\"“「『]?"
    r"(?P<value>[^\"”」』，,。；;\r\n]{2,80}?)(?:相关)?"
    r"(?:入口|游戏入口|官网|官方网站|词条|article|页面|page)",
    re.IGNORECASE,
)
_DIRECT_ENTRY_TARGET = re.compile(
    r"(?:进入|打开|访问|open)\s*[\"“「『]?"
    r"(?P<value>[^\"”」』，,。；;\r\n]{2,80}?)(?:相关)?"
    r"(?:入口|游戏入口|官网|官方网站|词条|article|页面|page)",
    re.IGNORECASE,
)
_FIND_AND_OPEN_TARGET = re.compile(
    r"(?:找到|定位)\s*(?:并\s*)?打开\s*[\"“「『]?"
    r"(?P<value>[^\"”」』，,。！？；;\r\n]{2,80}?)(?:[\"”」』]|$|[，,。！？；;\r\n])",
    re.IGNORECASE,
)
_TARGET_SITE_WORDS = frozenset({
    "4399", "百度", "baidu", "bing", "google", "github", "wikipedia", "京东", "淘宝",
})

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


def _requires_high_risk_confirmation(text: str) -> bool:
    """Detect requested high-risk actions without treating prohibitions as requests.

    Acceptance prompts deliberately contain phrases such as "不要登录" and
    "不要购买".  Those are immutable forbidden-action constraints, not
    confirmation points.  Keep confirmation requirements tied to a positive
    action clause from the user's task instead of any incidental occurrence
    of a sensitive word.
    """
    clauses = re.split(r"[。！？；;.!?\n]+", str(text or ""))
    markers = ("登录", "login", "submit", "提交", "购买", "purchase")
    negations = ("不要", "禁止", "不允许", "不得", "无需", "不能", "do not", "never", "without")
    protection_only = ("登录保护", "login protection", "验证码", "captcha", "人工验证", "human verification")
    action_words = ("点击", "进入", "打开", "执行", "click", "enter", "open", "authorize")
    for clause in clauses:
        lowered = clause.casefold()
        for marker in markers:
            if marker.casefold() not in lowered:
                continue
            if any(negation.casefold() in lowered for negation in negations):
                continue
            if marker.casefold() in {"登录", "login"} and any(
                    item.casefold() in lowered for item in protection_only
            ) and not any(item.casefold() in lowered for item in action_words):
                continue
            return True
    return False


def _target_terms_from_goal(goal: str, *, search_query: str = "") -> tuple[str, ...]:
    """Extract immutable user-target phrases without inventing page data.

    This is intentionally conservative.  Quoted search text and the noun
    before an explicit "entry/article/page" marker are useful semantic
    targets; if neither is present, return no target instead of guessing a
    product or game name from the surrounding prose.
    """
    original = str(goal or "")
    candidates: list[str] = []
    entry_match = (
        _ENTRY_TARGET.search(original)
        or _DIRECT_ENTRY_TARGET.search(original)
        or _FIND_AND_OPEN_TARGET.search(original)
    )
    if entry_match:
        candidates.append(entry_match.group("value"))
    if not candidates and search_query:
        candidates.extend(match.group("value") for match in _QUOTED_TEXT.finditer(search_query))
        if not candidates:
            candidates.append(search_query)
    if not candidates:
        candidates.extend(match.group("value") for match in _QUOTED_TEXT.finditer(original))

    result: list[str] = []
    for candidate in candidates:
        value = " ".join(str(candidate).strip().split())
        value = re.sub(r"^(?:搜索|查找|检索|search(?:\s+for)?)\s*", "", value, flags=re.IGNORECASE)
        tokens = [token for token in value.split() if token.casefold() not in _TARGET_SITE_WORDS]
        value = " ".join(tokens).strip(" ，,。；;:：")
        if value and len(value) <= 120 and value.casefold() not in _TARGET_SITE_WORDS:
            if value.casefold() not in {item.casefold() for item in result}:
                result.append(value)
    return tuple(result[:8])


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
    search_query: str = ""
    target_terms: tuple[str, ...] = ()
    target_kind: str = ""

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
        # "Wikipedia" is a named site, not a single host: the public portal
        # commonly redirects to the language subdomain used for the requested
        # article.  Both hosts are still explicit user-scope, while arbitrary
        # Wikipedia subdomains remain blocked until observed in the page.
        if "wikipedia" in text:
            explicit_origins.add("https://en.wikipedia.org")

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
        if _requires_high_risk_confirmation(text):
            confirmation.append("high_risk_external_action")

        search_query = ""
        search_match = _SEARCH_QUERY.search(str(goal or ""))
        if search_match:
            raw_query = search_match.group("value") or ""
            search_query = " ".join(raw_query.strip().split())[:120]
        target_terms = _target_terms_from_goal(goal, search_query=search_query)
        target_kind = "entry" if (
            _ENTRY_TARGET.search(str(goal or ""))
            or _DIRECT_ENTRY_TARGET.search(str(goal or ""))
            or _FIND_AND_OPEN_TARGET.search(str(goal or ""))
        ) else ""
        # Entry tasks often need one bounded site-search detour after the
        # target phrase appears only in ordinary page text.  Keep the budget
        # finite and below the hard cap, but do not let that legitimate
        # recovery path consume the ordinary 30-step budget immediately.
        if target_kind == "entry" and action_steps < 50:
            action_steps = 50

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

        named_site_present = bool(explicit_origins)
        generic_search_discovery = bool(
            _has_any(text, ("搜索", "search"))
            and not named_site_present
            and not _has_any(text, ("站内搜索", "页面内搜索", "site search", "within wikipedia"))
        )
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
            search_discovery_required=bool(generic_search_discovery or _has_any(text, (
                "使用搜索引擎", "打开搜索引擎", "搜索引擎", "search engine",
            ))),
            search_query=search_query,
            target_terms=target_terms,
            target_kind=target_kind,
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
            "search_query": self.search_query,
            "target_terms": list(self.target_terms),
            "target_kind": self.target_kind,
        }


__all__ = ["BrowserTaskSpec"]
