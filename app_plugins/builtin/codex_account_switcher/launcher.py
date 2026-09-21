"""Windows ChatGPT desktop lifecycle helpers used by the account plugin."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class LaunchError(RuntimeError):
    pass


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def detect_chatgpt_aumid() -> str | None:
    if os.name != "nt":
        return None
    script = (
        "$pkg = Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction SilentlyContinue "
        "| Select-Object -First 1; "
        "if ($pkg) { "
        "$manifest = Get-AppxPackageManifest -Package $pkg; "
        "$entry = $manifest.Package.Applications.Application "
        "| Where-Object { $_.Executable -match 'ChatGPT\\.exe$' } "
        "| Select-Object -First 1; "
        "if ($entry) { Write-Output ($pkg.PackageFamilyName + '!' + $entry.Id); exit 0 } "
        "}; "
        "$app = Get-StartApps | Where-Object { $_.Name -match 'Codex' } "
        "| Select-Object -First 1; "
        "if ($app) { Write-Output $app.AppID }"
    )
    try:
        result = _run(["powershell.exe", "-NoProfile", "-Command", script], 15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value.splitlines()[0].strip() if value else None


def is_chatgpt_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$items = Get-Process ChatGPT -ErrorAction SilentlyContinue "
        "| Where-Object { $_.Path -like '*OpenAI.Codex_*' }; "
        "if ($items) { Write-Output 'running' }"
    )
    try:
        result = _run(["powershell.exe", "-NoProfile", "-Command", script], 8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "running" in result.stdout


def close_chatgpt() -> bool:
    """Request a normal Codex desktop close. It intentionally never kills it."""
    if os.name != "nt":
        return True
    script = (
        "$processes = Get-Process ChatGPT -ErrorAction SilentlyContinue "
        "| Where-Object { $_.Path -like '*OpenAI.Codex_*' }; "
        "if (-not $processes) { exit 0 }; "
        "$processes | ForEach-Object { [void]$_.CloseMainWindow() }; "
        "$deadline = (Get-Date).AddSeconds(10); "
        "do { "
        "Start-Sleep -Milliseconds 250; "
        "$remaining = Get-Process ChatGPT -ErrorAction SilentlyContinue "
        "| Where-Object { $_.Path -like '*OpenAI.Codex_*' } "
        "} while ($remaining -and (Get-Date) -lt $deadline); "
        "if ($remaining) { exit 2 }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=13,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def launch_chatgpt_windows() -> None:
    if os.name != "nt":
        raise LaunchError("ChatGPT 桌面版启动功能仅支持 Windows。")
    aumid = detect_chatgpt_aumid()
    if not aumid:
        raise LaunchError("没有检测到 Codex Windows 桌面版，请先完成安装。")
    try:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{aumid}"])
    except OSError as error:
        raise LaunchError(f"启动 Codex 桌面版失败：{error}") from error


def open_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            raise LaunchError("账号档案仓库只能用 Windows 文件资源管理器打开。")
    except OSError as error:
        raise LaunchError(f"打开账号档案仓库失败：{error}") from error
