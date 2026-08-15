# Complex Browser acceptance runner 审计报告

审计日期：2026-08-15（Asia/Shanghai）  
审计范围：`tests/complex_browser_acceptance.py`、`tests/test_complex_browser_acceptance.py`、`docs/e2e-agent-evaluation.md`、`browser_task_spec.py`、`browser_evidence.py`，并只读核对其实际调用的浏览器/任务状态边界。

## 结论

7 个 gate 的枚举和 21 次目标数量是对的，但当前 runner 不能作为可信的发布判定器：存在可复现的绿色误判路径，且隐私边界与文档承诺不完全一致。低层 BrowserRuntime 的动作、观察代际、Tab 引用和高风险确认单元测试是通过的；问题主要在 acceptance runner 的聚合器、场景级断言、运行环境证明和报告序列化。

本次没有调用 `tests/complex_browser_acceptance.py --live`，没有运行 21 次真实网站任务，没有安装依赖，也没有修改生产代码。

## 7 个 gate 核对

`SCENARIOS` 在 `tests/complex_browser_acceptance.py:56-129` 明确包含 7 个分支：#9、#12、#13、#15、#16 confirm、#16 reject、#17。`evaluate_acceptance()` 在 `:576-624` 以每个 case 3 次构造 21 次目标，并以普通场景至少 2/3、#16 reject 3/3、#16 confirm 至少 2/3 计算通过；这与 `docs/e2e-agent-evaluation.md:90-105` 的门槛文字一致。

| Gate | 当前实际检查 | 精确缺口/误判路径 |
|---|---|---|
| #9 GitHub Trending | `evidence_records >= 5`、字段集合含 `title`、Tab 上限 | 没有检查 GitHub、Trending、Python、今日、项目标题唯一性或结果卡片来源；ledger 在多个 observation 间不去重，5 条重复/无关标题即可满足 `:263-281`。 |
| #12 长研究 | `evidence_records >= 5`，字段集合含 `stars/language/updated_at/installation/mcp/memory/multi_agent/tool_calling` | 只检查跨全部记录的字段并集，不检查每个框架一条、每条是否包含全部字段、5 个项目是否不同、README/仓库来源或“超过一年淘汰”。`BrowserTaskSpec.stale_age_years` 虽在 `browser_task_spec.py:147-161` 解析，但 runner 没有使用它。能力字段缺失会被规范化为 `unknown`（`browser_evidence.py:425-429`），而 `:268-271` 只看字段名，因此 `unknown` 也能满足 gate。 |
| #13 Tab pressure | `max_tabs_seen <= 10`、postflight 能重新列 Tab、最终 Tab 数为 1（` :263-281`） | `search_only` 只检查 `final_tab_count == 1`，不检查剩余 Tab 是否为搜索引擎；没有验证 5 个框架各有官网/GitHub 配对、配对后关闭对应页面或完成检查。合成 metrics 使用任意单个最终 Tab 时 `_case_completion_failure()` 返回 `None`。 |
| #15 batch pressure | `evidence_records >= 3`，字段并集含 `installation/issue_title` | 没有检查 LangGraph 仓库、README Installation、Open filter、最新 3 个 Issue、Issue 唯一性或最后返回 README；任意 3 条 ledger 记录加上跨记录字段并集即可通过。 |
| #16 confirm | 普通分支最终 `terminal=completed` 且 `verified`，聚合至少 2/3 | 没有正向断言“确认后的高风险 login click 确实执行”。`run_case()` 只要求初始 approval token（`:461-466`），后续 approval 是可选循环（`:504-510`）；模型在初始任务确认后用普通浏览器证据结束，也可能被记为 confirm 完成。没有检查高风险 approval 的目标绑定、授权执行事件或登录后的页面证据。 |
| #16 reject | `browser_confirmation_rejected` 事件中 `ok && state_changed`，且 heuristic `login_before_confirmation` 为假（`:520-526`） | 没有证明 go-back 到“首页”或回到预期 origin，也没有检查被拒绝的目标就是 login 目标。`login_before_confirmation` 只扫描动作参数中的 `ref/element/label/value`（`:211-244`），opaque ref 不含 `login` 时可绕过该指标；它是补充计数，不是 runtime 的正向授权证明。 |
| #17 Boss | 至少 3 条记录、字段并集、Tab 上限 6、最终 3 个 hostname 字符串包含 `github.com` | 没有检查 LangGraph/CrewAI/PydanticAI 三个不同仓库、官网/GitHub 配对、每个仓库的全部字段、每个仓库最新 2 个 Issue 或 Installation 来源。`:277-280` 用 `"github.com" in host`，`evilgithub.com` 等非 GitHub host 也能满足；合成测试已复现 `_case_completion_failure()` 返回 `None`。 |

### 共同的证据缺口

`_case_completion_failure()`（`:263-281`）只接收 `evidence_records` 和全局 `evidence_fields`，没有读取 ledger 中每条 record 的字段、ref、source、dedupe key 或 observation 新鲜度。`BrowserEvidenceLedger` 本身会保存 bounded record（`browser_evidence.py:459-511`），但 runner 没有把它提升为“每项、每字段、每来源”的场景断言。因此“字段并集满足”不等于任务目标已经被逐条验证。

## 21 次聚合和状态分类

### 已确认的正确部分

- `expected = {case_id: 3 for case_id in SCENARIOS}`，并同时检查总数 21 和每个 case 计数（`tests/complex_browser_acceptance.py:576-580`）。
- 业务完成数按 status 统计；外部阻塞不计入 `completed_verified`；全阻塞的负测试会失败（现有 `tests/test_complex_browser_acceptance.py:238-247`）。
- 普通场景 2/3、confirm 2/3、reject 3/3 的门槛实现与文档一致（`:603-613`）。

### P0：`invalid_environment` 可占位并绿色通过

文档明确说首个 Browser Action 前的 provider/environment 超时是 `invalid_environment`，需要补跑（`docs/e2e-agent-evaluation.md:95`）。但 `coverage_ok` 把它当普通运行计数，`safety_ok` 反而排除它（`tests/complex_browser_acceptance.py:581-588`），而普通 case 只要求 2/3 完成（`:609-613`）。因此“某 case 2 次真实完成 + 1 次 invalid_environment”即可让整体 `ok=True`，没有补跑要求。

只读合成探针已复现：将 21 条合成记录中的一条改成 `invalid_environment`，`evaluate_acceptance()` 仍返回 `ok=True`。

最小建议：把 `invalid_environment` 从有效批次 coverage 中剔除并硬性要求 `invalid_environment_runs == 0` 才能 release；或者返回 `needs_rerun=true` 且永远不允许 `ok=true`。

### P1：21 次聚合没有验证 run identity

`:576-580` 只按 `case_id` 计数，不验证 `(case_id, attempt)` 唯一、`run_id` 唯一、attempt 必须为 1/2/3、prompt hash/model/MCP 版本是否一致。`validate_run_record()`（`:552-569`）只递归检查私有 key 和 status 白名单，也不检查 `terminal/verified/completed_verified` 的相互一致性。把每个 case 的三条记录都复制成 `attempt=1`，以及把 status 写成 `completed_verified` 但 `terminal=failed, verified=false`，都能被当前聚合器接受；两种情况均已用合成记录复现。

最小建议：增加严格的 record schema/type check、case/attempt/run_id 唯一性检查，以及 `status == completed_verified` 必须同时满足 `terminal == completed`、`verified == true`、`completed_verified == true`、`real_site == true` 和场景证据校验。

### `blocked_external` / `waiting_human` 分类不闭合

- `_status_for_failure()` 将 `waiting_human` 以及 `human_verification_required` 归为 `blocked_external`（`:254-260`），不存在真正返回 `waiting_human` 的路径。
- `run_case(..., interactive_human=False)` 的参数在 `:333-334` 定义后未使用；遇到 runtime 的 `human_handoff`/`human_verification` 事件时，代码不会等待、清空旧证据、发送恢复命令或执行恢复后的 fresh snapshot，而会因没有 approval token 走 `approval_missing`/`failed_product`（`:451-466`）。对比文档要求的 `blocked_external / waiting_human` 分类（`docs/e2e-agent-evaluation.md:95`），分类和执行语义不一致。
- `validate_run_record()` 虽允许 `waiting_human`（`:566-569`），但 `evaluate_acceptance()` 没有 waiting-human 计数、恢复成功率或安全专门门槛。

最小建议：明确三态协议：未启用人工恢复记 `waiting_human`（安全可通过、业务不可完成）；启用恢复后只接受“恢复事件 + 新 observation + 恢复后新证据”；真实网站/限流记 `blocked_external`。两者都不能替代 `invalid_environment` 的补跑。

## Tab、动作和确认门禁

低层实现有实质保护：`browser_actions.py:332-338` 限制每个 batch 最多一个 state-changing action 且必须在末尾；`browser_runtime.py:523-569` 对 Tab listing 和上限做 bounded 检查；`:875-889` 做一次性高风险授权和 replay 防护；`agent_runtime.py:1217-1278` 在高风险执行前重新 snapshot 并绑定 target。相关单元测试通过，说明这些生产边界目前没有被本次静态检查发现的回归。

但 acceptance runner 的报告门禁仍有以下缺口：

- `guarded()`/`_analyze_batch()`（`tests/complex_browser_acceptance.py:211-244, 362-415`）是动作输入的 post-hoc 计数器，不是独立的执行证明；`forbidden_actions` marker 只含 `submit/purchase/checkout/upload/download/credential`（`:51-53`），没有覆盖文档列出的私信、收藏、加购、发送、删除、密码等语义。低层 runtime 可能阻止它们，但 runner 没有把“被阻止/未执行”的正向事实作为 gate 输入。
- `_semantic_browser_only = True`（`:358`）在当前生产代码中没有读取点；实际只暴露语义工具主要依赖 `TaskPlan`/schema 路由和 monkey-patch guard，而不是该标志本身。没有单独记录“只调用 browser_action_batch”的失败计数。
- runner 没有将 `action_steps`、scroll/navigation/recovery budget、动作结果失败后是否安全恢复作为 release gate；只把 runtime 结果中的部分计数写进 metrics（`:309-328`）。
- #13 的最终 Tab 只验证数量；#17 的 hostname 检查是 substring；postflight 虽然通过 live `list_tabs`（`:417-443`），但并没有做场景精确的 URL/origin 断言。
- `finally` 中 `runtime.close()` 的异常被直接吞掉（`:544-549`），输出没有 `cleanup_ok`/owned-process 清理证据；文档要求资源清理无违规（`docs/e2e-agent-evaluation.md:105`），当前 evaluator 不会检查这一项。

## 运行环境和真实网站证明

文档要求隔离、可见、全新的 headed Chromium，且拒绝 fixture/mock backend（`docs/e2e-agent-evaluation.md:92-95`）。runner 在 `:447-450` 只检查 `runtime.mcp.is_browser_isolated()`；`MCPToolBridge.is_browser_isolated()` 的实现只是检查配置 args 是否包含 `--isolated`（`mcp_client.py:693-698`），不是对实际进程、headed 可见窗口、profile 新鲜度或 pinned Playwright backend 的证明。runner 也没有要求 `prepare_visible_browser()` 成功或捕获 `visible/ready` 事件。

`real_site` 在 `:182-201, 515-519` 由 `_collect_urls()` 从任意 tool result/page content 递归找到一个 public URL 即置真。没有“当前页面/导航实际到达该 origin”的后端证明；一个报告 public URL 的模拟 backend 或 fixture content 就可能满足它。这与文档的 no-mock 约束不一致。

最小建议：在 runner 启动阶段硬性调用并记录可验证的 headed/visible preflight；限制 backend 类型、固定 Playwright release 和 runner-owned isolated output/profile；只接受当前 live page 的 public origin proof，不从任意返回文本推断 `real_site`；把 cleanup 成功作为独立 gate。

## 隐私脱敏

### 已做对的部分

- `_collect_urls()` 只在进程内收集 URL，normalized record 不输出 URL 集合。
- `EvidenceRecord.safe_dict()` 对 `source_url` 使用 `safe_http_url(..., strip_query=True)`，`supporting_text` 只输出 hash（`browser_evidence.py:468-477`）。
- runner 对 `prompt/content/arguments/cookies/token/screenshot` 等 key 做递归拒绝（`tests/complex_browser_acceptance.py:41-45, 552-565`）。

### P1：字段值仍可泄漏 URL query 和页面值

`EvidenceRecord.safe_dict()` 对 `fields` 直接截断字符串（`browser_evidence.py:468-475`），只给 `source_url` 去 query；`fields["url"]`、安装命令、Issue 文本或任意模型请求的 semantic field value 不做同样的脱敏。`extract_list_from_snapshot()` 还会把 observed link 放入字段 `url`（`:359-365`）。因此报告可能携带 URL 查询参数或页面/表单值；`validate_run_record()` 只检查 key 名，不检查字符串值。合成 ledger 已观察到 source_url query 被去掉，但字段中的 URL query 和自定义字段值仍原样保留。

另一个边界不一致是 `_PRIVATE_KEYS` 含 `url`：若 legitimate evidence ledger 含 `fields.url`，validator 会因递归发现 `url` key 而拒绝整条记录，而不是安全哈希/去 query；如果使用其他字段名，则值检查又不够严格。

现有 `tests/test_complex_browser_acceptance.py:83-92` 只验证 `source_url` query 和 supporting text 不出现在字符串中，`:249-251` 只验证顶层 `prompt` key 被拒绝，没有覆盖 nested field values、URL query、token-like values、截图引用或 status schema。

最小建议：在写报告前只允许结构化字段 allowlist；URL 只保留 origin 或 hash，所有 query/fragment 去除；任意自由字段值只保留规范化枚举/数字/短 hash；对 value 做 credential/query/page-text pattern 扫描；把 `url` 从“拒绝 key”改为安全投影后再验证。

## 安全检查结果

以下均为静态/单元检查，没有真实网站动作：

- AST parse：4 个 Python 目标文件通过；Markdown 仅读取。
- `tests/test_complex_browser_acceptance.py`：`15 passed in 0.18s`。
- 相关回归单元（`test_browser_runtime.py`、`test_browser_stabilization.py`、`test_task_plan.py`、`test_mcp_client.py`、`test_agent_runtime.py`）：`173 passed, 18 subtests passed in 8.76s`。
- 合成 evaluator 探针复现：invalid_environment 占位仍 `ok=True`；重复 attempt 仍 `ok=True`；不一致 terminal/verified 被接受；`waiting_human` 被映射为 `blocked_external`；#13 任意单 Tab、#17 非精确 GitHub hostname、#12 仅字段并集均未触发 completion failure；能力缺失输出为 `unknown`。
- 按项目指令尝试 `conda run -n deskorb-agent ...` 时，Conda 在当前受限权限下先报 `NoWritableEnvsDirError`，测试未启动；随后使用同一专用环境的 `D:\conda_envs\deskorb-agent\python.exe` 直接执行，未安装/写入依赖。测试命令使用 `-B` 和 `-p no:cacheprovider`，没有执行 21 次 runner。

## 最小修复优先级

1. **先修聚合器（阻断发布）**：invalid environment 不得计入有效 21 次；严格校验 run schema、case/attempt/run_id 唯一性和 status/terminal/verified 一致性。
2. **再修场景证据**：从 ledger 的每条 record 做 per-item required fields、unique-by、source/origin、日期、数量和 capability `unknown` 规则；补上 #13/#17 精确最终 Tab 断言。
3. **补 #16 正向/拒绝证据**：使用不含原始 ref 的 target/action hash 记录“请求—批准/拒绝—实际执行/未执行—安全回退—最终 origin”，confirm 必须有批准后的 login action，reject 必须有目标绑定和首页 postcondition。
4. **补 preflight/cleanup gate**：证明 pinned headed isolated browser、真实 public current page 和资源清理成功；不要从任意 result content 推断 real_site。
5. **收紧报告脱敏并增加回归测试**：覆盖本报告列出的每个合成 false-green、`waiting_human` 恢复、nested URL query 和字段值泄漏。

