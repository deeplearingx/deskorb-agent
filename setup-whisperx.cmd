@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=python.exe"
if exist "runtime\python\python.exe" set "PYTHON_EXE=runtime\python\python.exe"

if not exist "whisperX-main\pyproject.toml" (
  echo Bundled whisperX-main project was not found.
  exit /b 1
)

"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) and sys.version_info < (3, 14) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Python 3.10, 3.11, 3.12, or 3.13 is required for WhisperX.
  exit /b 1
)

if not exist "whisperX-main\.venv\Scripts\python.exe" (
  echo Creating whisperX-main\.venv ...
  "%PYTHON_EXE%" -m venv "whisperX-main\.venv"
  if errorlevel 1 exit /b 1
)

"whisperX-main\.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"whisperX-main\.venv\Scripts\python.exe" -m pip install -e "whisperX-main"
if errorlevel 1 exit /b 1
rem Keep the known-good 4.5 runtime on Python 3.10-3.12.  CTranslate2 4.5.0
rem has no Windows cp313 wheel, so Python 3.13 needs the first compatible
rem release instead. imageio-ffmpeg supplies the ffmpeg CLI used by WhisperX.
"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>nul
if errorlevel 1 (
  "whisperX-main\.venv\Scripts\python.exe" -m pip install --upgrade "ctranslate2==4.5.0" "imageio-ffmpeg>=0.6.0"
) else (
  "whisperX-main\.venv\Scripts\python.exe" -m pip install --upgrade "ctranslate2>=4.6.0" "imageio-ffmpeg>=0.6.0"
)
if errorlevel 1 exit /b 1

echo.
echo Bundled WhisperX is ready.
echo Python: whisperX-main\.venv\Scripts\python.exe
endlocal
