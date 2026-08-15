# Playwright MCP 启动 EPERM 根因诊断

诊断日期：2026-08-15（Asia/Shanghai）  
范围：只读检查 DeskOrb 的 Playwright MCP 配置、子进程环境、仓库内浏览器缓存和一次受控的无导航 MCP 协议诊断。没有安装依赖、没有运行 `tests/e2e_public_browser_action_probe.py`、没有导航/点击真实页面，也没有终止已有进程。

## 结论

最可能的根因不是仓库内 Chromium 可执行文件或 `PLAYWRIGHT_BROWSERS_PATH` 指向错误，而是 Playwright MCP 0.0.79 的另一套全局 server-registry：

```text
C:\Users\16354\AppData\Local\ms-playwright\b\browser@<guid>
```

`PLAYWRIGHT_BROWSERS_PATH` 只影响 Playwright 浏览器 binary registry；Playwright MCP 的 `serverRegistry` 另行使用 `%LOCALAPPDATA%\ms-playwright\b` 保存浏览器 server descriptor。一次受控诊断在首次 `browser_snapshot` 时复现了：

```text
EPERM: operation not permitted, open 'C:\Users\16354\AppData\Local\ms-playwright\b\browser@<guid>'
```

因此之前被报告为“Playwright MCP 启动 EPERM”的现象更准确地说是：MCP Node 进程已经启动并完成协议初始化，首次只读快照触发浏览器后端初始化时，写全局 descriptor 失败。`tests/e2e_public_browser_action_probe.py` 最终收到的是 `browser_mcp_start_failed`，这是当前错误传播/分类造成的笼统类别，不是 `subprocess.Popen` 本身失败。

NTFS ACL 检查没有显示普通用户拒绝：仓库内路径和 `%LOCALAPPDATA%\ms-playwright\b` 都显示当前用户 FullControl。因此剩余的环境解释是受管沙箱/安全软件对该用户级全局目录的写入限制，或多个 Playwright 版本/进程共享该目录造成的 Windows `open` 竞争。现有证据足以把故障定位到全局 registry 写入，但不能仅凭 ACL 在“沙箱策略”和“外部锁/安全软件”之间作绝对区分。

## 证据

### 1. 仓库内 CLI、依赖和浏览器缓存是完整且匹配的

静态检查结果：

- `mcp_client.py:33-64` 的本地根目录是 `D:\workspace\deskorb\.playwright-mcp`；CLI、`node_modules\playwright-core\package.json` 和 `ms-playwright` 都存在。
- `local_playwright_diagnostics()` 返回空列表，没有报告 `node_not_found`、CLI 缺失、依赖缺失或 Chromium 缺失。
- `.playwright-mcp\package.json` 为 `@playwright/mcp` `0.0.79`；`playwright`/`playwright-core` 为 `1.63.0-alpha-2026-08-05`。
- `playwright-core\browsers.json` 需要 Chromium revision `1237`，browser version `152.0.7977.8`；本地存在 `ms-playwright\chromium-1237\chrome-win64\chrome.exe`。
- `cli.js --version` 返回 `Version 0.0.79`，退出码 0。
- 直接执行本地 `chrome.exe --version` 退出码 0；没有出现执行文件级别的 `EPERM`。
- `chrome.exe` 大小为 4,399,616 bytes，属性为普通 Archive；仓库内 CLI、浏览器目录和 `chrome.exe` 的 ACL 均包含当前用户 FullControl。

这组证据不支持“Chromium 文件不存在、版本 revision 不匹配、`chrome.exe` 没有执行权限”作为首要根因。

### 2. `PLAYWRIGHT_BROWSERS_PATH` 的实际继承链

外层进程中 `PLAYWRIGHT_BROWSERS_PATH` 未设置；`TEMP`/`TMP` 为 `D:\ai_cache\temp`，`LOCALAPPDATA` 为 `C:\Users\16354\AppData\Local`。

`mcp_client.py:475-495` 的 `StdioMCPClient.start()` 先做 `env = os.environ.copy()`，再 `env.update(self.spec.env)`，并用无 shell 的 `subprocess.Popen` 启动 Node。当前本地 spec 的关键值是：

```text
command: node
args: D:\workspace\deskorb\.playwright-mcp\cli.js --browser chromium --isolated
cwd: D:\workspace\deskorb
PLAYWRIGHT_BROWSERS_PATH: D:\workspace\deskorb\.playwright-mcp\ms-playwright
resolved node: C:\nvm4w\nodejs\node.exe
```

一次受控诊断打印出的子进程环境也确认了：

```text
PLAYWRIGHT_BROWSERS_PATH = D:\workspace\deskorb\.playwright-mcp\ms-playwright
TEMP/TMP = D:\ai_cache\temp
cwd = D:\workspace\deskorb
```

当前环境没有额外的 `PLAYWRIGHT_MCP_*` 或 `PWTEST_*` 变量污染该诊断。换言之，浏览器 binary 的环境变量确实传到了子进程；但它不会自动重定向 `%LOCALAPPDATA%\ms-playwright\b`。

### 3. 一次受控 MCP 原始协议诊断

诊断使用本地 `node .playwright-mcp\cli.js --browser chromium --isolated`，只发送了：

1. `initialize`；
2. `notifications/initialized`；
3. `tools/list`；
4. 一次 `browser_snapshot`，没有 URL、页面导航或状态动作；
5. 关闭 stdin，让 MCP/浏览器自然退出。

结果：

| 阶段 | 结果 |
|---|---|
| `initialize` | 成功，返回 protocol/server info |
| `tools/list` | 成功，返回 24 个工具 |
| 首次 `browser_snapshot` | 返回 `isError=true` |
| MCP 子进程退出 | 自然退出，code 0 |
| 原始 process stderr | 空字符串 |

`browser_snapshot` 的 JSON-RPC stdout 工具结果包含：

```text
### Error
Error: async initializeServer: EPERM: operation not permitted, open 'C:\Users\16354\AppData\Local\ms-playwright\b\browser@<guid>'
```

这解释了为什么直接看 `mcp_client.py` 的 stderr 诊断可能看不到 EPERM：`StdioMCPClient._read_stderr()`（约 `mcp_client.py:617-623`）只收集 stderr，而本次错误在 MCP 的 stdout JSON 工具结果中。受控子进程结束后未发现残留 Node/Chrome 进程。

### 4. 全局 registry 的源码和历史残留

本地 `playwright-core` bundle 的 server-registry 实现（约 `lib\coreBundle.js:52050-52185`）明确显示：

- Windows 默认目录由 `LOCALAPPDATA` 计算；
- 最终目录是 `LOCALAPPDATA\ms-playwright\b`；
- `create()` 用 `fs.promises.writeFile(file, ...)` 写入 descriptor；
- `file` 名称是浏览器的 `browser@<guid>`。

该路径与 `PLAYWRIGHT_BROWSERS_PATH` 的 binary registry 代码路径分离。当前 `C:\Users\16354\AppData\Local\ms-playwright\b` 中还能读到旧 descriptor，内容显示 Playwright `1.62.0-alpha...`、用户级 npx cache 和另一个工作区 `D:\workspace\autogen\deskorb-agent`。这证明它是多个版本/工作区共享的全局状态目录，而不是本次仓库的私有浏览器缓存。

仓库历史说明 `tasks/plan.md:516` 曾把故障写成“Playwright 浏览器目录无权限”。这与当前原始错误目标不符；该描述应视为未经分层验证的历史判断，不能作为根因证据。

## 为什么 E2E 只显示 `browser_mcp_start_failed`

调用链如下：

```text
tests/e2e_public_browser_action_probe.py:48
  -> AgentRuntime.prepare_visible_browser()
     -> mcp.schemas(("playwright",))
        -> initialize + tools/list                 成功
     -> mcp.call(browser_snapshot, {})
        -> MCPToolBridge.call() 收到 result.isError
        -> 对 Playwright 返回 ok=False + content，未填 error
     -> prepare_visible_browser() 发现 snapshot.ok=False
        -> 没有 snapshot.error，回退 browser_mcp_start_failed
  -> probe 输出 startup_visible=false，steps=[]
```

相关代码位置：`mcp_client.py:893-895`、`agent_runtime.py:917-1000`、`tests/e2e_public_browser_action_probe.py:48-65`。因此当前 probe 的 `steps=[]` 也说明没有执行 `navigate`、`snapshot` 后续动作；这次故障不是页面状态动作失败。

## 最小安全修复建议

1. 对默认本地 Playwright MCP 同时设置两类路径：保留
   `PLAYWRIGHT_BROWSERS_PATH=<仓库>\.playwright-mcp\ms-playwright`，并新增
   `PWTEST_SERVER_REGISTRY=<当前启动用户确定可写的、每个 bridge 唯一的私有目录>`。
   这个变量需要同时传给本地 `node` 路径和固定版本的 `npx` fallback；不要把全局
   `%LOCALAPPDATA%\ms-playwright\b` 当作任务间共享写目录。
2. 私有 registry 的父目录必须以实际 DeskOrb launcher 的权限验证；本机 `TEMP/TMP` 是
   `D:\ai_cache\temp`，而受管执行环境的可写根目录可能比 NTFS ACL 更窄。若该 temp 根在
   实际部署中不可写，应选择该 launcher 明确允许的 per-run 目录。只清理本 bridge 自己创建
   的 registry，不删除全局旧目录、不碰其他进程的 descriptor。
3. 当前未提交的 `mcp_client.py` 已包含一个方向正确的候选实现：约 `254-267` 把
   `PWTEST_SERVER_REGISTRY` 放入 Playwright spec，约 `694-724` 为默认 bridge 预留唯一
   registry，约 `953-960` 只清理自有目录；对应测试也在
   `tests/test_mcp_client.py:133-152`。这部分是既有工作树改动，本诊断没有编辑或验证它，
   不能在本报告中宣称已修复。
4. 另做一个低风险的可观测性改进：当 Playwright 工具返回 `isError=true` 时，把错误正文
   经过现有 bounded/redaction 规则带入启动诊断，而不是只看 stderr。否则类似本次“stderr
   为空、真正错误在 stdout JSON”的问题仍会被错误地称为 generic start failure。

## 建议回归命令

以下命令只作为修复后的回归顺序，本次没有执行会启动 MCP 的回归命令：

```powershell
# 1) 不启动 server，确认运行时生成的 spec 同时带两类路径
rtk pwsh -NoLogo -NoProfile -Command "D:\conda_envs\deskorb-agent\python.exe -c 'import mcp_client; b=mcp_client.MCPToolBridge(None, enable_officecli=False); print(b.specs); b.close()'"

# 2) 先跑 focused 单元回归
rtk pwsh -NoLogo -NoProfile -Command "D:\conda_envs\deskorb-agent\python.exe -s -m pytest -q tests\test_mcp_client.py tests\test_browser_stabilization.py"

# 3) 再做只读 MCP readiness（无真实站点导航）
rtk pwsh -NoLogo -NoProfile -Command "D:\conda_envs\deskorb-agent\python.exe tests\mcp_readiness_probe.py"
```

通过标准：Playwright spec 的 env 同时出现私有 `PWTEST_SERVER_REGISTRY` 和仓库内
`PLAYWRIGHT_BROWSERS_PATH`；`mcp_readiness_probe.py` 能发现工具且无 diagnostics；首次
`browser_snapshot` 的工具结果为 `ok=true`，并且私有 registry 在自然关闭后没有残留自有文件。

只有 readiness 通过且得到明确授权后，才建议重跑真实公开站点 probe；本次不重放该动作：

```powershell
rtk pwsh -NoLogo -NoProfile -Command "D:\conda_envs\deskorb-agent\python.exe tests\e2e_public_browser_action_probe.py --url https://books.toscrape.com/ --output artifacts\public-books-browser-action-regression.json"
```

## 诊断限制和工作树说明

- `conda run -n deskorb-agent ...` 在本受管环境返回 `NoWritableEnvsDirError`（配置的 envs
  目录没有可写目录）；没有因此安装或修改任何依赖。只读 Python 检查改用同一专用环境的
  `D:\conda_envs\deskorb-agent\python.exe`。
- 开始诊断时工作树已经有多处用户未提交改动；`mcp_client.py` 和
  `tests/test_mcp_client.py` 在诊断期间也处于外部修改状态。本报告只新增本文件，未回滚、
  覆盖或修改其他文件。
- 本报告把原始错误定位到全局 registry 写入；如果将 registry 重定向到实际可写目录后仍
  复现同一 `open` EPERM，下一步才应在不改代码的前提下检查 Windows Defender/AppLocker
  或具体锁持有者，而不是继续重装 Chromium。
