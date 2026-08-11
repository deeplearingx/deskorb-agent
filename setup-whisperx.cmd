@echo off
setlocal
cd /d "%~dp0"

if not exist "whisperX-main\pyproject.toml" (
  echo Bundled whisperX-main project was not found.
  exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) and sys.version_info < (3, 14) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.10, 3.11, 3.12, or 3.13 is required for WhisperX.
  exit /b 1
)

if not exist "whisperX-main\.venv\Scripts\python.exe" (
  echo Creating whisperX-main\.venv ...
  python -m venv "whisperX-main\.venv"
  if errorlevel 1 exit /b 1
)

"whisperX-main\.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"whisperX-main\.venv\Scripts\python.exe" -m pip install -e "whisperX-main"
if errorlevel 1 exit /b 1
rem Pin a known-good Windows CTranslate2 runtime for this bundled checkout; imageio-ffmpeg supplies the ffmpeg CLI.
"whisperX-main\.venv\Scripts\python.exe" -m pip install --upgrade "ctranslate2==4.5.0" "imageio-ffmpeg>=0.6.0"
if errorlevel 1 exit /b 1

echo.
echo Bundled WhisperX is ready.
echo Python: whisperX-main\.venv\Scripts\python.exe
endlocal
