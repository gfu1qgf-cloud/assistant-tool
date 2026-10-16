"""Task refresh data helpers; no widgets, live models or file mutations."""
import copy
import os
from pathlib import Path


def analysis_summary(bundle, task_cache_count=0, transcription_cache_count=0):
    clips = [clip for task in bundle["tasks"] for clip in task["clips"]]
    missing_count = sum(
        len(task.get("missing_blocks", [])) for task in bundle["tasks"]
    )
    unverified_count = sum(
        len(task.get("unverified_blocks", [])) for task in bundle["tasks"]
    )
    problem_count = sum(
        issue.get("severity") != "info"
        for clip in clips
        for issue in clip.get("issues", [])
    )
    cut_decision_count = sum(
        issue.get("severity") == "info"
        for clip in clips
        for issue in clip.get("issues", [])
    )
    duplicate_count = sum(
        int(task.get("duplicate_count") or 0) for task in bundle["tasks"]
    )
    return {
        "task_count": len(bundle["tasks"]),
        "clip_count": len(clips),
        "green_count": sum(clip["status"] == "green" for clip in clips),
        "orange_count": sum(clip["status"] == "orange" for clip in clips),
        "pink_count": sum(clip["status"] == "pink" for clip in clips),
        "missing_count": missing_count,
        "unverified_count": unverified_count,
        "export_blocked": missing_count > 0,
        "problem_count": problem_count,
        "cut_decision_count": cut_decision_count,
        "duplicate_count": duplicate_count,
        "task_cache_count": task_cache_count,
        "transcription_cache_count": transcription_cache_count,
        "needs_review": (
            any(clip["status"] != "green" for clip in clips)
            or missing_count > 0
            or unverified_count > 0
            or cut_decision_count > 0
        ),
    }


def refresh_job(task, settings):
    from .engine import discover_task_videos
    if not isinstance(task, dict) or not str(task.get("task_dir") or "").strip():
        raise ValueError("当前任务缺少目录，无法重新选择片段。")
    directory = Path(task["task_dir"]).resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"任务目录不存在：\n{directory}")
    sources, seen = [], set()
    current = [clip.get("source") for clip in task.get("clips", []) if clip.get("source")]
    for raw in current + list(discover_task_videos(directory, settings)):
        path = Path(raw).resolve()
        key = os.path.normcase(str(path))
        if key in seen or not path.is_file():
            continue
        seen.add(key); sources.append(str(path))
    return {"task_id": str(task.get("task_id") or ""), "task_name": str(task.get("task_name") or ""),
            "label": str(task.get("label") or task.get("task_id") or "当前任务"),
            "task_dir": str(directory), "script": str(task.get("script") or ""),
            "language": str(task.get("language") or ""), "sources": sources,
            "force_task_reanalysis": True}


def task_identity(task):
    directory = str(task.get("task_dir") or "").strip()
    if not directory:
        raise ValueError("分析结果缺少任务目录，原核对结果已保留。")
    return str(task.get("task_id") or ""), os.path.normcase(str(Path(directory).resolve()))


def merge_task_result(bundle, task_index, result):
    tasks, fresh = bundle.get("tasks", []), result.get("tasks", [])
    if type(task_index) is not int or not 0 <= task_index < len(tasks):
        raise ValueError("当前刷新任务已不存在，原结果已保留。")
    if not isinstance(fresh, list) or len(fresh) != 1 or not isinstance(fresh[0], dict) or not fresh[0].get("clips"):
        raise ValueError("重新分析没有返回当前任务的有效片段，原结果已保留。")
    if task_identity(tasks[task_index]) != task_identity(fresh[0]):
        raise ValueError("重新分析返回了其他任务，原结果已保留。")
    merged = copy.deepcopy(bundle)
    merged["tasks"][task_index] = copy.deepcopy(fresh[0])
    # Keep the active panel settings and all OTHER tasks exactly as collected.
    merged["summary"] = analysis_summary(merged,
        sum(bool(task.get("analysis_from_cache")) for task in merged["tasks"]),
        sum(bool(clip.get("from_cache")) for task in merged["tasks"] for clip in task.get("clips", [])))
    return merged
