@echo off
setlocal
cd /d "%~dp0"
set "CONDA_NO_PLUGINS=true"
set "PYTHONNOUSERSITE=1"

where conda >nul 2>nul
if errorlevel 1 (
  echo Conda is required. Install Miniconda or Anaconda, then rerun this script.
  exit /b 1
)

conda env list | findstr /r /c:"^[ ]*deskorb-agent[ ]" >nul
if errorlevel 1 (
  echo Conda environment deskorb-agent is missing. Running setup...
  call setup-conda.cmd
  if errorlevel 1 exit /b 1
)

if not exist ".playwright-mcp\cli.js" (
  echo Local Playwright MCP is missing. Running setup...
  call setup-playwright-mcp.cmd
  if errorlevel 1 exit /b 1
)

conda run --no-capture-output -n deskorb-agent pythonw.exe deskorb_agent.py
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
