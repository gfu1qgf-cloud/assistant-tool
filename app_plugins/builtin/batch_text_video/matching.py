"""Batch narration discovery: folder identity, never guessed positional matches."""
import os
from pathlib import Path
import re
import uuid

from .store import file_identity

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts"}
SKIP_DIRECTORIES = {"静态文字版", ".git", ".venv", "music-cache", "video-cache", "__pycache__"}


def is_link(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def natural_key(path):
    return [int(value) if value.isdigit() else value.casefold()
            for value in re.split(r"(\d+)", str(path))]


def folder_entry(folder, metadata=None, explicit_voice=None):
    folder = Path(folder)
    metadata = metadata or {}
    files = sorted((p for p in folder.iterdir() if p.is_file()), key=natural_key) if folder.is_dir() else []
    candidates = [p for p in files if p.suffix.casefold() in AUDIO_EXTENSIONS
                  and p.stem.casefold() == "task_audio"]
    if not candidates and metadata.get("task_name"):
        candidates = [p for p in files if p.suffix.casefold() in AUDIO_EXTENSIONS
                      and p.stem.casefold() == str(metadata["task_name"]).casefold()]
    if explicit_voice:
        candidates = [Path(explicit_voice)]
    voice = str(candidates[0].resolve()) if len(candidates) == 1 else ""
    error = "" if voice else ("找到多个人声音频，请确认使用哪一个。" if candidates else "没有找到任务人声。")
    if metadata.get("text_only") and not candidates:
        error = ""
    videos = [p for p in files if p.suffix.casefold() in VIDEO_EXTENSIONS
              and not p.stem.endswith("_文字版")]
    background = str(videos[0].resolve()) if len(videos) == 1 else ""
    label = str(metadata.get("label") or (Path(explicit_voice).stem if explicit_voice
                and Path(explicit_voice).stem.casefold() != "task_audio" else folder.name))
    stem = (folder.name + "_" + Path(voice).stem) if voice else folder.name
    return {"id": uuid.uuid4().hex, "path": background, "voice_path": voice,
            "task_dir": str(folder.resolve()), "label": label,
            "title": str(metadata.get("title") or ""), "body": str(metadata.get("body") or ""),
            "name": stem + "_文字版", "status": "待生成" if (voice or metadata.get("text_only")) and background else "需配置",
            "output": "", "error": error or ("请批量分配背景视频。" if not background else ""),
            "match_error": error}


def scan_inputs(paths=(), targets=(), cancel=None, report=None):
    report = report or (lambda _text: None)
    entries, seen, issues = [], set(), []

    def add(folder, metadata=None, voice=None):
        key = file_identity(voice or folder)
        if key in seen:
            return
        seen.add(key)
        try:
            entries.append(folder_entry(folder, metadata, voice))
        except OSError as error:
            issues.append(f"{Path(folder).name}：{error}")

    for target in targets:
        if cancel and cancel.is_set():
            break
        add(target["target_dir"], target)
    for raw in paths:
        if cancel and cancel.is_set():
            break
        path = Path(raw)
        if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS:
            add(path.parent, voice=path)
        elif path.is_dir():
            for folder, directories, files in os.walk(path, followlinks=False):
                if cancel and cancel.is_set():
                    break
                directories[:] = sorted((name for name in directories if name not in SKIP_DIRECTORIES
                    and not name.startswith(".text-video-")
                    and not is_link(Path(folder, name))), key=natural_key)
                if any(Path(name).stem.casefold() == "task_audio"
                       and Path(name).suffix.casefold() in AUDIO_EXTENSIONS for name in files):
                    add(folder)
                    report(f"已发现 {len(entries)} 个人声任务")
    return {"entries": entries, "issues": issues, "cancelled": bool(cancel and cancel.is_set())}


def merge_entries(state, entries):
    known = {file_identity(job.get("voice_path") or job.get("task_dir") or job["path"])
             for job in state["jobs"]}
    count = 0
    for entry in entries:
        key = file_identity(entry.get("voice_path") or entry["task_dir"])
        if key in known:
            continue
        entry = dict(entry)
        # If the user added the videos first, attach matching folder narration to
        # those rows instead of silently creating a second output for every video.
        existing = [job for job in state["jobs"] if entry["path"] and job.get("path")
                    and file_identity(job["path"]) == file_identity(entry["path"])
                    and not job.get("voice_path") and not job.get("task_dir")]
        if len(existing) == 1:
            job = existing[0]
            for field in ("voice_path", "task_dir", "label", "match_error", "status", "error"):
                job[field] = entry[field]
            for field in ("title", "body"):
                if not job.get(field):
                    job[field] = entry[field]
            known.add(key)
            count += 1
            continue
        pool = state.get("backgrounds", [])
        if not entry["path"] and pool:
            entry["path"] = pool[count % len(pool)]
        if entry["path"] and not entry["match_error"]:
            entry.update(status="待生成", error="")
        state["jobs"].append(entry)
        known.add(key)
        count += 1
    return count


def assign_backgrounds(state, paths, ids=None):
    paths = [str(Path(p).resolve()) for p in paths if Path(p).is_file() and Path(p).suffix.casefold() in VIDEO_EXTENSIONS]
    if not paths:
        raise ValueError("没有可用的背景视频。")
    state["backgrounds"] = list(dict.fromkeys(paths))
    jobs = [job for job in state["jobs"] if (job.get("voice_path") or job.get("task_dir"))
            and (ids is None or job["id"] in ids)]
    for index, job in enumerate(jobs):
        job["path"] = state["backgrounds"][index % len(state["backgrounds"])]
        job.update(status="需配置" if job.get("match_error") else "待生成", error=job.get("match_error", ""))
    return len(jobs)
