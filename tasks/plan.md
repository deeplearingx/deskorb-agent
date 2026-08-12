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
