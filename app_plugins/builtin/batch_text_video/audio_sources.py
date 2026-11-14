"""Audio source adapters: shared by editor, preview and final rendering."""
from pathlib import Path

from .custom_pools import get_pool

VOICE_MODES = (("沿用任务人声", "task"), ("不使用人声", "none"),
               ("池内依次轮换", "rotate"), ("共用池内第一条", "single"))
AUDIO_ROLES = {
    "voice": {"title": "人声控件", "pool_key": "voice_pool_id", "color": "#5088b5",
              "default": "选择人声／视频音轨池…", "note": "从头完整播放，成品长度跟随人声。"},
    "music": {"title": "背景音乐控件", "pool_key": "music_pool_id", "color": "#ac8441",
              "default": "默认音乐池", "note": "编码前固定音乐分配并预留续播位置；失败重试沿用。"},
}


def video_audio_candidate(path):
    from .matching import VIDEO_EXTENSIONS
    return Path(path).suffix.casefold() in VIDEO_EXTENSIONS


def voice_library(state, identifier):
    if identifier == 'builtin:background':
        pool = dict(name='默认背景池', kind='visual', items=[{'id': path, 'path': path} for path in state.get('backgrounds', [])])
    elif identifier == 'builtin:image':
        pool = dict(name='默认图片池', kind='visual', items=state.get('image_pool', []))
    else:
        pool = get_pool(state, identifier)
    if pool['kind'] not in {'voice', 'image', 'video', 'visual'}:
        raise ValueError('人声来源必须是人声池或含视频的素材池。')
    items = pool['items'] if pool['kind'] == 'voice' else [item for item in pool['items'] if video_audio_candidate(item['path'])]
    return pool, items


def voice_choices(state):
    result = []
    for identifier in ('builtin:background', 'builtin:image'):
        pool, items = voice_library(state, identifier)
        if items: result.append((identifier, pool['name']+' · 视频音轨'))
    for pool in state.get('resource_pools', []):
        if pool['kind'] == 'voice':
            result.append((pool['id'], pool['name']))
        elif pool['kind'] in {'image', 'video', 'visual'} and any(video_audio_candidate(i['path']) for i in pool['items']):
            result.append((pool['id'], pool['name']+' · 视频音轨'))
    return result


def pool_voice(state, identifier, index=0, mode="rotate"):
    if mode not in {"rotate", "single"}:
        raise ValueError("人声池使用方式无效。")
    pool, items = voice_library(state, identifier)
    if not items:
        raise ValueError(f"人声池“{pool['name']}”为空或没有视频／音频，请先添加素材。")
    item = items[0 if mode == "single" else index % len(items)]
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
        if discover and not job.get('voice_path') and job.get('_voice_override_before_assignment') is False:
            from .task_associations import refresh
            job=refresh(job,settings)['job']
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
    if not path and not discover and job.get("task_voice_optional") and not job.get("match_error"):
        return ""
    if discover and (not path or job.get('task_audio_auto') is not False and not Path(path).is_file()):
        from .matching import folder_entry
        folder = job.get("task_dir") or (Path(job['path']).parent if job.get('path') else None)
        if not folder:
            return ""
        from .task_associations import sheet_audio_name
        name=job.get('task_audio_name') or sheet_audio_name(job)
        match = folder_entry(folder,dict(text_only=bool(job.get('task_voice_optional')),audio_name=name))
        if match["match_error"]:
            raise ValueError(match["match_error"])
        path = match["voice_path"] or path
    return path
