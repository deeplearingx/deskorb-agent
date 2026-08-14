# DeskOrb 比赛展示与验收脚本

## 一句话定位

DeskOrb 是一个“可验证、可恢复、可人工接管”的 Windows 工作代理，而不是只会
宣称任务完成的聊天机器人。LLM 负责提出语义步骤；运行时负责权限边界、页面引用
生命周期、结构化证据和最终验收。

## 现场固定流程

每个浏览器任务都按同一条可观察链路演示：

```text
输入任务 → 一次任务确认 → starting → visible → ready
→ 搜索/点击/切页 → running → 结构化提取 → verifying
→ 来源、耗时、动作数和验证状态 → completed
```

只演示公开真实网站，不要求登录态，也不执行登录、验证码、上传、提交、购买或
发布。建议每个场景连续运行 3 次，并记录 `task_progress`、`tool_result` 和
`browser_status` 中的结构化字段，不记录 URL 参数、网页正文、凭据或工具参数。
其中 `elapsed_ms` 可直接用于统计首次 `visible`、首次 `running` 和最终
`completed` 的 p95。

| 场景 | 用户任务 | 必须展示的证据 |
| --- | --- | --- |
| Python 文档 | 在 `docs.python.org` 搜索指定技术主题，打开正确章节 | 标题、章节、来源链接，`verify.passed=true` |
| arXiv | 在 `arxiv.org` 搜索指定主题并打开一篇结果 | 标题、作者、来源链接，提取字段与验证字段一致 |
| 中文维基 | 在 `zh.wikipedia.org` 站内搜索并打开目标页面 | 经过验证的摘要和来源链接 |

现场目标：首次可见 p95 ≤ 12 秒，首次有效动作 p95 ≤ 25 秒，正常任务总耗时
p95 ≤ 60 秒；证据准确率和安全通过率 100%，无证据完成声明、重复副作用和
`unknown` 失败。

## 三项技术差异

1. **可信执行**：快照只用于定位；最终结果必须经过结构化提取和独立验证，模型
   自然语言不能替代成功证据。
2. **有界恢复**：过期引用、页面重渲染、新标签页、无进展和超时进入明确状态；
   最多重新观察或重连一次，绝不自动重放可能已经生效的点击或输入。
3. **跨域安全**：网页内容是不可信数据，不能扩大文件、桌面或后续工作流权限；
   只有验证后的字段才能写入文件或交给桌面适配器。

## 消融对比

用同一批页面和同一模型，对比“普通模型直接调用浏览器工具”和“DeskOrb 可信
执行层”。每次只改变执行层，页面、任务和超时预算保持不变。

| 故障注入 | 普通直连 | DeskOrb 记录项 |
| --- | --- | --- |
| 页面重渲染后继续使用旧 ref | 是否误点/误报成功 | `browser_stale_evidence`、重新观察次数 |
| 无进展按钮 | 是否重复点击 | `browser_no_progress`、是否安全阻断 |
| `target=_blank` 新标签页 | 是否停留在旧页 | `switch_tab`、来源链接准确率 |
| 工具调用超时 | 是否重放副作用 | `tool_execution_timeout`、重复动作数 |

报告至少包含完成率、重复动作数、错误成功声明数、证据准确率和安全通过率；把
无进展场景的“安全阻断”计为预期结果，而不是失败。

## 当前落地进度（2026-08-14）

浏览器稳定化已经从协议修复推进到多步骤恢复状态机：

- provider 将 `switch_tab` 参数展平到动作顶层时，运行时会补齐当前
  `observation_id`；每个导航、点击、输入、等待或切 Tab 后都必须重新观察。
- MCP 断连和工具超时分别记录为 `browser_mcp_connection_failed` 与
  `tool_execution_timeout`，只允许一次重新连接/重新观察；再次失败记录
  `browser_mcp_recovery_exhausted` 并阻断，不重放可能已经生效的动作。
- 当前 Slice 2 focused 回归为 `160 passed, 37 subtests passed`，完整回归为
  `711 passed, 77 subtests passed in 32.15s`。真实
  `books.toscrape.com` 语义探针已通过：`navigate → snapshot → click_ref → snapshot`、
  `startup_visible=true`、4 个动作步骤、约 11.7 秒，结束后无残留 Node/Chromium/MCP
  进程。脱敏报告见 `artifacts/public-books-browser-action.json`。
- Bing/FastAPI 的真实模型场景曾完成浏览器动作后以
  `provider_timeout_after_tools` 结束；这被归类为模型/供应商预算问题，不冒充浏览器
  失败或任务完成。下一步先补动态 DOM、旧 ref、窗口不可见和 MCP 进程退出夹具，再重跑
  真实网站 3× 验收。

答辩现场仍只展示“有证据的成功”：若网络、验证码或模型超时，展示明确的
  `blocked/failed` 状态或同一真实网站的预录运行，不用本地夹具伪装线上成功。

## 叙事边界

Office、会议纪要和桌面控制只作为应用广度展示，不与浏览器主线争夺答辩时间。
跨域和桌面适配器在真实 runner 闭环前，不宣称为“已完全落地”。网络异常时使用
同一真实网站完整运行的预录制视频作为备用，不用本地夹具伪装成线上成功。
