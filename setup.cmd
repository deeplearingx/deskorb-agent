@echo off
setlocal
cd /d "%~dp0"

where codex >nul 2>nul
if errorlevel 1 (
  echo Codex CLI was not found. Install it with:
  echo   npm install -g @openai/codex
  exit /b 1
)

python --version >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo.
echo Codex Overlay is ready.
echo Run: Start Codex Overlay.cmd
endlocal

