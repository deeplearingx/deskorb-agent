# DeskOrb 测试目录

测试目录按运行边界分为四类。文件暂时保留原路径，避免破坏已有命令和人工探针入口；新的共享数据加载逻辑位于 `tests/e2e_support/`。

## 入口与职责

| 入口 | 类型 | 资源边界 | 适用场景 |
| --- | --- | --- | --- |
| `test_*.py` | 单元/集成回归 | 默认无公网、无真实桌面 | 每次代码修改后的快速验证 |
| `matrix_e2e_runner.py` | 确定性控制组 | 本地夹具、脚本化模型、桌面 double | 稳定回归和质量门禁前置检查 |
| `local_real_e2e_runner.py` | 本机真实 E2E | 真实模型；公网和当前桌面必须显式授权并预检通过 | 本机 smoke、真实桌面和只读公网验收 |
| `*_probe.py` | 环境/人工探针 | 可能启动浏览器、Office 或临时应用 | 针对性诊断，不属于默认单测 |
| `run_quality_gate.py` | 报告门禁 | 只读脱敏 JSON | 覆盖率、完成率、安全和证据门禁 |

任务数据的唯一加载边界是 `e2e_support.datasets.load_e2e_matrix_cases()`，由基础数据集、扩展数据集组成 60 条矩阵。步骤下界位于 `e2e_step_baselines.json`，指标汇总和隐私校验位于根目录 `e2e_metrics.py`。

## 常用命令

在仓库根目录执行：

```powershell
python -m pytest -q
python tests\matrix_e2e_runner.py --repetitions 1 --output artifacts\matrix-e2e.json
python tests\run_quality_gate.py --report artifacts\matrix-e2e.json --require-full-matrix --repetitions 1
```

本机真实 runner 默认是 fail-closed。只有在当前机器已完成预检、并且用户明确授权时才加上公网或当前桌面开关：

```powershell
python tests\local_real_e2e_runner.py `
  --repetitions 1 `
  --allow-public-network `
  --allow-current-desktop `
  --output artifacts\local-real-e2e.json `
  --markdown-output artifacts\local-real-e2e.md
```

缺少模型、交互桌面、浏览器隔离环境或人工恢复时，报告必须记录 `blocked` 和具体 `failure_kind`，不能当作成功。真实 runner 的报告只保存脱敏结构化指标；提示、URL、页面正文、截图、凭据和工具参数不得写入报告。

## 产物与清理边界

`artifacts/`、`.deskorb-playwright-*`、`mcp-profile-*` 和 `.mcp-probe-*` 是本地运行产物，已被 `.gitignore` 忽略。它们可能包含调试所需的短期状态；不要在测试脚本中把这些目录当作源代码或固定输入，也不要在没有明确范围时递归删除。
