"""Safely prefix source videos with their reviewed export order."""

import re
import uuid
from pathlib import Path


_ORDER_PREFIX = re.compile(r"^\[\d+\] ")


def build_video_rename_plan(bundle):
    clips = [
        clip
        for task in bundle.get("tasks", [])
        for clip in task.get("clips", [])
    ]
    width = max(2, len(str(max((int(clip.get("export_order") or 0) for clip in clips), default=0))))
    plan = []
    source_targets = {}
    for clip in clips:
        source = Path(str(clip.get("source") or ""))
        if not source.is_file():
            raise ValueError(f"原视频不存在：{source}")
        order = int(clip.get("export_order") or 0)
        if order < 1:
            raise ValueError(f"视频顺序无效：{source.name}")
        base_name = _ORDER_PREFIX.sub("", source.name)
        target = source.with_name(f"[{order:0{width}d}] {base_name}")
        previous = source_targets.setdefault(source, target)
        if previous != target:
            raise ValueError(f"同一视频被分配了不同顺序：{source}")
        if source != target and not any(old == source for old, _new in plan):
            plan.append((source, target))

    if len({target for _source, target in plan}) != len(plan):
        raise ValueError("命名后会产生重名文件，请先检查片段顺序。")
    moving_sources = {source for source, _target in plan}
    for _source, target in plan:
        if target.exists() and target not in moving_sources:
            raise ValueError(f"目标文件已存在，未修改任何视频：{target}")
    return plan


def apply_video_rename_plan(plan):
    """Use temporary names to support swaps, rolling back on failure."""
    staged = []
    finished = []
    try:
        for source, target in plan:
            temporary = source.with_name(f".{uuid.uuid4().hex}.smart-video-rename{source.suffix}")
            source.rename(temporary)
            staged.append((source, temporary, target))
        for source, temporary, target in staged:
            temporary.rename(target)
            finished.append((source, target))
    except OSError as error:
        rollback_errors = []
        rollback_staged = []
        for source, target in finished:
            try:
                temporary = source.with_name(f".{uuid.uuid4().hex}.smart-video-rollback{source.suffix}")
                target.rename(temporary)
                rollback_staged.append((source, temporary))
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        for source, temporary in rollback_staged + [
            (source, temporary) for source, temporary, _target in staged[len(finished):]
        ]:
            try:
                temporary.rename(source)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        detail = f"视频命名失败：{error}"
        if rollback_errors:
            detail += "\n部分文件未能回滚，请检查原目录：" + "; ".join(rollback_errors)
        raise OSError(detail) from error


def update_bundle_video_paths(bundle, plan):
    """Keep review/export references in sync with renamed source files."""
    paths = {str(source): str(target) for source, target in plan}
    for task in bundle.get("tasks", []):
        task_paths = {
            old: new
            for old, new in paths.items()
            if any(str(clip.get("source")) == old for clip in task.get("clips", []))
        }
        names = {}
        ambiguous = set()
        for old, new in task_paths.items():
            old_name, new_name = Path(old).name, Path(new).name
            if old_name in names and names[old_name] != new_name:
                ambiguous.add(old_name)
            names[old_name] = new_name
        for name in ambiguous:
            names.pop(name, None)

        def replace(value):
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, str):
                return task_paths.get(value, names.get(value, value))
            return value

        task.update(replace(task))
