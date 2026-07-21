@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Codex Overlay is not set up yet. Running setup...
  call setup.cmd
  if errorlevel 1 exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "claude_overlay.py"
endlocal

