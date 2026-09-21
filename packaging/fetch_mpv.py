"""Fetch the pinned official mpv runtime used by the timeline player."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import urllib.request
import zipfile


VERSION = "0.41.0"
ARCHIVE_URL = (
    "https://github.com/mpv-player/mpv/releases/download/v0.41.0/"
    "mpv-v0.41.0-x86_64-pc-windows-msvc.zip"
)
ARCHIVE_SHA256 = "4e197f729f5071c6772f35fffd96e0f36e3e8a044bd9479b136bb09b7c6a80ff"
RUNTIME_FILES = ("mpv.exe", "vulkan-1.dll")


def main():
    project_root = Path(__file__).resolve().parents[1]
    output_dir = project_root / "runtime" / "mpv"
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lzx-mpv-") as temporary:
        archive_path = Path(temporary) / "mpv.zip"
        print(f"下载 mpv {VERSION}：{ARCHIVE_URL}")
        urllib.request.urlretrieve(ARCHIVE_URL, archive_path)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if digest.lower() != ARCHIVE_SHA256:
            raise RuntimeError(
                f"mpv 下载文件校验失败：{digest}，拒绝安装。"
            )
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            for name in RUNTIME_FILES:
                if name not in names:
                    raise RuntimeError(f"mpv 发布包缺少文件：{name}")
                target = output_dir / name
                target.write_bytes(archive.read(name))
                print(f"已保存：{target}")
    print("mpv 运行库安装完成。")


if __name__ == "__main__":
    main()
