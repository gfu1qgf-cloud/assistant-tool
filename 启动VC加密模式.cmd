@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0main.py" (
  call "%~dp0启动程序.cmd" --configure-veracrypt %*
  goto :done
)
if exist "%~dp0AssistantTool.exe" "%~dp0AssistantTool.exe" --configure-veracrypt
:done
endlocal
