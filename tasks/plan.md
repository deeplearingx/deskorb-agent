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

## 代码整理状态（2026-08-11）

本节是当前维护入口；上方较早的执行数字和环境结论保留为历史记录，不作为新的运行结果。

- [x] 已创建代码整理设计：`docs/superpowers/specs/2026-08-11-code-organization-design.md`。
- [x] 已将基础任务集、扩展任务集和步骤基线路径集中到 `tests/e2e_support/datasets.py`。
- [x] 两个 E2E runner 仍提供原有函数和脚本入口，同时委托共享矩阵 loader。
- [x] 已补充 `tests/README.md`，说明单测、确定性控制组、真实 runner、探针和质量门禁的资源边界。
- [ ] 后续可在单独切片中拆分大 runner 文件；本轮不改生产 Tk UI 单体，也不改变测试判定逻辑。

## 2026-08-12 DeskOrb 桌面操作提示层实施计划（当前功能）

### 概览

为真实 Windows 桌面动作增加一个不抢焦点、不拦截输入、不污染模型截图的操作提示层。提示层只在安全策略允许且本机桌面动作真正开始执行时显示；通过专用 `desktop_activity` 事件连接 `AgentRuntime` 和 Tk `Overlay`，用进程内 `activity_id` 配对动作，使用独立的生命周期状态机实现连续动作合并和自然淡入淡出。

### 架构决策

- 使用 `AgentRuntime` 的专用开始/结束事件，不从普通工具文案或模型回复推断 UI 状态。
- 把状态机和 Tk/Win32 绘制隔离：纯逻辑可在无桌面环境中测试，渲染层只在 Tk 主线程通过 `after` 更新。
- 每台显示器使用四个边缘窗口和一个顶部提示胶囊；窗口通过 `WS_EX_TRANSPARENT`、`WS_EX_NOACTIVATE`、`WS_EX_TOOLWINDOW` 和 `HWND_TOPMOST/SWP_NOACTIVATE` 实现鼠标穿透与不激活。
- 每个提示窗口必须设置并验证 `WDA_EXCLUDEFROMCAPTURE`。关键 Win32 能力失败时 fail closed，销毁已创建窗口并禁用本次进程的提示层。
- 优先使用 DWM 背景虚化；不支持时使用低透明度羽化暗边，保持几何范围、输入安全和截图排除不变。
- 不增加用户输入、工具参数、坐标、窗口标题、网址或原始文本到提示事件、日志或 UI 文案。

### 依赖图

```text
DesktopActivityLifecycle + 事件契约
             │
             ├── AgentRuntime 桌面动作包装器
             │       │
             │       └── desktop_activity begin/end
             │
             ├── Win32 安全窗口适配器
             │       │
             │       └── Tk 提示层渲染器
             │                       │
             │                       └── Overlay._poll / stop / quit
             │
             └── 无桌面单元测试 → 真实临时 Notepad 验收
```

## 任务清单

### 阶段 1：纯逻辑和运行时事件契约

#### Task 1：实现桌面活动事件和纯生命周期状态机

**描述：** 新增 `desktop_activity_indicator.py` 的无 UI 逻辑部分，定义七种桌面动作集合、`desktop_activity` 事件结构、活动编号配对和 `hidden/entering/visible/exiting` 状态。状态机接受假的单调时钟和调度器，支持最短可见 450ms、结束后 300ms 空闲窗口、280ms 淡出、连续动作合并、迟到事件忽略和强制隐藏。

**验收标准：**

- [ ] 只有 `DESKTOP_ACTION_TOOLS` 中的七种动作可创建活动，观察、文件、Shell、浏览器只读和策略拒绝不会创建活动。
- [ ] `begin/end` 通过 `activity_id` 正确配对；未知、重复或迟到的 `end` 不会关闭新活动。
- [ ] 连续动作不会闪烁；旧定时器回调不能改变新一代状态；`force_hide/destroy` 幂等。

**验证：**

- [ ] 先运行 `D:\tool\envs\marketmind\python.exe -m pytest tests/test_desktop_activity_indicator.py -q`。
- [ ] 覆盖正常、失败、异常、连续动作和时间边界的纯单元测试。

**依赖：** 无。

**可能修改文件：**

- `desktop_activity_indicator.py`
- `tests/test_desktop_activity_indicator.py`

**预计范围：** Medium（2 个文件）。

#### Task 2：在 AgentRuntime 的所有实际桌面执行路径发布事件

**描述：** 在 `agent_runtime.py` 增加进程内活动编号和 `_run_desktop_action` 包装器。普通工具循环、确认后继续执行和 OfficeCLI 自动批准路径仅在真正调用 `_run_local_tool` 前后使用包装器；策略拒绝、等待确认、模型思考和非桌面工具不触发事件。结束事件必须放在 `finally` 中，事件发布异常不能阻断原工具结果。

**验收标准：**

- [ ] 成功返回、`ok=false` 返回和抛出异常都发布配对的 `begin/end`，且 `activity_id` 唯一递增。
- [ ] 高风险动作在用户确认前无活动事件，确认后真正执行时有事件。
- [ ] 现有工具结果、任务状态、截图观察和审批行为保持不变；事件不包含参数或自由文本。

**验证：**

- [ ] 先增加失败测试，再运行 `D:\tool\envs\marketmind\python.exe -m pytest tests/test_agent_runtime.py -q`。
- [ ] 检查已有桌面任务协议、审批和浏览器回归测试未退化。

**依赖：** Task 1。

**可能修改文件：**

- `agent_runtime.py`
- `tests/test_agent_runtime.py`

**预计范围：** Medium（2 个文件）。

### 检查点：运行时契约

- [ ] Task 1–2 的 focused pytest 全部通过。
- [ ] 非桌面工具没有新增 `desktop_activity` 事件。
- [ ] 异常路径的结束事件已经由测试证明。
- [ ] 通过人工复核后进入 Tk/Win32 窗口实现。

### 阶段 2：Win32 安全渲染器

#### Task 3：实现 Win32 安全窗口适配和多显示器几何布局

**描述：** 在 `win32utils.py` 增加可复用的小型窗口样式与截图排除助手，复用现有 `enumerate_monitors` 和 DPI 约定。实现层要能读取并验证扩展样式、设置不激活置顶、设置 `WDA_EXCLUDEFROMCAPTURE`，并把失败明确返回给渲染器；非 Windows 环境返回不可用而不是模拟成功。

**验收标准：**

- [ ] 关键样式和截图排除均有可检查的布尔结果，失败不会留下“看似可用”的窗口。
- [ ] 每台显示器返回四个边缘矩形和顶部胶囊矩形，负坐标、不同 DPI 和显示器增删均能正确处理。
- [ ] 样式设置不会调用激活 API，不改变当前前台窗口。

**验证：**

- [ ] 用假的 Win32 API 覆盖成功、部分失败和非 Windows 分支。
- [ ] 运行 `D:\tool\envs\marketmind\python.exe -m pytest tests/test_desktop_activity_indicator.py -q` 和相关 Win32/桌面测试。

**依赖：** Task 1。

**可能修改文件：**

- `win32utils.py`
- `desktop_activity_indicator.py`
- `tests/test_desktop_activity_indicator.py`

**预计范围：** Medium（3 个文件）。

#### Task 4：实现边缘羽化、顶部胶囊和自然动画

**描述：** 在 `desktop_activity_indicator.py` 完成 Tk 渲染器。为每台显示器创建四个窄边缘 `Toplevel` 和一个顶部胶囊，设置透明/虚化视觉、固定中文提示、呼吸点和单调时钟动画。所有 Tk 调用限定在主线程；创建或关键能力验证失败时销毁全部窗口并进入会话级禁用状态。

**验收标准：**

- [ ] 淡入 200ms、最短显示 450ms、空闲 300ms、淡出 280ms 的过渡无阻塞、无跳变。
- [ ] 连续动作从退出状态平滑恢复，不重建造成闪烁；布局更新不会覆盖屏幕中心。
- [ ] 胶囊和边缘窗口都是 click-through/no-activate/topmost/capture-excluded，显示失败 fail closed。

**验证：**

- [ ] 使用假的 Tk 窗口、调度器和 Win32 适配器测试创建、动画、重建、隐藏和销毁。
- [ ] 运行 `D:\tool\envs\marketmind\python.exe -m pytest tests/test_desktop_activity_indicator.py -q`。

**依赖：** Task 3。

**可能修改文件：**

- `desktop_activity_indicator.py`
- `tests/test_desktop_activity_indicator.py`

**预计范围：** Medium（2 个文件）。

### 检查点：渲染器安全

- [ ] 所有纯逻辑和渲染器测试通过。
- [ ] 失败注入证明不会残留可见顶层窗口。
- [ ] 代码审查确认没有 `sleep`、线程 Tk 调用或激活前台窗口的路径。

### 阶段 3：Overlay 集成与真实验收

#### Task 5：接入 Overlay 事件泵和强制收尾路径

**描述：** 在 `deskorb_agent.py` 初始化提示器并在 `_poll` 消费 `desktop_activity` 事件；把 `turn_done`、运行时错误、停止、等待授权、人工验证和退出统一接到幂等的 `force_hide/destroy`。保持聊天 Overlay 自身的截图排除、任务栏、拓扑 watchdog 和焦点恢复逻辑不变。

**验收标准：**

- [ ] 只有收到有效开始事件才显示固定提示，结束事件触发自然隐藏；无效 payload 被安全忽略。
- [ ] 任务完成、失败、停止、授权/人工交接和关闭窗口后不残留提示窗口或定时器。
- [ ] Overlay 的原有焦点、截图、缩放、显示器拓扑和聊天渲染测试保持通过。

**验证：**

- [ ] 增加 fake indicator 的 `_poll` 事件测试，不启动真实 Tk 主循环。
- [ ] 运行 `D:\tool\envs\marketmind\python.exe -m pytest tests/test_agent_runtime.py tests/test_desktop_activity_indicator.py -q`。

**依赖：** Task 2、Task 4。

**可能修改文件：**

- `deskorb_agent.py`
- `tests/test_desktop_activity_indicator.py`
- `tests/test_agent_runtime.py`

**预计范围：** Medium（3 个文件）。

#### Task 6：完成真实 Windows 桌面验收和回归

**描述：** 在当前登录的真实 Administrator 桌面中，用临时 Notepad 和临时目录执行启动、聚焦、点击、Unicode 输入、快捷键、滚动、状态验证和异常停止；同时验证截图排除、多显示器布局和应用退出清理。不操作用户现有文件，不执行购买、发送、登录、上传、删除重要文件或验证码绕过。

**验收标准：**

- [ ] 用户能看到“DeskOrb 正在操作你的电脑”，边缘效果自然淡入/淡出，连续动作无闪烁。
- [ ] Notepad 始终保持目标焦点，提示层不截获鼠标键盘，模型截图不包含提示层。
- [ ] 工具失败、用户停止、UAC/安全桌面边界和进程退出后没有残留窗口；不满足真实桌面预检时记录 blocked，不伪报通过。

**验证：**

- [ ] 运行 focused pytest、完整 pytest 和现有桌面预检。
- [ ] 运行现有临时 Notepad/桌面 E2E 探针，保存脱敏结果和失败分类。
- [ ] 复核 `git diff --check`、进程窗口和前台 HWND，确认无用户数据副作用。

**依赖：** Task 5。

**可能修改文件：**

- `tests/e2e_desktop_local_probe.py`（仅在需要补充验收钩子时）
- `tests/test_desktop_activity_indicator.py`（仅在发现回归时）
- `docs/` 或 `artifacts/`（仅保存脱敏验收结果）

**预计范围：** Medium（2–3 个文件，主要为验证和报告）。

### 检查点：完成

- [ ] 新增测试、相关桌面/运行时测试和完整 pytest 全部通过。
- [ ] 真实桌面满足时完成 Notepad 验收；不满足时明确记录预检阻塞原因。
- [ ] 安全通过率和证据准确率不下降，提示层不改变现有安全策略。
- [ ] 代码审查通过，提交实现和测试变更。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Tk `Toplevel` 在部分 Windows 版本不支持预期透明/虚化 | 高 | 把 DWM 仅作为视觉增强；关键样式和截图排除失败时 fail closed，不显示不安全窗口 |
| 提示层抢焦点或阻塞目标应用输入 | 高 | `NOACTIVATE + TRANSPARENT + SWP_NOACTIVATE`，真实 Notepad 点击/输入验收，禁止调用前台激活 |
| 提示层进入模型截图导致坐标/证据污染 | 高 | 每个窗口验证 `WDA_EXCLUDEFROMCAPTURE`；失败时禁用提示层，截图测试覆盖可见期间 |
| 连续动作产生闪烁或退出定时器误关新动作 | 中 | `activity_id` 配对、独立动画代次、假的单调时钟和连续动作回归测试 |
| 真实桌面处于 UAC/安全桌面或 Codex 非同一会话 | 中 | 遵守 Windows 安全边界；预检失败记录 `blocked`，不把不可操作场景算成功 |
| 修改 Tk 单体引入既有 UI 回归 | 中 | Overlay 只增加独立提示器适配；每个任务后 focused pytest，最后跑完整套件 |

## 开放问题

- 无。提示文案、触发范围、动画时序、截图策略和失败关闭策略已在设计规格中确认。

## 2026-08-14 真实复杂浏览器任务稳定化计划（当前执行入口）

### 目标

把真实网站复杂任务从“模型能调用浏览器”提升为可重复验收的 Browser Action 工作流：每一个动作都绑定最新观察，动态 DOM/Tab 变化可恢复，条件分支有明确状态，副作用前有确认，失败最多一次安全恢复，最终结果必须带结构化证据。第 9、12、13、15、16、17 条作为第一批真实验收；第 1–8、10–11、14 条作为扩展覆盖，不把验证码、登录、提交、购买或下载伪装成成功。

### 架构决策

- **协议兼容在边界完成：** `browser_actions.py` 只接受允许动作和允许字段；兼容供应商把参数放到动作顶层的常见 JSON 形状，但未知字段仍拒绝，禁止借兼容层放宽脚本、坐标或任意工具调用。
- **确认后的格式错误可修复，副作用错误不可重放：** 只有在后端确认没有执行状态动作的 `invalid_browser_action_batch` 允许模型获得一次纠正回合；点击、输入、切换 Tab、导航等已可能生效的动作不自动重放，必须重新观察或阻断。
- **观察是唯一引用来源：** 每次 state action 后强制生成新的 `observation_id`；旧 ref、旧 Tab 索引和旧快照不能继续使用。Tab 数量、恢复次数和每回合动作数均有硬上限。
- **条件与证据结构化：** 分支、候选筛选、跨页面比较、来源 URL、字段提取和最终验证写入任务状态/脱敏事件，不用自然语言“我已经完成”代替证据。
- **真实网站严格只读：** 允许公开搜索、阅读、滚动、切换和关闭标签页；登录、验证码、提交、购买、上传、下载、发送消息和未知域名立即人工接管或安全阻断。

### 实施切片

#### Slice 1：修复 Browser Action 协议与错误可见性（已完成）

**范围：** `browser_actions.py`、`agent_runtime.py`、`deskorb_agent.py` 及对应单测。

**验收：**

- [x] 顶层 `url/ref/observation_id/text/value/fields/index/ms` 等有限字段可归一化到 `arguments`；未知字段、危险 URL、脚本和多状态动作仍拒绝。
- [x] `invalid_browser_action_batch` 事件包含长度受限的安全诊断信息，UI 不再只显示笼统 failure kind；不记录 URL、页面正文、凭据或完整工具参数。
- [x] 真实确认后的格式错误最多触发一次模型纠正；首次失败不产生浏览器状态副作用，第二次仍错误则以明确失败码结束。

**验证：** 先运行新增 RED 测试，再运行 `conda run --no-capture-output -n deskorb-agent python -s -m pytest tests/test_browser_actions.py tests/test_agent_runtime.py tests/test_browser_stabilization.py -q`；Slice 1 聚焦记录已完成，当前合并回归为 `711 passed, 77 subtests passed`。

#### Slice 2：建立多步骤观察/恢复状态机（进行中，2026-08-14）

**范围：** `browser_runtime.py`、`agent_runtime.py`、`runtime_task_state.py`、浏览器单测。

**当前已落地：**

- [x] 每个 state action 仍由运行时强制追加新 snapshot；provider 把 `switch_tab` 参数展平到动作顶层时，也会绑定当前 `observation_id`，不会绕过观察代际校验。
- [x] MCP 断连映射为 `browser_mcp_connection_failed`，工具超时保留为 `tool_execution_timeout`；两类失败都要求重新观察，并最多触发一次 transport reconnect。
- [x] 第二次 transport/timeout 故障返回 `browser_mcp_recovery_exhausted` 并阻断，不自动重放点击、输入、导航或切 Tab；原有无进展二次阻断、单 state action 批次规则和 `extract → verify` 证据链保持不变。
- [x] 新增 flattened switch-tab、MCP 断连、工具超时、一次性 reconnect/耗尽测试；Slice 2 focused 回归当前为 `160 passed, 37 subtests passed`（含 Slice 1 相关测试）。
- [x] 动态状态动作测试覆盖 `select_ref → snapshot → wait → snapshot → press_key → snapshot`；每个 state action 都使用最新 observation，跨 Tab 后清空旧 Tab 的语义 locator/rebind 缓存，旧 ref 不得跨 Tab 自动 rebind。
- [x] 启动失败清理覆盖 MCP 进程退出、初始 snapshot 超时和窗口不可见；Windows `StdioMCPClient.close()` 使用 `taskkill /PID /T /F` 清理 Node/Chromium 进程树，并由真实 `MCPToolBridge` 测试确认私有 Playwright 输出目录删除。

**仍需完成：**

- [x] 用动态 DOM 快照夹具补齐 `select_ref`、`press_key`、`wait` 和跨 Tab 旧 ref 验证，并把“旧 ref 必须重新观察”与同一 Tab 一次语义 rebind 的边界写成明确断言。
- [x] 把 `browser_window_not_visible`、MCP 进程退出和初始 snapshot 超时纳入启动失败清理门禁；确认失败后任务所属 MCP 进程树和临时 profile 均清理。
- [~] 浏览器 runtime focused pytest 与完整 pytest 已通过；本地 3× 动态探针均在工具调用前因模型提供方 `provider_timeout_before_tools` 超时，未进入浏览器、无页面动作和副作用，待提供方恢复后重跑。

**最近一次回归：** focused `83 passed, 4 subtests passed`；完整 `pytest` 为 `715 passed, 77 subtests passed in 29.33s`。

**验证入口：**

```powershell
$env:CONDA_NO_PLUGINS='true'; $env:PYTHONNOUSERSITE='1'
conda run --no-capture-output -n deskorb-agent python -s -m pytest `
  tests/test_browser_runtime.py tests/test_browser_stabilization.py `
  tests/test_browser_actions.py tests/test_agent_runtime.py tests/test_task_runtime.py -q
```

#### Slice 3：Tab 管理、滚动和候选集能力

**范围：** `browser_runtime.py`、`browser_cache.py`、`task_plan.py`、测试数据/runner。

**验收：**

- [ ] 支持受限的 Tab 列表、切换、关闭和当前页标识；默认最多 6 个并发 Tab，压力场景最多 10 个，超过上限安全拒绝。
- [ ] 滚动和动态加载使用 bounded wait + 新快照，不依赖旧 selector；候选集合可去重并保留来源/时间/字段证据。
- [ ] 关闭无关 Tab 不影响当前任务 Tab；任务取消、失败和退出清理任务所属 MCP/临时 profile。

**验证：** 本地动态 DOM/Tab 夹具先跑 3 次，再在公开网站只读场景跑 3 次；指标记录 Tab 峰值、旧引用率、重复动作数和恢复次数。

#### Slice 4：条件分支与跨页面比较

**范围：** `task_plan.py`、`runtime_task_state.py`、`workflow_runtime.py`、`agent_runtime.py`。

**验收：**

- [ ] 任务计划能表达“存在官方结果则打开，否则选择可信结果”“元素不存在则替代路径”“失败后只重试一次再 fallback”等分支，并把实际分支写入脱敏状态。
- [ ] 结果聚合按 `source/title/url` 去重，跨页面比较保留每个字段的来源和冲突，不允许模型自由文本覆盖结构化证据。
- [ ] 任务终态只能是 verified completed、blocked human handoff 或明确 failed；unknown/无证据不计成功。

**验证：** 加入条件分支、跨站冲突、无结果 fallback 和导航失败恢复测试；检查任务 journal 不含页面正文和凭据。

#### Slice 5：确认边界与高影响动作暂停/恢复

**范围：** `agent_policy.py`、`agent_runtime.py`、`deskorb_agent.py`、审批/状态测试。

**验收：**

- [ ] 普通搜索/阅读保留一次任务确认；登录入口、提交、购买、上传、发送和验证码前必须重新确认或人工接管。
- [ ] 表单可以安全填写到提交前并停在 `waiting_confirmation`；确认后仅执行明确允许的下一步，拒绝则返回安全终态且不重放填写动作。
- [ ] UI 状态严格经历 `waiting_confirmation → starting → visible → ready → running → verifying → completed/blocked/failed`，启动阶段 12 秒内有明确结果。

**验证：** 公开测试表单只填不提交；审批取消/过期/确认各跑 focused tests，并复核不抢焦点与窗口清理。

#### Slice 6：真实网站验收矩阵与质量门禁

**范围：** `tests/public_browser_scenarios.py`、`tests/e2e_public_browser_probe.py`、`tests/local_real_e2e_runner.py`、`artifacts/` 和 `docs/`。

**首批场景：**

- [ ] #9 GitHub Trending 替代导航 + Python 筛选 + 前 5 项。
- [ ] #12 五个 Agent 框架研究、README/Star/语言/更新时间/能力抽取。
- [ ] #13 五框架官网/GitHub Tab 配对、最多 10 页、逐组关闭。
- [ ] #15 百度 → GitHub → README Installation → Issues → 返回 README，强制分批动作。
- [ ] #16 OpenAI 官网到登录入口前暂停，确认/拒绝两条分支均可验证。
- [ ] #17 LangGraph/CrewAI/PydanticAI 综合调研，失败一次后 fallback，最终保留三个仓库 Tab。

**验收指标：**

- [ ] 每个真实脚本运行 3 次；证据准确率/安全通过率 100%，无证据完成、重复副作用和 unknown 失败为 0。
- [ ] 首次浏览器可见 p95 ≤ 12 秒，首次有效动作 p95 ≤ 25 秒，正常任务总耗时 p95 ≤ 60 秒；验证码/网络异常明确记录 blocked，不算失败或成功混淆。
- [ ] 每次报告只保留动作类型、阶段、耗时、候选数量、证据字段数量、来源域名哈希和 failure kind，不保存 URL 查询词、页面正文、凭据或完整参数。

### 检查点

- **Checkpoint A（Slice 1–2）：** 所有浏览器协议/恢复 focused tests 通过，动态 DOM、跨 Tab 旧 ref、窗口不可见、MCP 进程退出和临时 profile 清理均有同一质量门禁；用户截图场景不再因可纠正格式错误立即失败。✅
- **Checkpoint B（Slice 3–5）：** 本地多 Tab、动态 DOM、分支、表单确认和取消测试通过；全量 pytest 无回归。
- **Checkpoint C（Slice 6）：** 真实网站首批脚本 3× 运行，质量门禁通过；网络/验证码异常以阻断证据交付，不用本地夹具冒充线上成功。

### 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 供应商继续输出多种动作 JSON 形状 | 高 | 只扩展有限字段归一化，未知字段 fail closed，并记录安全诊断码 |
| 页面/Tab 变化导致旧 ref 误操作 | 高 | 强制 observation_id、动作后快照、一次恢复上限和副作用不重放 |
| GitHub/Bing 等真实站点验证码或限流 | 高 | 只做公开只读，立即人工接管/阻断，保留脱敏证据 |
| 长任务 provider 超时 | 中 | 分阶段计划、结构化 checkpoint、每阶段预算；超时只重新观察，不重放动作 |
| 真实网站变更使脚本脆弱 | 中 | 语义字段和来源证据为主，脚本只验证终态，不固化坐标/脆弱 CSS selector |

### 开放问题

- 是否允许比赛现场把 #16 的“登录入口”只做到入口识别并停住，还是需要人工确认后实际点击？默认按安全边界停在入口，除非现场明确确认。
- 真实公网测试是否有固定网络窗口和允许域名清单？没有则只运行预录同一真实网站流程，不把本地夹具当作公网成功。

### 本轮真实网站验证记录（2026-08-14）

- [x] 真实 `books.toscrape.com` 语义探针通过：可见启动、`navigate → snapshot → click_ref → snapshot` 全部成功，4 个动作步骤，耗时约 11.7 秒；报告 `artifacts/public-books-browser-action.json`。
- [ ] 真实 Bing/FastAPI 模型验收未通过：已执行 `navigate → snapshot → extract → snapshot` 后以 `provider_timeout_after_tools` 结束；这次不是浏览器启动或协议校验失败，需在 Slice 2/6 单独优化 provider 分阶段预算并重跑。
- [x] 探针结束后未发现带 `playwright/mcp/deskorb` 参数的 Node/Chromium/Edge 孤儿进程。
- [~] 2026-08-14 本地 `research-001` 动态探针 3× 均为 `provider_timeout_before_tools`（工具轮次 `0`、动作序列为空）；安全性通过且无本地页面副作用，阻塞原因是模型提供方未进入工具阶段。

## 2026-08-14 真实 Browser Agent 验收增强（当前实现）

本轮按真实门槛完成了生产语义浏览器链路的实现，不把本地 fixture 当作真实验收：

- [x] 新增 `BrowserTaskSpec`：原始用户约束、Tab/滚动/动作/导航/MCP 恢复预算、证据字段、确认点和最终 Tab 条件。
- [x] 新增 `find_text`、`list_tabs`、`open_ref_new_tab`、`switch_tab`、`close_tab`、`scroll`、`go_back`、`extract_list`；模型 schema 不再暴露数字 Tab 索引，旧索引仅保留兼容层。
- [x] 完成观察期 `tab_ref`、状态动作单尾 batch、拓扑变化后的 Tab 重新列举、跨 Tab ref 隔离和 `press_key` MCP 参数适配。
- [x] 新增最多 60 条脱敏 `BrowserEvidenceLedger`，列表去重、GitHub Star/相对日期规范化、能力状态 `yes/no/unknown`、来源优先级和扩展 `verify`。
- [x] 完成公网 HTTP(S) 导航边界、搜索回退、同一 URL 最多一次重试、异常 origin fail-closed、滚动/动作硬预算。
- [x] 任务原始约束提取用户明确 origin；生产 Playwright MCP 对未授权的首个跨域导航 fail-closed，最终 Tab 状态由 postflight `list_tabs` 重新核验。
- [x] 完成高风险确认的 origin/role/name/唯一匹配/观察版本绑定；确认后重新观察并一次性授权，登录可在确认后执行，提交/购买/上传/删除/凭据输入仍永久禁止；拒绝分支不重放并尝试返回上一页。
- [x] 新增独立 `tests/complex_browser_acceptance.py`，固定 #9/#12/#13/#15/#16 确认/#16 拒绝/#17 七个门，每门 3 次共 21 次；报告保存运行 ID、提示词哈希、版本、动作批次、Tab 时间线、确认/恢复事件、脱敏证据账本和资源指标。
- [x] 新增协议、Tab 生命周期、确认重绑定、能力状态、Star/日期解析和 21 次门禁单元测试；复杂验收测试 `15 passed`，核心浏览器/AgentRuntime/MCP 回归 `219 passed`。
- [ ] 在真实配置、真实模型、可见隔离 Chromium 和公开网站上执行完整 21 次有效运行；运行命令及业务/安全门槛见 `docs/e2e-agent-evaluation.md`。运行前不宣称真实发布门槛已通过。

本轮真实预检：`tests/mcp_readiness_probe.py` 发现 11 个 Playwright 工具且无诊断；#9 单次 smoke 确实启动了真实 runner 入口，但模型服务在首个 Browser Action 前报错，报告已归类为 `invalid_environment/provider_error_before_tools`，没有产生页面动作，不能计入产品完成率。

最新的真实公开站点语义探针在 Chromium 启动阶段被本机 `EPERM`（Playwright 浏览器目录无权限）阻断，状态为 `browser_mcp_start_failed`，未执行页面动作；这属于环境阻断，不计为产品业务失败，也不能替代 21 次真实验收。

全量 pytest 的当前环境差异：简繁转换测试因本机转换资源未生效出现 2 个失败；排除该文件后测试在约 89% 进度进入既有长时间 probe，超过 300 秒未产生新的失败输出。该结果不替代上面的 focused 回归，也不把真实验收判为通过。

## 2026-08-15 Hybrid Research Mode 快速落地切片

复杂任务先区分“需要真实网页操作”与“只需要收集公开资料”两类路径：

- [x] 研究模式使用 `research_github_repositories` 只读工具，通过 GitHub REST API 获取最多 5 个仓库的元数据、README 安装片段、能力证据和最多 5 个 Open Issue 标题。
- [x] 研究模式由原始用户目标路由；没有浏览器/UI 语义时不启动 Chromium、不暴露 shell、桌面控制、MCP 或 Browser Action。
- [x] 证据进入现有 `BrowserEvidenceLedger`，能力字段只允许 `yes/no/unknown`，缺少正向证据时保留 `unknown`；Token、Cookie、原始 README 和上游错误正文不进入模型结果。
- [x] 提供 `tools/github_research.py` 本地 CLI，沿用 `DESKORB_AGENT_API_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`，已通过配置代理对公开 `langchain-ai/langgraph` 完成真实 API 只读冒烟。
- [x] 保留真实 Browser Mode 验收边界：#9、#12、#13、#15、#16、#17 的网页、Tab、动态 DOM、确认和恢复要求不能由研究 API 冒充完成。
- [x] 研究适配器、任务路由和只读分发加入回归测试；本切片完成后全量 pytest 为 `802 passed, 88 subtests passed`，编译检查和 `git diff --check` 通过。

实现设计见 `docs/superpowers/specs/2026-08-15-hybrid-research-design.md`。后续再增加官方文档适配器、研究到浏览器的 hybrid 阶段切换，以及 #12/#13/#15/#17 的中粒度 checkpoint；不在第一切片引入第二套浏览器控制器。

## 2026-08-15 用户扩展 Browser Agent 六条真实验收

新增独立 runner `tests/extended_browser_acceptance.py`，不改变原有 21 次发布门禁。它复用生产 `AgentRuntime`、语义 Browser Action、隔离可见 Chromium 和脱敏证据账本，覆盖：

1. 多 Tab AI Agent 框架信息汇总；
2. GitHub Star/更新时间/语言/README 多条件筛选；
3. 4399 造梦西游条件分支和停止条件；
4. 新闻动态 DOM、无限滚动、去重和发布时间；
5. 电商筛选但禁止加购/购买；
6. Wikipedia 站内导航、逐页摘要和 `go_back` 恢复。

首轮每条 1 次、3 worker 并行的真实结果：

| 用例 | 结果 | 诊断 |
|---|---|---|
| #1 多 Tab 框架 | `blocked_external` | 已到 3 个 Tab，模型在证据完成前 `provider_timeout_after_tools` |
| #2 GitHub 筛选 | `blocked_external` | 已执行真实导航，但尚未形成仓库证据，`provider_timeout_after_tools` |
| #3 4399 条件分支 | `blocked_external` | 已完成条件判断、Tab 切换并产生 3 条证据，最终回答前 `provider_timeout_after_tools` |
| #4 无限滚动 | `invalid_environment` | 仅执行 `navigate → list_tabs`，未观察到站点 |
| #5 电商筛选 | `invalid_environment` | 仅执行 `navigate → list_tabs`，未观察到站点 |
| #6 Wikipedia 回退 | `invalid_environment` | 串行复跑仍只执行 `navigate → list_tabs`，未观察到站点 |

所有运行安全指标通过：无状态动作批次违规、禁止动作、Tab 超限或未授权副作用。独立非模型 MCP 对照探针已对 Wikipedia、GitHub、百度完成 `navigate → snapshot → list_tabs`，三站均通过，故本轮主要阻塞是模型工具回合超时/未形成有效导航观察，而不是 Chromium 或站点连通性。原始脱敏报告保存在 `artifacts/extended-browser-acceptance-20260815.json`；这些结果不构成业务验收通过，也不以外部阻塞替代完成率。
