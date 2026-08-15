@echo off
setlocal
cd /d "%~dp0"

set "PYTHONNOUSERSITE=1"
set "CONDA_NO_PLUGINS=true"
set "PATH=%~dp0runtime\python;%~dp0runtime\python\Scripts;%~dp0runtime\node;%~dp0runtime\pwsh;%~dp0runtime\git\cmd;%PATH%"
set "DESKORB_AGENT_PWSH=%~dp0runtime\pwsh\pwsh.exe"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.playwright-mcp\ms-playwright"

if exist "%~dp0tools\officecli\officecli.exe" set "DESKORB_AGENT_OFFICECLI_BINARY=%~dp0tools\officecli\officecli.exe"
if not exist "%~dp0runtime\python\pythonw.exe" (
  echo DeskOrb Agent runtime is missing.
  echo Please rerun the installer or use setup-all.cmd.
  pause
  exit /b 1
)
if not exist "%~dp0deskorb_agent.py" (
  echo DeskOrb Agent entry point is missing.
  pause
  exit /b 1
)

start "" /b "%~dp0runtime\python\pythonw.exe" "%~dp0deskorb_agent.py"
endlocal
