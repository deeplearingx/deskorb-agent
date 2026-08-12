# 复杂场景 E2E 测试清单

- [x] 环境与依赖盘点
- [x] 单元测试收集
- [x] 运行时 E2E
- [x] 60 条矩阵 1 次运行
- [x] 质量门禁与隐私报告校验
- [x] 失败场景复现/修复（临时工作目录、最终 verify 聚合、连接关闭噪声）
- [x] repeatable 矩阵 3 次稳定性验证
- [x] 真实公网只读验收（淘宝/必应）
- [x] 当前桌面显式授权诊断
- [x] live 报告隐私/格式校验
- [x] 最终结果与阻塞项汇总

## 历史真实环境结论（前期快照）

- [x] 已执行公网点击/标签切换链路并验证安全策略未误阻断
- [x] 已执行当前桌面只读诊断并修复 SQLite 写入问题
- [ ] 公网 Bing 业务终态完整通过（当前阻塞：模型首回合超时；此前一次为未验证终态）
- [ ] 当前桌面环境可读且有可捕获前台窗口（前期快照中的阻塞；以最新真实会话预检报告为准）

## 当前权威执行清单（2026-08-11）

- [x] Slice 1：接入生产运行时任务协议与结构化终态。
- [x] Slice 2：移除本机真实 runner 对旧私有 API 的依赖；统一读取 `tool_result` 脱敏事件，并校验桌面目标窗口绑定。
- [ ] Slice 3：统一控制组指标、失败分类和质量门禁。
- [ ] Slice 4：完成预检后执行真实桌面、公网只读和 60 × 3 验收。

## 最新回归（2026-08-11）

- `394 passed, 50 subtests passed`。
- 确定性矩阵 `57 passed, 3 skipped, 0 failed`；live/consent 场景未计入成功。
- 空输入、Office 空上下文、malformed function arguments、危险动作阻断/执行判定均有回归覆盖。
- 真实 `diagnose-001` smoke 通过：4 次读取、1 次最小写入、1 次非破坏性 shell 验证；运行时收尾超时不再覆盖已发布的完成终态。

## 当前代码整理状态（2026-08-11）

- [x] 60 条任务集、扩展集和步骤基线由 `tests/e2e_support/datasets.py` 统一解析。
- [x] `matrix_e2e_runner.py` 与 `local_real_e2e_runner.py` 保留兼容入口，并共享唯一矩阵加载器。
- [x] 测试目录职责和真实 runner 的 fail-closed 边界已写入 `tests/README.md`。
- [x] 结构回归覆盖共享路径、60 条唯一性和两个 runner 的一致性。
- [ ] 后续正式授权矩阵：代码整理完成后再单独执行，不与结构重构混在同一提交中。

## 桌面操作提示层（2026-08-12）

- [ ] Task 1：实现 `DesktopActivityLifecycle`、事件结构和纯单元测试。
- [ ] Task 2：为 AgentRuntime 实际桌面动作接入 `desktop_activity begin/end`。
- [ ] Task 3：实现 Win32 click-through/no-activate/capture-excluded 适配和多显示器几何。
- [ ] Task 4：实现四边羽化、顶部提示胶囊和自然动画。
- [ ] 检查点：运行时契约与渲染器 focused pytest 全部通过。
- [ ] Task 5：接入 Tk Overlay 事件泵、停止/错误/退出强制清理。
- [ ] Task 6：完成真实临时 Notepad、截图排除和异常收尾验收。
- [ ] 检查点：完整 pytest、代码审查和实现提交。
