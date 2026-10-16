@echo off
chcp 65001 >nul
if exist "%~dp0AssistantTool.exe" (
    start "" "%~dp0AssistantTool.exe" --offline %*
    exit /b
)
call "%~dp0启动程序.cmd" --offline %*
