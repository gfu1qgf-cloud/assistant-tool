"""Audio source adapters: shared by editor, preview and final rendering."""
from pathlib import Path

from .custom_pools import get_pool

VOICE_MODES = (("沿用任务人声", "task"), ("不使用人声", "none"),
               ("池内依次轮换", "rotate"), ("共用池内第一条", "single"))
AUDIO_ROLES = {
    "voice": {"title": "人声控件", "pool_key": "voice_pool_id", "color": "#5088b5",
              "default": "选择自建人声池…", "note": "从头完整播放，成品长度跟随人声。"},
    "music": {"title": "背景音乐控件", "pool_key": "music_pool_id", "color": "#ac8441",
              "default": "默认音乐池", "note": "轮换并记住续播位置；导出成功才推进。"},
}


def pool_voice(state, identifier, index=0, mode="rotate"):
    if mode not in {"rotate", "single"}:
        raise ValueError("人声池使用方式无效。")
    pool = get_pool(state, identifier, "voice")
    if not pool["items"]:
        raise ValueError(f"人声池“{pool['name']}”为空，请先添加音频。")
    item = pool["items"][0 if mode == "single" else index % len(pool["items"])]
    if not Path(item["path"]).is_file():
        raise FileNotFoundError(f"人声池“{pool['name']}”的文件不存在：{Path(item['path']).name}")
    return item["path"]


def voice_slot(job, state):
    # A copy preview and its quick export must resolve the same rotation item.
    copy_id = job.get("copy_id") or job.get("id")
    for index, entry in enumerate(state.get("copy_pool", [])):
        if entry.get("id") == copy_id:
            return index
    for index, entry in enumerate(state.get("jobs", [])):
        if entry.get("id") == job.get("id"):
            return index
    return 0


def resolve_voice(job, state, *, discover=False):
    """Explicit per-row assignment wins; otherwise follow this profile's card."""
    settings = state["settings"]
    if not settings.get("use_voice"):
        return ""
    if job.get("voice_override"):
        return str(job.get("voice_path") or "")
    mode = settings.get("voice_pool_mode", "task")
    if mode not in {value for _label, value in VOICE_MODES}:
        raise ValueError("人声控件的来源无效，请重新选择。")
    if mode == "none":
        return ""
    if mode in {"rotate", "single"}:
        return pool_voice(state, settings.get("voice_pool_id", ""), voice_slot(job, state), mode)
    path = str(job.get("task_voice_path", job.get("voice_path")) or "")
    # Table-copy imports can mix tasks with and without existing narration.
    # Do not turn a valid text-only task into a failed mandatory-voice job.
    if not path and job.get("task_voice_optional") and not job.get("match_error"):
        return ""
    if not path and discover:
        from .matching import folder_entry
        folder = job.get("task_dir") or (Path(job['path']).parent if job.get('path') else None)
        if not folder:
            return ""
        match = folder_entry(folder)
        if match["match_error"]:
            raise ValueError(match["match_error"])
        path = match["voice_path"]
    return path
