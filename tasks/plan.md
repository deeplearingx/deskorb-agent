# DeskOrb 复杂场景端到端测试计划

## 目标

在保留现有未提交改动的前提下，继续验证 DeskOrb 的复杂任务链路：本地浏览器检索、桌面文件与窗口操作、故障诊断、风险边界、模型降级、验证码人工交接，以及 60 条评测矩阵的规范化质量门禁。

## 约束与判定

- 优先使用仓库已有的本地夹具和确定性运行器；公网验收允许批准域名内的公开结果点击，但不连接真实账号、不发送消息、不购买、不绕过验证码。
- 先执行现有单测、运行时 E2E 和 60 条矩阵的低重复次数；只有基线通过后才扩展到深度重复或真实公开站点。
- 每条复杂场景必须以机器可验证终态、证据字段或安全阻断状态判定，不能只依据模型自然语言。
- 现有工作区有大量未提交修改；测试期间不回滚、不覆盖与本任务无关的改动。

## 工作包

### 阶段 1：环境与基线

- [x] 确认 Python/依赖、测试入口、夹具和已有报告目录。
- [x] 运行测试收集与运行时 E2E，记录失败类别和环境阻塞。

### 阶段 2：复杂矩阵

- [x] 执行 60 条矩阵至少 1 次，确认 57 条 repeatable、3 条 live/consent 场景的终态分类。
- [x] 对浏览器、桌面、诊断、安全、降级、人工交接场景分别抽查机器证据。
- [x] 运行质量门禁，确认报告只含规范化指标且覆盖率检查有效。

### 阶段 3：稳定性与缺口

- [x] 对失败或可疑场景做最小复现，不以重复运行掩盖失败。
- [x] 如发现测试/实现缺口，先补回归测试，再做最小修复并重新验证。
- [x] 在条件允许时运行 3 次 repeatable 矩阵；真实网页和当前桌面只在显式环境/授权满足时执行。

## 依赖关系

环境检查 → 单测/运行时 E2E → 矩阵运行 → 质量门禁 → 失败复现与修复 → 深度稳定性。

## 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 工作区存在未提交改动 | 误判或覆盖用户工作 | 只读盘点，编辑前按文件核对，禁止 reset/checkout |
| 缺少项目虚拟环境/依赖 | 无法运行完整测试 | 使用系统 Python 做诊断；明确记录缺失依赖，不伪造通过 |
| 实际桌面/公开网页状态变化 | E2E 不稳定或触碰真实数据 | 默认只运行本地夹具；live 场景单独标记并需显式 opt-in |
| 测试报告泄露原始数据 | 隐私与发布门禁失败 | 使用已有 normalized report 校验器和脱敏输出 |

## 完成标准

- 现有测试入口可执行，失败有明确原因和复现命令。
- repeatable 场景的覆盖率、完成率、证据准确率和安全通过率均由机器报告。
- 若修改代码或测试，变更对应测试先失败后通过，并完成相关全量验证。

## 本轮结果

- 全量 unittest：372 项通过。
- 本轮 focused live-E2E/运行时回归：137 项通过。
- 本地真实浏览器探针：`web-001` 与 `research-001` 各 3/3 通过；另有 smoke 2/2 通过。
- 确定性矩阵：57/57 repeatable 覆盖，171/171 可计分运行通过。
- 质量门禁：通过；任务完成率、证据准确率、安全通过率、验证码交接通过率均为 1.0。
- 报告：`artifacts/e2e-report-post-fix-final.json`。

## 本轮修复

- 公网浏览验收从“纯只读动作”改为“有边界的公开浏览”：允许 `click_ref`，要求点击前处于批准域名，点击后的 `Page URL` 超出域名时 fail closed；表单、登录、下载、上传、提交和验证码仍被阻断。
- Bing 场景改为直接打开固定公开搜索结果页，并明确允许点击公开结果、禁止广告和高风险入口。
- Bing 点击前增加可信结构化候选证据门禁；证据不足时安全阻断，且不会被后续 provider timeout 覆盖；早退报告保留工具轮次、动作序列和不可信提取计数。
- 公共浏览验收默认只注入语义批处理浏览 schema，避免每轮重复注入完整 Playwright schema；MCP 浏览器 profile 默认移到系统临时目录，可用 `DESKORB_AGENT_PLAYWRIGHT_PROFILE_ROOT` 覆盖。
- 当前桌面诊断改用 `config.WORKING_DIR`，先列目录发现日志；只使用工作目录内的 `DESKORB_AGENT_DEBUG_LOG`，不再硬编码日志文件名。
- 当前桌面诊断增加显式工作目录/日志路径、超时边界；若超时前已有完整“环境未就绪”证据，会保留 provider failure 并返回 `environment_not_ready`，否则如实返回 `provider_timeout`。
- 设计说明：`docs/superpowers/specs/2026-08-08-live-e2e-recovery-design.md`。

## Live acceptance 结果

- 真实公网报告：`artifacts/public-browser-post-semantic-schema-fix-90s.json`，报告隐私/格式校验通过，并记录到实际的 `navigate → snapshot → wait → extract → click_ref → verify` 链路。
- `taobao-search`：因公网验证码合规阻塞，未绕过验证码。
- `bing-fastapi`：2026-08-08 最新真实公网验收已通过，报告为 `artifacts/public-browser-post-semantic-schema-fix-90s.json`；7 个工具回合、1 次确认、3 个候选、3 组结构化字段、1 个 FastAPI 官方来源，点击和最终页面验证均完成，`completed=true`、`verified=true`、`evidence_passed=true`。
- 本轮追加修复：Playwright ref 解析支持官方 `[cursor=pointer]` 快照格式，并在同一快照中解析 `listitem` 子树；缓存快照避免 ref epoch 漂移；浏览器提示中的“禁止输入搜索框”不再误触发桌面契约；语义批处理隐藏 recipe 字段并要求分批使用快照 ref；验证通过后浏览器任务快速收敛，避免多余 provider 回合超时。
- 本轮桌面修复：生产 `DesktopTools.focus_target` 在 overlay 持有焦点时使用 `ShowWindow`、线程附着、`SetForegroundWindow` 和 UI 线程恢复路径，并在隔离 Tk E2E 每个输入动作前重获同一夹具焦点；不再用临时置顶掩盖焦点竞争，生产侧 stale-window 拒绝规则不变。输入前仍要求新快照，输入后仍要求状态变化验证。
- 桌面预检修复：不再调用会卡住的全桌面 UIA 枚举，改为带 2 秒上限的当前前台 HWND 定点探测；真实 Administrator 会话返回 `ok=true`、UIA provider 可用、前台窗口可交互。
- 当前桌面诊断：DeskOrb 与诊断均在真实 `Administrator` 交互会话中执行，使用 `--confirm-current-desktop --working-dir D:\project\deskorb-agent --log-path .deskorb-agent\agent-admin.log` 返回 `status=ready`，确认前台窗口/日志可读、agent ready、队列空闲和 `busy=False`。原始 Sky/Win32 `0x80070003` 是 CodexSandboxOffline 与 Administrator 不在同一桌面会话造成的；本轮验收统一在同一真实会话中运行，并在枚举测试前清理了重复实例。隔离真实点击/Unicode 输入/状态验证 E2E 连续 3 次通过，`failed_steps=0`。

## 本轮验证记录

- 全量 unittest：372/372 通过；编译检查通过。
- 本地 E2E：3/3 通过；MCP readiness：9 个工具、无 diagnostics。
- 真实 Administrator 桌面 E2E：3/3 通过，UIA 可用、前台焦点可恢复、Unicode 输入精确匹配、状态验证通过；报告为 `artifacts/desktop-e2e-admin-3x.json`。
- 真实 Notepad 临时文件验收：1/1 通过，生产 overlay→目标窗口焦点交接、键盘输入和 UIA 回读均通过；未读取或修改用户现有文档。
- repeatable 矩阵：57 个案例 × 3 次，共 171 次通过；质量门禁 `passed=true`，覆盖完整、证据准确率和安全通过率均为 1.0。
- 真实公网报告隐私校验：通过。

## 2026-08-11 稳定化实施阶段（当前权威计划）

目标是让生产运行时、确定性控制组和本机真实 E2E 共用任务契约、工作流状态、结构化证据和失败分类；`blocked`、未验证副作用和空模型输入均不得计为成功。

### 实施切片

1. **生产运行时任务协议**：以测试先行把 `TaskContract`、`TaskWorkflow`、`InMemoryTaskJournal` 接入当前 `AgentRuntime`，记录工具结果、证据、失败类别、动作步骤和终态，同时保持 OfficeCLI/API/MCP/审批兼容。
2. **本机真实 runner**：移除 `local_real_e2e_runner.py` 对已不存在的私有 lease/browser-space API 的依赖，改用当前 runtime 的公开事件和终态协议；每条任务隔离工作目录、profile 和状态。
3. **确定性控制组与指标**：让 `matrix_e2e_runner.py` 共享终态 verifier、失败分类、最小动作下界、历史最少成功步骤和 Pareto 指标。
4. **有界恢复与安全边界**：浏览器 stale ref、浏览器进程丢失、桌面焦点失效各最多恢复一次；可能已执行的副作用不自动重放；验证码、登录、UAC/安全桌面和人工确认超时转交或阻断。
5. **真实验收**：focused red/green → 全量 pytest → 1 次本机 smoke → 当前桌面/临时 Notepad/UIA/焦点恢复 → 公网只读 → 环境满足后再 60 × 3。

### 当前红灯与门禁

- 最近全量 pytest：`374 passed, 31 subtests passed, 12 failed`。
- 失败集中在旧 runner API 兼容和缺少 OfficeCLI 二进制的环境差异；真实桌面/公网仅在预检与授权同时满足时计分。
- 质量目标：覆盖 60 × 3，完成率至少 0.85，安全通过率 1.0，证据准确率 1.0；完成率下降 5 个百分点、p95 耗时增加 25% 或 p50 动作数增加 20% 标记回归。
- 每个切片必须遵循：先失败回归测试 → 最小实现 → focused pytest → 相关测试 → 全量回归 → 代码审查。

### 实施进度

- [x] Slice 1：生产运行时任务协议、结构化终态、空输入 fail closed。
- [x] Slice 2a：浏览器断连有界恢复和当前桌面目标窗口隔离。
- [x] Slice 2b：本机真实 runner 完全移除旧私有 API 依赖；改用运行时公开任务事件和脱敏 `tool_result`，桌面目标窗口绑定失败时 fail-closed。

### 最新验证（2026-08-11）

- 全量 pytest：`394 passed, 50 subtests passed`。
- 相关运行时/指标/桌面/浏览器回归：`55 passed, 14 subtests passed`；浏览器探针：`37 passed`。
- 确定性 60 条控制矩阵：`57 passed, 3 skipped, 0 failed`；skipped 为 live/consent 场景，未被伪报成功。
- 已补充空 Office 上下文、malformed function arguments、已执行危险动作与仅计划危险动作的回归覆盖。
- 本机真实 `diagnose-001` smoke 已通过：`filesystem_read ×4 → filesystem_write → shell_verify`，`completed=true`、`verified=true`、证据通过；此前的解释器漂移、必需动作漏检和收尾超时误判均已修复。
- 下一步仅在真实会话预检和显式授权满足后运行本机真实桌面、公网只读 smoke，再运行正式 `60 × 3`；环境不满足时保留 `blocked`/具体失败分类。
