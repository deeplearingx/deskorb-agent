@echo off
setlocal
cd /d "%~dp0"

python "tools\venv_bootstrap.py" check --project-root "%CD%" >nul 2>nul
if errorlevel 1 (
  echo DeskOrb Agent virtual environment is missing or unhealthy. Running setup...
  call setup.cmd
  if errorlevel 1 exit /b 1
) else if not exist ".playwright-mcp\cli.js" (
  echo Local Playwright MCP is not installed. Running setup...
  call setup-playwright-mcp.cmd
  if errorlevel 1 exit /b 1
) else if not exist ".playwright-mcp\node_modules\playwright-core\package.json" (
  echo Local Playwright MCP dependencies are missing. Running setup...
  call setup-playwright-mcp.cmd
  if errorlevel 1 exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "deskorb_agent.py"
endlocal
