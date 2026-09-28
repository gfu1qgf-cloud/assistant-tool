"""Durable link-only archive, independent of the seven-day config summary."""

import json
import os
import re
import shutil
from pathlib import Path

from app_paths import APP_ROOT
from model.DailyLinkHistory import (
    DAILY_LINK_HISTORY_CONFIG_KEY,
    normalize_daily_link_history,
)


ARCHIVE_NAME = "DailyLinkArchive.json"
_LOG_LINK = re.compile(
    r"\[插件/task_delivery\]\s*(.+?)：(https://drive\.google\.com/drive/folders/[A-Za-z0-9_-]+)"
)
_LOG_SAVED = re.compile(
    r"每日链接已保存\s+(\d{4}-\d{2}-\d{2})\s*/\s*批次\s+([^：:\s]+)"
)


def merge_link_histories(*histories):
    """Merge by date/person/slot without deleting entries from older snapshots."""
    merged = {}
    for history in histories:
        for day, entry in normalize_daily_link_history(
            history, retain_all=True
        ).items():
            target = merged.setdefault(day, {
                "updated_at": "", "people": {}, "task_sheet_failures": {},
            })
            target["updated_at"] = max(
                target["updated_at"], entry.get("updated_at", "")
            )
            for person, slots in entry.get("people", {}).items():
                person_target = target["people"].setdefault(person, {})
                for slot, link_entry in slots.items():
                    previous = person_target.get(slot)
                    if (previous is None or
                            link_entry.get("saved_at", "") >= previous.get("saved_at", "")):
                        person_target[slot] = dict(link_entry)
    return normalize_daily_link_history(merged, retain_all=True)


def _read_history(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _log_histories(log_dir):
    """Recover printed person-folder links only when followed by a saved batch."""
    recovered = {}
    if not log_dir.is_dir():
        return recovered
    files = sorted(log_dir.glob("assistant-tool.log*"),
                   key=lambda path: (path.stat().st_mtime, path.name))
    for path in files:
        pending = {}
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if "各人员 Google Drive 文件夹链接" in line:
                    pending = {}
                    continue
                match = _LOG_LINK.search(line)
                if match is not None:
                    pending[match.group(1).strip()] = match.group(2)
                    continue
                match = _LOG_SAVED.search(line)
                if match is None or not pending:
                    continue
                day, slot = match.groups()
                if line[:10] != day:
                    pending = {}
                    continue
                saved_at = line[:19].replace(" ", "T")
                entry = {
                    day: {
                        "updated_at": saved_at,
                        "people": {
                            person: {slot: {"link": link, "saved_at": saved_at}}
                            for person, link in pending.items()
                        },
                    }
                }
                recovered = merge_link_histories(recovered, entry)
                pending = {}
    return recovered


def recover_daily_link_archive(root=APP_ROOT):
    """One-time recovery from existing config backups and delivery logs."""
    root = Path(root)
    recovered = {}
    backups = sorted(root.glob("config.json.bak*"),
                     key=lambda path: (path.stat().st_mtime, path.name))
    for path in backups:
        recovered = merge_link_histories(
            recovered, _read_history(path).get(DAILY_LINK_HISTORY_CONFIG_KEY)
        )
    recovered = merge_link_histories(recovered, _log_histories(root / "logs"))
    return merge_link_histories(
        recovered,
        _read_history(root / "config.json").get(DAILY_LINK_HISTORY_CONFIG_KEY),
    )


def save_daily_link_archive(history, root=APP_ROOT):
    root = Path(root)
    path = root / ARCHIVE_NAME
    normalized = merge_link_histories(history)
    if not normalized:
        return normalized
    temporary = path.with_name(path.name + ".tmp")
    if path.is_file() and _read_history(path):
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return normalized


def load_daily_link_archive(current_history=None, root=APP_ROOT):
    root = Path(root)
    path = root / ARCHIVE_NAME
    archive = _read_history(path)
    if not archive:
        archive = _read_history(path.with_name(path.name + ".bak"))
    if not archive:
        archive = recover_daily_link_archive(root)
    merged = merge_link_histories(archive, current_history)
    if merged and (merged != archive or not path.is_file()):
        save_daily_link_archive(merged, root)
    return merged
