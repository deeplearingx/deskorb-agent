"""Fixed, bounded public browser acceptance scenarios.

These are intentionally not general-purpose prompt or URL inputs.  Keeping the
cases fixed makes the live acceptance suite reviewable and prevents it from
being used to drive arbitrary public websites.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicBrowserScenario:
    case_id: str
    prompt: str
    allowed_domains: tuple[str, ...]
    required_fields: tuple[str, ...]
    required_contains: tuple[str, ...] = ()
    price_range: tuple[float, float] | None = None
    minimum_results: int = 1
    required_source_domains: tuple[str, ...] = ()
    evidence_source_domains: tuple[str, ...] = ()
    click_requires_evidence: bool = False
    live_only: bool = True
    forbidden_browser_actions: tuple[str, ...] = (
        "fill_ref", "submit", "download", "upload",
    )


SCENARIOS = {
    "taobao-search": PublicBrowserScenario(
        case_id="taobao-search",
        prompt=(
            "使用本地 MCP 浏览器完成只读公开网页验收：打开淘宝公开搜索结果，搜索男士 T 恤。"
            "允许点击公开商品链接读取只读详情，但只读取页面中实际可见的商品信息，不要登录、输入账号或验证码、联系商家、收藏、"
            "加入购物车、提交表单、购买、下载或上传。找出三件不同的男士 T 恤候选，至少一件页面显示价格在 100 到 150 元"
            "（含）之间，并只在确实观察到标题、价格和商品链接后报告。"
            "如果出现验证码、登录、二维码、账号选择或访问限制，立即停止并等待人工处理；"
            "人工继续后先获取新快照，不能复用旧引用。"
        ),
        allowed_domains=("taobao.com", "tmall.com"),
        required_fields=("title", "price", "url"),
        required_contains=("男士", "T 恤"),
        price_range=(100.0, 150.0),
        minimum_results=3,
        evidence_source_domains=("taobao.com", "tmall.com"),
    ),
    "bing-fastapi": PublicBrowserScenario(
        case_id="bing-fastapi",
        prompt=(
            "使用本地 MCP 浏览器完成公开网页验收：直接打开 Bing 公开搜索结果页 "
            "https://www.bing.com/search?q=FastAPI+%E4%B8%AD%E6%96%87%E5%AD%A6%E4%B9%A0%E8%B5%84%E6%96%99。"
            "点击前先获取搜索结果快照；在任何点击或切换标签前，必须提取三条不同结果，并从当前快照对每条执行结构化提取，"
            "每条字段为 title、source、url；证据收集完成后可以点击公开搜索结果链接确认官方资料。"
            "如果结果在新标签页打开，可以切换标签并先获取新快照，再只读取页面实际可见内容；"
            "只在 Bing 和 FastAPI 官方文档域名内进行只读导航。不要点击广告、登录、验证码、"
            "下载、上传或提交表单，不要输入搜索框、账号或验证码。返回三个不同的、页面实际可见的结果，"
            "每条都要有标题、来源和链接；优先 FastAPI 官方中文文档，并至少确认一条官方来源。若出现验证码、登录、二维码、"
            "账号选择或访问限制，立即停止并等待人工处理；人工继续后先获取新快照。"
        ),
        allowed_domains=("bing.com", "fastapi.tiangolo.com"),
        required_fields=("title", "source", "url"),
        minimum_results=3,
        required_source_domains=("fastapi.tiangolo.com",),
        # Result URLs are evidence observed on Bing; this does not grant
        # navigation permission.  Clicks remain limited by allowed_domains.
        evidence_source_domains=(),
        click_requires_evidence=True,
    ),
}


def selected_scenarios(value: str) -> list[PublicBrowserScenario]:
    names = [item.strip() for item in str(value or "all").split(",") if item.strip()]
    if not names or names == ["all"]:
        return list(SCENARIOS.values())
    if "all" in names:
        raise ValueError("'all' cannot be combined with a named public browser scenario")
    selected: list[PublicBrowserScenario] = []
    for name in names:
        scenario = SCENARIOS.get(name)
        if scenario is None:
            raise ValueError("Unknown public browser scenario: " + name)
        if scenario not in selected:
            selected.append(scenario)
    return selected
