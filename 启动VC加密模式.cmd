@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0main.py" if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0main.py" --configure-veracrypt
  goto :done
)
if exist "%~dp0AssistantTool.exe" "%~dp0AssistantTool.exe" --configure-veracrypt
:done
endlocal
