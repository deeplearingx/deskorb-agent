@echo off
setlocal
cd /d "%~dp0"
set "CONDA_NO_PLUGINS=true"
set "CONDA_SOLVER=classic"
set "PYTHONNOUSERSITE=1"

where conda >nul 2>nul
if errorlevel 1 (
  echo Conda is required. Install Miniconda or Anaconda, then rerun this script.
  exit /b 1
)

conda env update --name deskorb-agent --file environment.yml --prune
if errorlevel 1 exit /b 1

call setup-playwright-mcp.cmd
if errorlevel 1 exit /b 1

echo.
echo Conda environment deskorb-agent is ready.
echo Activate with: conda activate deskorb-agent
endlocal
