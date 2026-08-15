@echo off
setlocal

cd /d "D:\workspace\deskorb"
set "DESKORB_AGENT_API_IMAGE_INPUT=1"
set "DESKORB_AGENT_API_PROXY="
set "ALL_PROXY="
set "all_proxy="

if not exist "D:\conda_envs\deskorb-agent\pythonw.exe" (
    echo DeskOrb Agent environment was not found:
    echo D:\conda_envs\deskorb-agent\pythonw.exe
    pause
    exit /b 1
)

if not exist "D:\workspace\deskorb\deskorb_agent.py" (
    echo DeskOrb Agent entry point was not found:
    echo D:\workspace\deskorb\deskorb_agent.py
    pause
    exit /b 1
)

start "DeskOrb Agent" /d "D:\workspace\deskorb" "D:\conda_envs\deskorb-agent\pythonw.exe" "D:\workspace\deskorb\deskorb_agent.py"
endlocal
