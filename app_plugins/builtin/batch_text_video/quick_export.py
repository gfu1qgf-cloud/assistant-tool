"""Snapshot one visible composition without pairing or exporting the other jobs."""
import copy
from pathlib import Path
import uuid

from .store import file_identity


def prepare_current_export(state, entry, copy_id="", queue_id=""):
    path = str(entry.get("path") or "")
    if not path or not Path(path).is_file():
        raise ValueError("请先选择一个有效的背景视频。")
    if entry.get("requires_title") and not str(entry.get("title") or "").strip():
        raise ValueError("表格文案没有标题，请补上第一行标题后再导出。")
    result = copy.deepcopy(state)
    pair_key = [copy_id, file_identity(path)]
    # A copy-pool preview takes precedence over a previously highlighted queue row.
    job = next((item for item in result["jobs"] if not copy_id and queue_id
                and item["id"] == queue_id), None)
    if job is None:
        job = next((item for item in result["jobs"] if item.get("pair_key") == pair_key), None)
    if job is None and copy_id:
        job = next((item for item in result["jobs"] if item["id"] == copy_id
                    and file_identity(item.get("path") or "") == pair_key[1]), None)
    if job is None:
        job = {"id": uuid.uuid4().hex, "output": "", "copy_id": copy_id, "pair_key": pair_key}
        result["jobs"].append(job)
    # The queue retains the rendered composition and its ordinary completion record.
    # Do not replace a queue ID with the source copy's ID or create suffixed names.
    job.update({key: copy.deepcopy(value) for key, value in entry.items()
                if key not in {"id", "output", "status", "error", "pair_key", "copy_id"}})
    job.update(path=path, title=entry.get("title", ""), body=entry.get("body", ""),
               name=entry.get("name") or Path(path).stem + "_文字版",
               status="待生成", error="")
    return result, job["id"]
