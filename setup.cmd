@echo off
setlocal
cd /d "%~dp0"

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
echo DeskOrb Agent is ready.
echo Run: Start DeskOrb Agent.cmd
endlocal
