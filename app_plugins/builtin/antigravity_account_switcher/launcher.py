"""Launcher and lifecycle helpers for Antigravity Desktop & IDE."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
import psutil
import time
from .clients import default_client, command_user_data, installed_client


class LaunchError(RuntimeError):
    pass


def default_antigravity_exe() -> Path:
    return default_client().executable


def is_antigravity_running(client=None) -> bool:
    if os.name != "nt":
        return False
    try:
        names = {"antigravity.exe", "antigravity ide.exe", "antigravity", "antigravity ide"}
        for process in psutil.process_iter(["name"]):
            if (client is not None and client.auth_kind == 'windows' and
                    (process.info.get('name') or '').casefold() == 'language_server.exe' and
                    Path(process.exe()).resolve() == client.executable.parent / 'resources/bin/language_server.exe'):
                return True  # The desktop reads its credential once at backend startup.
            if (process.info.get("name") or "").casefold() in names:
                if client is None or Path(process.exe()).resolve() == client.executable:
                    return True
        return False
    except (OSError, psutil.Error) as err:
        raise LaunchError("无法确认 Antigravity 是否已经退出，未修改登录数据。") from err


def close_antigravity(client=None) -> bool:
    """Request clean close of Antigravity desktop processes without forceful killing."""
    if os.name != "nt":
        return True
    if client is not None:
        try:
            main_pids = []
            installed = installed_client(client.executable)
            if installed is None:
                return False
            for process in psutil.process_iter(['name']):
                if (process.info.get('name') or '').casefold() not in ('antigravity.exe', 'antigravity ide.exe'):
                    continue
                if Path(process.exe()).resolve() != client.executable:
                    continue
                arguments = process.cmdline()
                if any(value.startswith('--type=') for value in arguments):
                    continue
                source = command_user_data(arguments) or installed.user_data
                if source != client.user_data:
                    return False  # Do not close another user-data environment.
                main_pids.append(int(process.pid))
            if main_pids:
                pid_list = ','.join(str(pid) for pid in main_pids)
                subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                    f'Get-Process -Id {pid_list} -ErrorAction SilentlyContinue | ForEach-Object {{ [void]$_.CloseMainWindow() }}'],
                    capture_output=True, timeout=3, check=False,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + 10
            while is_antigravity_running(client) and time.monotonic() < deadline:
                time.sleep(.2)
            return not is_antigravity_running(client)
        except (OSError, psutil.Error, subprocess.TimeoutExpired, LaunchError):
            return False
    script = (
        "$processes = Get-Process | Where-Object { $_.ProcessName -match '^(Antigravity|antigravity)' }; "
        "if (-not $processes) { exit 0 }; "
        "$processes | ForEach-Object { [void]$_.CloseMainWindow() }; "
        "$deadline = (Get-Date).AddSeconds(10); "
        "do { "
        "  Start-Sleep -Milliseconds 250; "
        "  $remaining = Get-Process | Where-Object { $_.ProcessName -match '^(Antigravity|antigravity)' } "
        "} while ($remaining -and (Get-Date) -lt $deadline); "
        "if ($remaining) { exit 2 }"
    )
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=13,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def launch_antigravity(exe_path: Path | None = None, *, client=None) -> None:
    target = client.executable if client is not None else exe_path or default_antigravity_exe()
    if not target.is_file():
        raise LaunchError(f"未找到 Antigravity 可执行文件：{target}，请检查安装路径。")
    try:
        arguments = [str(target)]
        if client is not None:
            arguments += ['--user-data-dir', str(client.user_data)]
        subprocess.Popen(arguments)
    except OSError as err:
        raise LaunchError(f"启动 Antigravity 失败：{err}") from err


def open_path(path: Path) -> None:
    try:
        if not path.is_dir():
            raise LaunchError("账号仓库目录不可用，请检查加密卷。")
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            raise LaunchError("该平台不支持直接调用文件资源管理器。")
    except OSError as err:
        raise LaunchError(f"打开目录失败：{err}") from err
