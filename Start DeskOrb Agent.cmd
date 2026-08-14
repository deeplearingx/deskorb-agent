@echo off
setlocal
call "%~dp0Start DeskOrb Agent Conda.cmd" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal
exit /b %EXIT_CODE%
