# DeskOrb Agent 环境安装脚本

这一组脚本用于准备 DeskOrb Agent 的开发、测试和发布构建环境。它们不写入 API Key，也不会替用户安装 Microsoft Office 或 PowerToys 这类外部应用。

## 构建 Windows 安装包

主安装包会携带独立的 `deskorb-agent` Python 运行时、Playwright MCP 和 Chromium、便携版 Node.js、PowerShell 7、MinGit，以及 OfficeCLI 二进制文件。WhisperX 的虚拟环境约占 2.2 GB，因此不放入主安装包；安装后需要时再运行 `setup-whisperx.cmd`。

先准备构建依赖：

```powershell
.\setup-all.cmd -InstallPortableTools -InstallDotNet -BuildOfficeCli
```

构建安装包：

```powershell
.\packaging\build-installer.ps1
```

输出文件：

```text
artifacts\installer\DeskOrb-Agent-0.2.0-Setup-x64.exe
```

构建机需要预先安装 Inno Setup 6 和 `conda-pack`。安装器使用当前用户目录下的
`%LOCALAPPDATA%\Programs\DeskOrb Agent`，不需要管理员权限；安装完成时会自动执行
`conda-unpack` 修正 Python 环境路径，并创建开始菜单和桌面快捷方式。

## 一键准备开发环境

在仓库根目录打开终端：

```powershell
.\setup-all.cmd -InstallPortableTools -InstallDotNet -BuildOfficeCli
```

这条命令会：

1. 准备 Conda 环境 `deskorb-agent`；
2. 安装固定版本的 Playwright MCP 和 Chromium；
3. 在 `runtime\` 下准备便携版 Node.js、PowerShell 7、MinGit 和 .NET 10 SDK；
4. 构建自包含的 `tools\officecli\officecli.exe`；
5. 运行环境检查并打印报告。

如果还需要会议录音和 WhisperX：

```powershell
.\setup-all.cmd -InstallPortableTools -InstallDotNet -BuildOfficeCli -InstallWhisperX
```

WhisperX 会显著增加磁盘占用，并可能在首次使用时下载语音模型。建议先使用核心环境完成桌面、浏览器和 OfficeCLI 验收，再单独启用 WhisperX。

## 只检查环境

```powershell
.\check-environment.cmd
```

将可选组件也作为必需项检查：

```powershell
.\check-environment.cmd -RequireWhisperX -RequireOfficeCli -Strict
```

输出 JSON 报告：

```powershell
.\check-environment.cmd -JsonPath artifacts\environment-report.json
```

## 便携工具下载

`packaging\install-portable-tools.ps1` 默认从官方发布源查询当前版本：

- Node.js LTS：nodejs.org；
- PowerShell 7：PowerShell GitHub Releases；
- MinGit：Git for Windows GitHub Releases。

如果构建环境不能访问网络，可以手动准备 ZIP，再通过代理或预置缓存完成安装；不要把 API Key 放进脚本参数或 ZIP 包。

可以固定版本以便重复构建：

```powershell
.\setup-all.cmd `
  -InstallPortableTools `
  -InstallDotNet `
  -NodeVersion 22.22.1 `
  -PowerShellVersion 7.5.3 `
  -GitVersion 2.53.0.windows.1
```

若需要更新已有便携工具：

```powershell
.\setup-all.cmd -InstallPortableTools -ForcePortableTools
```

只准备项目内的 .NET 10 SDK：

```powershell
.\packaging\install-dotnet-sdk.ps1
```

该脚本使用 Microsoft 官方 `dotnet-install.ps1`，默认把 SDK 放到
`runtime\dotnet`，不会修改系统级 .NET 安装。

## 外部可选依赖

### PowerToys

PowerToys 不是 DeskOrb 的内置运行时。程序会从以下位置检测 `PowerToys.DSC.exe`：

```text
%LOCALAPPDATA%\PowerToys\PowerToys.DSC.exe
%ProgramFiles%\PowerToys\PowerToys.DSC.exe
```

未安装 PowerToys 时，DeskOrb 主程序仍然可以运行，但 PowerToys MCP 功能不可用。

### Microsoft Office

OfficeCLI 的磁盘文件操作不要求安装 Office；但对当前打开、包含未保存修改的 Word/Excel 文档进行 COM 读写时，需要本机安装 Microsoft Office。

### API 配置

API Key 应在 DeskOrb 的 Connection settings 中配置，或使用 `.env.example` 生成本地配置文件。真实 `.env` 文件不要提交到 Git，也不要放入发布包。

## 发布构建前置条件

正式安装包构建机还需要：

- Windows x64；
- Conda；
- .NET 10 SDK；
- Inno Setup 6；
- 可访问 Node.js、GitHub Releases 和 Playwright MCP 仓库的网络；
- 至少 5 GB 可用空间；如果启用 WhisperX，建议预留 8 GB 以上。

环境准备完成后，再执行安装器构建脚本。当前脚本只负责准备运行依赖和构建产物，API Key、用户数据和会议录音不会被打进安装包。
