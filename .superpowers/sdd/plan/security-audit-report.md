# DeskOrb 只读安全审计报告

审计日期：2026-08-15  
审计范围：浏览器动作 schema、AgentRuntime、BrowserRuntime、BrowserTaskSpec、BrowserEvidenceLedger、确认/恢复逻辑，以及 tests/complex_browser_acceptance.py 验收 runner。  
审计依据：项目现有实现、security-review 的输入/输出边界与敏感信息要求、agent-harness-construction 对窄动作空间、稳定结果、恢复契约和证据来源的要求。

## 执行边界与验证结果

- 未修改生产代码，未安装依赖，未启动 live browser runner，也未执行登录、提交表单、购买、上传、删除或输入凭据的动作。
- 只读检查了工作区已有的脏改动；这些改动在本审计开始前已存在，本次只新增本报告文件。
- 使用专用环境 D:\conda_envs\deskorb-agent 运行：

  conda run -p D:\conda_envs\deskorb-agent python -B -s -m pytest -q -p no:cacheprovider tests/test_mcp_security.py tests/test_browser_actions.py tests/test_browser_runtime.py tests/test_browser_stabilization.py tests/test_complex_browser_acceptance.py tests/test_agent_policy.py tests/test_agent_runtime.py tests/test_workflow_runtime.py

  结果：214 passed，33 subtests passed，12.23s。
- 另做了不启动浏览器的内存级探针：证实 EvidenceRecord.safe_dict() 会返回字段内的原始 URL 查询串和页面字段；证实嵌套 browser action 的未知参数会通过 validate_browser_action_batch()。
- 测试通过不代表以下边界已经安全：现有测试主要验证主流程、一次性确认和 query 从 source_url 移除，没有覆盖递归敏感字段过滤、userinfo、跨源 click_ref、精确 origin 比较、未知嵌套参数和确认分支的最终页面断言。

## 总结

没有发现必须命名为 Critical 的无条件利用链，但发现 6 项 High 和 3 项 Medium 风险。High 项集中在两类真实验收后果：

1. 页面/MCP 结果和 evidence ledger 在多个边界仍可能携带 URL query、页面字段、任意后端返回值；如果后端返回 headers、cookie、storage 或凭据字段，当前代码没有统一的递归脱敏边界。
2. 页面观察到的链接、控件启发式和验收 runner 的字符串/布尔判断可能允许越权导航、高风险动作漏拦截，或在实际目标/最终页面未被证明时给出绿色验收结果。

已有的有效控制包括：一次性、目标绑定的高风险确认；确认在 MCP 调用前消费；超时/连接失败路径要求重新观察；handoff 清空 observation、evidence 和授权；一个 batch 最多一个 state-changing action；直接 navigate 和 open_ref_new_tab 有公网 URL 检查。这些控制降低了风险，但没有覆盖下列具体缺口。

## Findings

### F-01 — High：complex acceptance report 重新暴露 evidence 字段、页面数据和字段内 URL query

风险：runner 的“metrics-only / privacy-safe”约束只保护了若干键名和 source_url，没有保护 evidence record 的字段值。验收 JSON 会把页面派生字段写入报告；字段可能是页面标题、摘要、链接或含 token/query 的 URL。页面正文虽被作为 supporting text hash 处理，但同一信息可能通过 fields 重新出现。

证据：

- tests/complex_browser_acceptance.py:407-415 从 live session 读取 ledger.safe_dict()，并把所有 record 的字段名汇总到 metrics。
- tests/complex_browser_acceptance.py:284-328 的 _normalized_run() 将 metrics["evidence_ledger"] 原样放入最终 run record；这与文件顶部“prompts, URLs, page text, arguments, cookies, screenshots never leave process”的声明不一致。
- browser_evidence.py:460-475 的 EvidenceRecord.safe_dict() 对 fields 只截断 key/value，不做 query、userinfo、cookie、token、凭据或页面内容脱敏。
- browser_evidence.py:480-512 的 ledger 只对 source_url 调用 strip_query=True；fields 仍由 normalize_evidence_fields() 直接保存。
- browser_evidence.py:443-455 的 list extraction 也会把 projected fields 原样放入 record。
- 现有 tests/test_complex_browser_acceptance.py:86-92 只断言 source_url 的 query 和 supporting text 不出现，没有断言 fields["url"] 或其他字段值不出现。内存探针实际得到的 safe dict 仍包含字段内的 ?token=... 和页面字段值。

最小修复建议：

1. 把“供验收报告使用的安全视图”与内部 evidence ledger 分开；报告 schema 只允许 record kind、字段名、字段数量、经过验证的 origin/path、observation id 的不可逆摘要和 supporting text hash，禁止任何原始 fields 值。
2. 若必须返回字段，使用递归 allowlist 和统一 sanitizer：URL 只保留 scheme/host/path，去掉 query、fragment、userinfo；对 key 和 value 同时检测 cookie、authorization、token、secret、password、credential、storage 等模式；未知字段默认丢弃。
3. 在 validate_run_record() 增加嵌套值级检查，而不是只检查 _PRIVATE_KEYS 的键名；补充字段内 URL、alternate key、嵌套 dict/list 的回归测试。

### F-02 — High：MCP/浏览器结果以任意字典形态穿过后端、模型 transcript 和 UI

风险：当前 content_trust: "untrusted_page_data" 只是语义标签，不是隐私过滤。后端返回值仍可直接到达模型 provider 的 function-call output，且 pre-dispatch UI event 还会收到完整 action arguments。若 MCP/浏览器适配器返回 cookie、headers、storage state、authorization、凭据或未清洗的错误，代码没有一个统一的递归脱敏出口。

证据：

- mcp_client.py:926 取出 result.get("content", result)；随后返回的 content 只是按字节截断，没有字段 allowlist 或敏感值递归过滤。mcp_client.py 的 MCPError 通路也会把普通异常文本作为 error 返回。
- browser_runtime.py:650-691 在 snapshot 成功后保留 raw content / raw result，并以 {**result, ...} 返回；browser_runtime.py 的 find_text、list_tabs、extract 等路径同样保留适配器返回字典的内容。
- agent_runtime.py:1285 和 1505 将原始 result JSON 放入 function_call_output，因此随后会被发送给模型；这发生在 _publish_tool_result() 的安全 UI telemetry 之外。
- agent_runtime.py:1443 把未清洗的 arguments 放入 self.ui.put(("tool", ...))。虽然 agent_runtime.py:784-833 的 tool_result telemetry 是有界且不含自由文本的，但早先的 tool event 仍持有完整参数。

最小修复建议：

1. 在 MCP client 到 BrowserRuntime 的唯一边界建立 runtime-owned serializer；只允许明确的结构化字段、有限长度的页面文本和有限的 link metadata，丢弃任意后端额外键。
2. 对所有递归 dict/list 做统一脱敏，至少覆盖 cookie、set-cookie、authorization、proxy-authorization、headers、storage、token、secret、password、credential、api_key、session；URL 去掉 query、fragment、userinfo。错误向模型和 UI 只暴露稳定的 failure_kind、safe retry 和 stop 指示。
3. transcript 和 UI 的 tool event 都只发送安全 action summary（action type、opaque ref/observation id 摘要、风险级别），不能在另一路把原始参数送出。用 fake backend 返回上述敏感键的测试验证 provider transcript、UI event、report 三条出口都没有原值。

### F-03 — High：click_ref 可用页面观察到的链接扩大 origin，绕过直接导航的公网/私网检查

风险：页面里的链接被视为“观察到”并不等于它是任务允许的、可访问的公共目标。一个页面可以提供 localhost、RFC1918、云 metadata、内部域名或任意外部域名链接。click_ref 在调用后端之前就把该链接 origin 加入允许集合，导致页面链接成为 scope expansion；真实 Playwright 路径没有看到与 direct navigate 同级的网络/DNS egress enforcement。

证据：

- browser_runtime.py:78-84 的 direct navigate 会调用 is_safe_public_browser_url()。
- browser_runtime.py:839-857 的 open_ref_new_tab 在 strict real backend 下也会调用该检查。
- 但 browser_runtime.py:858-869 的 click_ref 只解析 clicked_url/clicked_origin，随后无条件执行 _allowed_origins.add(clicked_origin)，没有先调用 is_safe_public_browser_url()，也没有要求该 origin 在 task spec 的 explicit allowlist 中。
- browser_runtime.py:666-670 的 post-observation 检查只把最终 origin 与已经被加入的集合比较；它不能证明该 origin 一开始就被授权。
- browser_task_spec.py:64-76 的任务契约明确要求页面观察不能扩大任务 origin；当前 click 分支与该契约不一致。
- browser_evidence.py:110-138 明确把 DNS rebinding 留给外部网络 sandbox，但本仓库的 Playwright isolation 配置不等同于 DNS/egress allowlist；因此该安全控制在应用层不可验证。

最小修复建议：

1. click_ref dispatch 前对 observed target 做与 navigate 相同的 public URL 检查，并按 task spec 的 exact origin/trusted-domain policy 决定是否允许；默认不因页面 link 自动扩大 allowlist。
2. 在修改 _allowed_origins 之前完成校验；导航后再次对最终 URL 的 scheme、host、effective port 做精确比较，并检查 redirect/连接目标。无法在应用层保证 DNS rebinding 时，在 browser/network boundary 施加 egress allowlist。
3. 增加 fake snapshot 测试：观察到 http://127.0.0.1/...、RFC1918、userinfo URL 和未声明 public origin 时，click_ref 不得调用 backend；跨源公共链接应由明确策略决定，而不是“一律允许”。

### F-04 — High：高风险/禁用动作主要依赖控件文字启发式，通用控件可漏过确认

风险：按钮名称、ref、label 不是动作安全属性。一个无语义名称的 textbox 可能是 password/email/credential 字段；一个“Continue”或“Next”按钮可能提交表单、购买或触发外部副作用；Enter 也可能 submit。当前代码对这类目标缺少 input type、form action/method、control role/state 和 navigation effect 的结构化判定，因而可能既不请求 fresh confirmation，也不触发永久禁止。

证据：

- browser_runtime.py:1565-1588 的 batch_requires_confirmation() 只组合 role/name/parent/state token 和少量 submit/doubleClick 参数，再匹配固定 marker。
- browser_runtime.py:1946-1975 的 _candidate_risk_text()、_is_forbidden_high_risk_action()、_is_high_risk_action() 仍主要依赖 candidate 文本；_is_high_risk_action() 的 marker 集合也不能从 generic textbox 或 generic Enter 推断真实表单副作用。
- browser_runtime.py:116-145 会向 backend 转发 button、doubleClick、modifiers、element、slowly、submit 等后端选项；这些选项不应由未严格约束的模型参数决定隐含动作语义。
- tests/complex_browser_acceptance.py:51-53 的 runner 禁止动作 marker 甚至未覆盖 login/password/username/email；230-233 只从 action/ref/element/label/value 文本计数，因此无法作为实际 action ledger 的安全证明。

最小修复建议：

1. 从浏览器 adapter 返回结构化 target metadata（role、input type、password/autocomplete、form action/method、submit semantics、download/upload、origin）；风险分类基于 metadata 和实际 effect，不基于显示文字猜测。
2. 对 fill_ref、password/email/credential-like input、press_key Enter、未知或多义的 form control 默认 fail closed；明确区分“需要确认”和“永久禁止”，并把 classification 绑定在 observation/target 上。
3. 对 runner 记录实际 runtime action decision（dispatched、blocked、confirmation id、forbidden reason），不要用字符串扫描替代。补测试覆盖 generic textbox、Continue/Next、Enter、unknown backend knobs 和无 label 控件。

### F-05 — High：origin/final-tab 验证使用 substring，可能把 attacker domain 当成真实站点

风险：github.com.evil.example、evilgithub.com 或带相似文本的 URL 可能满足“包含 github.com”的条件。该问题可将 phishing/错误站点计为 real site 或 final state，并影响 acceptance green result。

证据：

- browser_runtime.py:1376-1383 对 required_origins 使用 required_origin in observed_origin。
- browser_runtime.py:1397-1406 对 final_tabs 使用 expected string 是否为 tab URL 的 substring。
- tests/complex_browser_acceptance.py:276-280 的 github_repositories gate 使用 all("github.com" in item for item in hosts)。
- browser_task_spec.py 和导航执行路径已经使用 origin 概念，但 verify/report gate 没有复用 exact (scheme, hostname, effective port) 比较。

最小修复建议：

1. 统一使用解析后的 scheme、canonical hostname、effective port 做 exact origin comparison；若政策允许子域名，只允许 host == expected 或 host.endswith("." + expected)，禁止 substring。
2. 对 final tab URL 先执行 userinfo/query/fragment scrub 和 public-host check，再比较；不要把 raw hostname substring 当作站点身份。
3. 添加 github.com.evil、evilgithub.com、不同 port、userinfo 和 redirect 的验证测试。

### F-06 — High：case-16 confirm/reject 分支没有证明实际动作和最终页面，可能以弱状态事件作验收证据

风险：验收可以在未证明 login click 已经在确认之后发生，或未证明 reject 回到了指定 home page 的情况下给出 completed_verified。这不是把模型自然语言直接当事实，而是把过弱的 runtime event、status 和布尔 metrics 当成了事实；结果同样会隐藏安全回归。

证据：

- tests/complex_browser_acceptance.py:417-437 会做 postflight tab listing，但 reject 分支在 510 调用 postflight_tab_check() 后没有使用其返回值来断言目标页面。
- tests/complex_browser_acceptance.py:496-503 只用 browser_confirmation_rejected event 的 ok 与 state_changed 设置 return_after_rejection；这不能证明是 home page，也不能证明 login page 从未到达。
- tests/complex_browser_acceptance.py:521-524 的 reject pass 条件仅为 return_after_rejection and not login_before_confirmation。
- tests/complex_browser_acceptance.py:605-608 的 confirm pass 只按 status == completed_verified 计数，没有检查确认后的 login click_ref、目标绑定 confirmation id、实际 dispatch 和 fresh postcondition。
- tests/complex_browser_acceptance.py:289-307 还会把 status/metrics 组合为 verified，而不是要求每个 branch 都有独立、来源可追溯的最终状态证明。

最小修复建议：

1. 让 BrowserRuntime 产生不可由模型填写的 action ledger：每个 high-risk action 记录 target digest、observation id、confirmation id、dispatch result 和 postcondition observation。
2. confirm branch 必须证明对应 login target 在 approval 之后 dispatch，并由新 observation 证明预期结果；reject branch 必须证明 exact home origin/page marker、tab 数量和无 login dispatch。postflight 返回值必须参与 gate。
3. 移除“model reported completed”“state_changed”“status 字符串”作为独立证据；只接受 runtime-owned verifier 的结构化结果。

### F-07 — Medium：动作 schema 对嵌套参数不严格，违反 schema-first/narrow action contract

风险：顶层扁平参数有 allowlist，但只要使用 arguments 对象，未知键就会直接进入 runtime；模型 schema 也将多种 action 的 nested arguments 设为 additionalProperties: True，整个 browser tool strict: False。这给 backend-specific knobs、未来字段和风险校验留下了隐式扩展面，并使错误结果和重试行为不够确定。

证据：

- browser_actions.py:68-88 的 _flatten_action_arguments() 对嵌套 dict 直接 return dict(arguments)，没有 unknown-key rejection。
- agent_runtime.py:1867-1886、1909、1966-2004 的 fill/click/select/wait/extract/verify 参数 schema 多处 additionalProperties: True；agent_runtime.py:2013-2015 的 browser tool strict 为 False。
- browser_actions.py:196-338 只验证已知字段的部分类型，未知 nested field 仍能保留；内存探针用一个 unknown backend key 的 click action 得到了成功的 BrowserAction。

最小修复建议：

1. 对每个 action 建立单一、版本化的 allowlist schema，nested 和 flattened 两种输入都拒绝未知键；模型 schema strict: true，runtime 仍必须独立校验。
2. 仅允许明确声明且有类型/范围的 backend options；不要把 submit、doubleClick 等隐含副作用选项作为任意模型字段转发。
3. 每种 invalid schema 都返回稳定的 status/failure_kind/next_actions/requires_reobservation，并增加 unknown nested argument 的测试。

### F-08 — Medium：普通 backend failure 不一定使 observation 失效，存在 side effect unknown 后重放风险

风险：timeout/connection failure 已有重新观察控制，但普通 browser_backend_failure 不会自动设置 requires_reobservation。如果 backend 在实际产生副作用后才返回非结构化错误，模型可能在同一 observation 上再次发出相同 semantic action。高风险确认的一次性消费能降低一部分风险，但对被 F-04 启发式漏判的动作、以及普通 state action 仍不够。

证据：

- browser_runtime.py:188-191 的 _REOBSERVATION_FAILURES 只覆盖有限的 timeout/connection 类别。
- browser_runtime.py:1865-1873 的 _backend_failure() 只有当 failure kind 位于该集合时才设置 _reobservation_required=True 和 requires_reobservation=True；普通 backend failure 原样保留且可能没有 retry/stop contract。
- browser_runtime.py:880-895、994-1012 的重复 action/signature 防护依赖 action 已被记录或 observation 已失效；generic error 后的未知副作用没有统一状态。

最小修复建议：

1. 任意 state-changing call 在 dispatch 后得到 ok:false、invalid result 或不确定 transport 状态时，统一标记 side_effect_unknown、立即 invalidate observation、禁止相同 semantic action，直到 fresh snapshot。
2. 结果 schema 必须显式给出 retry_safe、requires_reobservation、next_actions 和 safe stop；只有明确的“未 dispatch”错误允许原 observation 上的安全重试。
3. 用 fake backend 分别模拟“error before dispatch”和“error after dispatch”，验证只有前者可安全重试。

### F-09 — Medium：evidence 字段按 ledger 全局聚合，缺少同一记录/同一来源绑定；generic verified 缺少 provenance

风险：只要不同页面的 records 合起来包含所需字段，验收就可能通过；一个 record 的 title 与另一个 record 的 URL/来源没有被绑定为同一对象。通用 workflow helper 还会把结果中的 verified=True 直接当作证据，缺少 verifier kind、source、observation 和 runtime provenance。

证据：

- browser_runtime.py:1355-1363 遍历所有 ledger records，并把有值字段的字段名全局合并为一个 evidence_fields 集合，只验证字段存在，不验证所需字段是否来自同一 record、同一 observation 或同一 source URL。
- browser_runtime.py:1367-1383 的 source priority 仍使用 source 字符串 substring；required origin 也不是 exact origin。
- tests/complex_browser_acceptance.py:411-415 全局 union record fields，tests/complex_browser_acceptance.py:263-271 再用 union 去满足 scenario required fields。
- workflow_runtime.py:379-390 的 _has_verifiable_evidence() 对任意 result 的 verified truthy 值直接返回 true；这适合 runtime-owned adapter，但没有在边界上证明该字段不是模型/外部 adapter 自报。

最小修复建议：

1. evidence 验证按 item/record 进行：每个结果必须有稳定 identity、title/primary key、source origin/path、observation id 和字段集合；required fields 必须在同一 record 内满足。
2. verified 不应作为模型可写的自由布尔值；只接受 allowlisted verifier 产生的 kind、版本、source/observation binding，必要时使用进程内不可伪造的 evidence handle。
3. acceptance report 只输出上述绑定后的摘要；对 alternate verifier 或未知 provenance 默认 fail closed。

## 风险排序与最小落地顺序

1. 先修 F-01/F-02：统一建立 report/provider/UI 三个出口的敏感数据 sanitizer 和 allowlisted output schema。这是 URL query、页面内容、cookie/凭据泄漏的共同根因。
2. 再修 F-03/F-04/F-05：收紧 observed-link navigation、结构化高风险分类、精确 origin comparison。测试必须覆盖 private/attacker domain、userinfo、generic form control 和 Enter。
3. 再修 F-06/F-09：把验收改为 runtime-owned action/evidence ledger，按同一 record、同一 source、同一 observation 验证；禁止 status、state_changed、model answer 单独构成证据。
4. 最后修 F-07/F-08：严格 schema 和统一 uncertain-side-effect recovery，保证工具错误结果包含稳定的 safe retry/stop contract。

## 未发现或已具备的控制

- 高风险确认使用 observation、origin、target role/name hash、match count 和 action hash 绑定；一次性授权在 MCP 调用前消费。
- permanently forbidden action 在确认后仍会被再次拒绝；确认不能授权 submit/purchase/upload/deletion/credential input。
- timeout/connection close、stale observation、no-progress handoff、缓存 workflow revalidation 和部分 desktop replay guard 已有测试覆盖。
- 本次没有执行任何真实外部副作用动作，因此没有把“测试通过”解释为真实站点安全验收通过。
