@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "%~dp0.venv-py314\Scripts\python.exe" (
    echo 未找到程序独立环境 .venv-py314。请按 README 使用 Python 3.14 创建环境。
    pause
    exit /b 1
)
"%~dp0.venv-py314\Scripts\python.exe" -u "%~dp0main.py" %*
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" (
    echo 程序异常退出，错误码：%APP_EXIT_CODE%。请查看 logs 文件夹。
    pause
)
exit /b %APP_EXIT_CODE%
