"""Fail a release when its ZIP contains local state or private configuration."""

from __future__ import annotations

import sys
from pathlib import Path
from zipfile import BadZipFile, ZipFile


REQUIRED_FILES = {
    "assistanttool.exe",
    "readme.md",
    "changelog.md",
    "build_info.json",
    "app_icon.png",
    "app_icon.ico",
    "_internal/任务登记表格.ods",
    "config.example.json",
    "_internal/runtime/mpv/mpv.exe",
    "_internal/runtime/ffmpeg/ffmpeg.exe",
    "_internal/runtime/ffmpeg/ffprobe.exe",
    "_internal/runtime/ffmpeg/license",
}
PRIVATE_FILES = {
    "config2.json",
    "dailyquantitycategories.json",
    "googledrivecredentials.json",
    "googledrivetoken.json",
    "googlesheetscredentials.json",
    "googlesheetstoken.json",
    "tasktableschema.json",
    "tasksubmissionlog.jsonl",
    "taskresultpendingfiles.json",
    "googlesheetmonitorstate.json",
    "reviewsubmissionhistory.json",
    "videouploadhistory.json",
    "dailyquantitystats.json",
    "dailylinkarchive.json",
    "inventorystate.json",
    "materialdrivesyncstate.json",
    "dailytasks.json",
    "taskreferencedownloadstate.json",
}
PRIVATE_DIRECTORIES = {
    ".venv",
    ".git",
    ".github",
    "logs",
    "materiallibrary",
    "materialorganizer",
    "smartimagesearch",
    "smartmusicsearch",
    "videopromptassistant",
    "batchtextvideo",
    "cookingassistant",
    "videouploadhistoryarchives",
    "人脸数据库",
}


def validate_release_archive(path: str | Path) -> int:
    """Return the entry count or raise ValueError without showing file data."""
    archive_path = Path(path)
    if not archive_path.is_file():
        raise ValueError(f"发布包不存在：{archive_path}")
    try:
        with ZipFile(archive_path) as archive:
            entries = archive.infolist()
            seen = set()
            violations = []
            for info in entries:
                name = info.filename.replace("\\", "/")
                parts = [part.casefold() for part in name.split("/") if part]
                if (
                    name.startswith("/")
                    or any(part in (".", "..") for part in parts)
                    or (parts and ":" in parts[0])
                ):
                    violations.append(name)
                    continue
                normalized = "/".join(parts)
                seen.add(normalized)
                if (
                    any(part in PRIVATE_DIRECTORIES for part in parts)
                    or (parts and parts[-1] in PRIVATE_FILES)
                    or (len(parts) == 1 and parts[0] == "config.json")
                    or (parts and (parts[-1] == ".env" or
                        (parts[-1].startswith('.env.') and parts[-1] != '.env.example')))
                    or (parts[:2] == ['config', 'waste_reminder'])
                    or (parts and parts[-1].startswith('.~lock.'))
                    or (parts and parts[-1].startswith('deliverytodos.sqlite3'))
                    or (len(parts) == 1 and parts[0].startswith('config')
                        and parts[0][6:-5].isdigit() and parts[0].endswith('.json'))
                ):
                    violations.append(name)
    except BadZipFile as error:
        raise ValueError("发布包不是有效的 ZIP 文件") from error

    missing = sorted(REQUIRED_FILES - seen)
    if missing or violations:
        details = []
        if missing:
            details.append("缺少必要文件：" + "、".join(missing))
        if violations:
            details.append("发现私有数据或不安全路径：" + "、".join(violations[:10]))
        raise ValueError("；".join(details))
    return len(entries)


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：python packaging/check_release_artifact.py <发布包.zip>", file=sys.stderr)
        return 2
    try:
        count = validate_release_archive(sys.argv[1])
    except (OSError, ValueError) as error:
        print(f"发布包检查失败：{error}", file=sys.stderr)
        return 1
    print(f"发布包检查通过：{count} 个条目，未发现已知本地状态文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
