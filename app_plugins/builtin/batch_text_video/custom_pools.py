"""Named, typed resource pools. Records only: source files are never moved."""
import copy
from pathlib import Path
import uuid

POOL_TYPES = {
    "image": ("视觉元素", "图片 / GIF / 水印", "在视觉元素的来源中选择此池，逐视频使用下一项"),
    "video": ("视觉元素", "视频", "用于背景搭配或绑定画面中的视频元素"),
    "visual": ("视觉元素", "图片／视频／GIF", "混合视觉素材，可绑定到同一个画面元素"),
    "text": ("文字元素", "文案文件", "UTF-8 TXT／Markdown，绑定文本框时逐视频取用"),
    "subtitle": ("文字元素", "字幕文件", "已有SRT，绑定字幕控件时逐视频取用"),
    "music": ("听觉元素", "背景音乐", "续用音乐位置；有人声时支持 EQ 与自动压低"),
    "voice": ("听觉元素", "人声", "从头完整播放；成品长度跟随人声，不裁静音"),
}
MAX_POOL_ITEMS = 1000


def normalize_pools(state):
    pools = state.setdefault("resource_pools", [])
    if not isinstance(pools, list) or len(pools) > 100:
        raise ValueError("自建资源池记录异常，未清空原记录。")
    ids, names = set(), set()
    for pool in pools:
        if not isinstance(pool, dict) or pool.get("kind") not in POOL_TYPES:
            raise ValueError("资源池类型无效。")
        identifier, name = pool.get("id"), pool.get("name")
        if (not isinstance(identifier, str) or not identifier or identifier in ids
                or not isinstance(name, str) or not name.strip() or len(name) > 80 or name in names):
            raise ValueError("资源池名称或编号无效 / 重复。")
        ids.add(identifier)
        names.add(name)
        items = pool.get("items")
        if not isinstance(items, list) or len(items) > MAX_POOL_ITEMS:
            raise ValueError(f"每个资源池最多 {MAX_POOL_ITEMS} 项。")
        item_ids = set()
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not item["id"] or item["id"] in item_ids
                    or not isinstance(item.get("path"), str) or not item["path"]):
                raise ValueError("资源池条目记录无效。")
            item_ids.add(item["id"])
        pool.setdefault("next_id", "")
        if not isinstance(pool["next_id"], str):
            raise ValueError("资源池轮换位置无效。")
    return pools


def get_pool(state, identifier, kind=None):
    pool = next((pool for pool in state.get("resource_pools", []) if pool["id"] == identifier), None)
    if pool is None or kind is not None and pool["kind"] != kind:
        raise ValueError("所选资源池不存在或类型不匹配，请重新选择。")
    return pool


def create_pool(state, name, kind):
    name = str(name).strip()
    if kind not in POOL_TYPES or not name or len(name) > 80:
        raise ValueError("请选择用途，名称不能为空或超过 80 字。")
    if any(pool["name"] == name for pool in state.get("resource_pools", [])):
        raise ValueError("资源池名称重复。")
    if len(state.get("resource_pools", [])) >= 100:
        raise ValueError("最多允许 100 个资源池。")
    pool = {"id": uuid.uuid4().hex, "name": name, "kind": kind, "items": [], "next_id": ""}
    state.setdefault("resource_pools", []).append(pool)
    return pool


def accepted_suffixes(kind):
    if kind == "visual":
        from .visual_media import VISUAL_SUFFIXES
        return VISUAL_SUFFIXES
    if kind in {"text", "subtitle"}:
        return {".txt", ".md"} if kind == "text" else {".srt"}
    if kind == "image":
        from .layers import IMAGE_SUFFIXES
        return IMAGE_SUFFIXES | {".gif"}
    from .matching import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
    return VIDEO_EXTENSIONS if kind == "video" else AUDIO_EXTENSIONS


def add_pool_paths(pool, paths):
    from .store import add_paths, file_identity
    known = {file_identity(item["path"]) for item in pool["items"]}
    additions = []
    for value in paths:
        path = Path(value).resolve()
        identity = file_identity(path)
        if path.is_file() and path.suffix.casefold() in accepted_suffixes(pool["kind"]) and identity not in known:
            additions.append(str(path))
            known.add(identity)
    if len(pool["items"]) + len(additions) > MAX_POOL_ITEMS:
        raise ValueError(f"每个资源池最多 {MAX_POOL_ITEMS} 项，未添加本批文件。")
    if pool["kind"] == "music":
        return add_paths(pool["items"], additions, music=True)
    for path in additions:
        pool["items"].append({"id": uuid.uuid4().hex, "path": path, "name": Path(path).name})
    return len(additions)


def pool_choices(state, kind, default_label=None):
    return ([('', default_label)] if default_label else []) + [
        (pool["id"], pool["name"]) for pool in state.get("resource_pools", []) if pool["kind"] == kind]


def music_source(state):
    identifier = state["settings"].get("music_pool_id", "")
    if identifier:
        pool = get_pool(state, identifier, "music")
        return pool["items"], pool["next_id"], pool
    return state["music"], state["next_music_id"], None


def assign_voices(pairs, voices, mode="task"):
    """An explicit queue binding, not a music playhead or a task import."""
    result = copy.deepcopy(pairs)
    if mode not in {"task", "none", "rotate", "single"}:
        raise ValueError("人声搭配方式无效。")
    if mode in {"rotate", "single"} and not voices:
        raise ValueError("所选语音池为空，请先添加人声。")
    for index, pair in enumerate(result):
        if mode == "none":
            pair["voice_path"] = ""
        elif mode != "task":
            pair["voice_path"] = voices[0 if mode == "single" else index % len(voices)]["path"]
        # Explicitly distinguish no voice from legacy auto-discovery of task_audio.
        if mode != "task":
            pair["voice_override"] = True
    return result
