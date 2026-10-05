"""Fetch the pinned Windows FFmpeg build linked by ffmpeg.org.

No installer, PATH changes or replacement of other applications' encoders.
"""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

VERSION = "9.0.2"
ARCHIVE_URL = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip"
ARCHIVE_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
RUNTIME_FILES = ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe")
MAX_DOWNLOAD = 180 * 1024 * 1024


def install_archive(archive_path, output_dir):
    """Only extract known basenames, after checking the complete archive hash."""
    archive_path, output_dir = Path(archive_path), Path(output_dir)
    digest = hashlib.sha256()
    with archive_path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != ARCHIVE_SHA256:
        raise ValueError("FFmpeg 下载校验失败，拒绝安装或执行。")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ffmpeg-verified-", dir=output_dir.parent) as folder:
        staging = Path(folder)
        with zipfile.ZipFile(archive_path) as archive:
            for name in (*RUNTIME_FILES, "LICENSE", "README.txt"):
                matches = [item for item in archive.infolist()
                           if not item.is_dir() and Path(item.filename).name == name]
                if len(matches) != 1 or matches[0].file_size > MAX_DOWNLOAD:
                    raise ValueError("FFmpeg 发布包缺少或含有异常文件：" + name)
                with archive.open(matches[0]) as source, (staging / name).open("wb") as target:
                    shutil.copyfileobj(source, target)
        (staging / "BUILD_INFO.txt").write_text(
            f"FFmpeg {VERSION}, Windows x64 release essentials build (gyan.dev)\n"
            f"Archive: {ARCHIVE_URL}\nSHA256: {ARCHIVE_SHA256}\n"
            "Distributor: https://www.gyan.dev/ffmpeg/builds/\n"
            "FFmpeg source revision: https://github.com/FFmpeg/FFmpeg/commit/946fcce07b\n"
            "GPLv3 build, invoked as separate processes. Preserve LICENSE and README.txt when distributing.\n"
            "See vendor README.txt for build configuration and external library/source information.\n",
            "utf-8")
        output_dir.mkdir(parents=True, exist_ok=True)
        for path in staging.iterdir():
            os.replace(path, output_dir / path.name)


def main():
    output = Path(__file__).resolve().parents[1] / "runtime" / "ffmpeg"
    with tempfile.TemporaryDirectory(prefix="ffmpeg-download-") as folder:
        archive = Path(folder) / "ffmpeg.zip"
        print(f"下载 FFmpeg {VERSION}，校验后安装到 {output}", flush=True)
        request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "AssistantTool-build/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as target:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise ValueError("FFmpeg 发布包超过大小限制。")
                target.write(chunk)
        install_archive(archive, output)
    print("标准 FFmpeg / FFprobe / FFplay 安装完成；未修改系统 PATH。", flush=True)


if __name__ == "__main__":
    main()
