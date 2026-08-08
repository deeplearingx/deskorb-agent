@echo off
setlocal
cd /d "%~dp0"

python "tools\venv_bootstrap.py" check --project-root "%CD%" >nul 2>nul
if errorlevel 1 (
  echo DeskOrb Agent virtual environment is missing or unhealthy. Running setup...
  call setup.cmd
  if errorlevel 1 exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "deskorb_agent.py"
endlocal
