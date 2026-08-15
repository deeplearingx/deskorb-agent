@echo off
setlocal
cd /d "%~dp0"

set "PS_EXE="
if exist "%~dp0runtime\pwsh\pwsh.exe" set "PS_EXE=%~dp0runtime\pwsh\pwsh.exe"
if not defined PS_EXE if exist "%LOCALAPPDATA%\PowerShell7\pwsh.exe" set "PS_EXE=%LOCALAPPDATA%\PowerShell7\pwsh.exe"
if not defined PS_EXE if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PS_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PS_EXE (
  where pwsh >nul 2>nul
  if not errorlevel 1 set "PS_EXE=pwsh"
)
if not defined PS_EXE if exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not defined PS_EXE (
  echo PowerShell is required to check the environment.
  exit /b 1
)

"%PS_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\check-environment.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
