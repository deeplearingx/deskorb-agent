@echo off
setlocal
cd /d "%~dp0"

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  exit /b 1
)

python "tools\venv_bootstrap.py" ensure --project-root "%CD%"
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -c "from PIL import Image; import keyboard; import win32com.client"
if errorlevel 1 (
  echo DeskOrb Agent dependencies could not be imported.
  exit /b 1
)

echo.
echo DeskOrb Agent is ready.
echo Run: Start DeskOrb Agent.cmd
endlocal
