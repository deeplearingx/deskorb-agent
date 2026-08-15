# DeskOrb Hybrid Research / Browser 设计

## 目标

把“收集公开资料”和“真实浏览器操作”拆成两个执行模式，避免让模型用 Playwright 完成不需要 UI 的信息收集，同时保留 Browser Agent 对动态 DOM、Tab、确认和异常恢复的真实验收能力。

## 模式路由

| 模式 | 触发条件 | 工具面 | 典型任务 |
|---|---|---|---|
| `research` | 调研/比较/收集公开资料，且没有打开浏览器、点击、Tab、滚动、返回或确认要求 | 只读 `research_github_repositories` | 调研 LangGraph、CrewAI、PydanticAI |
| `browser` | 用户要求真实网页动作、动态页面、Tab、弹窗、确认或失败恢复 | 现有 `browser_action_batch` | #9、#13、#15、#16 |
| `hybrid` | 先收集资料，再要求浏览器验证或执行 UI 动作 | 分阶段切换 | 后续扩展 |

第一切片实现 `research` 和 `browser` 两条路径。`hybrid` 先由上层任务拆分，避免在同一轮同时暴露两套工具。

## Research Mode 第一版

`research_runtime.GitHubResearchClient` 使用 GitHub REST API 的只读端点，最多处理 5 个仓库、每个仓库最多 5 个 Open Issue。返回内容只包括：

- 仓库名称、公开 URL、Star、主要语言、最近 push 时间。
- README 中有限的 Installation 命令。
- MCP、Memory、Multi-Agent、Tool Calling 的 `yes/no/unknown`。
- Open Issue 标题。
- `BrowserEvidenceLedger` 兼容的脱敏证据记录。

原始 README、API 错误正文、查询参数、Token 和 Cookie 不进入模型结果或报告。能力字段没有正向/负向证据时保持 `unknown`。

## 本地快速使用

命令行入口不启动 Chromium，适合先验证研究链路和代理连通性：

```powershell
python tools/github_research.py `
  --repo langchain-ai/langgraph `
  --repo crewAIInc/crewAI `
  --repo pydantic/pydantic-ai `
  --include-issues `
  --field stars `
  --field language `
  --field updated_at `
  --field installation `
  1>$env:TEMP\deskorb-research.json
```

运行时会沿用 `DESKORB_AGENT_API_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`；公开仓库不需要 Token，若配置 `DESKORB_AGENT_GITHUB_TOKEN` 只用于 GitHub API 请求头，不会写入证据账本。Agent 内部对没有浏览器/UI 要求的框架调研自动暴露同一个只读工具；包含打开网页、Tab、滚动、确认或失败恢复的提示词仍路由到 Browser Mode。

## 为什么第一版不直接嵌入完整开源 Agent 框架

项目已经拥有 `TaskPlan`、`AgentRuntime`、Browser Action 协议、确认状态和 `BrowserEvidenceLedger`。直接嵌入 browser-use 会产生第二套浏览器控制面，直接嵌入 Crawl4AI 会引入额外浏览器/爬虫生命周期和更大的安全审计面；直接引入 LangGraph 则需要迁移现有任务状态和审批链。

因此第一切片只复用开源项目的公开协议/数据源，保持本地实现小而可审计。后续如果需要跨阶段持久化，再把当前 `TaskPlan`/`InMemoryTaskJournal` 适配到 LangGraph checkpoint，而不是同时运行两套执行器。

## 验收边界

- #12/#17 的真实 Browser Agent 验收仍必须使用真实浏览器，因为它们要测 Browser Action 以及页面证据。
- 普通用户的框架调研可以走 Research Mode，避免为每个仓库付出多轮 DOM/Tab 操作成本。
- Research Mode 不是 Browser 验收的替代品，也不能用于登录、提交、购买、上传、验证码或任何副作用动作。

## 后续切片

1. 为 Research Mode 增加通用官方文档/Markdown 来源适配器。
2. 增加 `BrowserTaskCheckpoint`，在每个已验证框架后压缩上下文。
3. 为 #13/#15 增加受控中粒度工作流，内部仍逐个观察和执行状态动作。
4. 增加 `hybrid` 阶段切换和来源优先级验证。
