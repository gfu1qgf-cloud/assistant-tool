"""Resource libraries and explicit batch pairing. Never deletes source files."""
from model.AppDataStorage import data_file
import copy
from pathlib import Path
import uuid

from .store import file_identity

RESOURCE_KEYS = ("settings", "jobs", "music", "next_music_id", "backgrounds", "copy_pool", "image_pool", "resource_pools")


def migrate_pools(state):
    from .custom_pools import normalize_pools
    normalize_pools(state)
    from .task_naming import refresh_task_names
    refresh_task_names(state)
    # Run once: removing a library item must not resurrect it from an old job.
    if "copy_pool" not in state:
        state["copy_pool"] = [copy.deepcopy(job) for job in state["jobs"]
                              if job.get("title") or job.get("body") or job.get("task_dir") or job.get("voice_path")]
        state["backgrounds"] = list(dict.fromkeys(state.get("backgrounds", []) +
                                                  [job["path"] for job in state["jobs"] if job.get("path")]))
    if "image_pool" not in state:
        items = []
        for layer in state["settings"].get("layers", []):
            if layer["kind"] == "image":
                items.extend(layer.get("sequence_items", []) or [layer])
        state["image_pool"] = []
        known = set()
        for item in items:
            if item.get("path") and file_identity(item["path"]) not in known:
                state["image_pool"].append(copy.deepcopy(item))
                known.add(file_identity(item["path"]))


def add_videos(state, paths):
    from .engine import VIDEO_SUFFIXES
    from .layers import IMAGE_SUFFIXES
    known = {file_identity(path) for path in state["backgrounds"]}
    added = 0
    for value in paths:
        path = Path(value).resolve()
        key = file_identity(path)
        if path.is_file() and path.suffix.casefold() in VIDEO_SUFFIXES | IMAGE_SUFFIXES and key not in known:
            state["backgrounds"].append(str(path))
            known.add(key)
            added += 1
    return added


def merge_copies(state, entries, sheet=False):
    """Import to the copy library, not to the export queue."""
    from .matching import merge_entries
    from .task_texts import merge_sheet_entries
    proxy = {"jobs": state["copy_pool"], "backgrounds": []}
    if sheet:
        added, updated = merge_sheet_entries(proxy, entries)
    else:
        added, updated = merge_entries(proxy, entries), 0
    add_videos(state, [entry["path"] for entry in entries if entry.get("path")])
    return added, updated


def new_copy():
    return {"id": uuid.uuid4().hex, "path": "", "title": "", "body": "", "name": "新文案",
            "label": "新文案", "status": "需配置", "output": "", "error": "", "voice_path": ""}


def pairing_plan(copies, videos, mode="copies"):
    if not videos:
        raise ValueError("背景池为空，请先添加背景视频或图片。")
    if not copies:
        if mode != "videos":
            raise ValueError("文案池为空：请添加文案，或使用“每个背景配一条文案”搭配固定文本框。")
        return [{"copy": None, "path": path} for path in videos]
    if mode == "one_to_one" and len(copies) != len(videos):
        raise ValueError(f"一对一需要数量相同：文案{len(copies)}条，背景{len(videos)}个。")
    if mode not in {"copies", "videos", "one_to_one", "single"}:
        raise ValueError("搭配方式无效。")
    length = len(videos) if mode == "videos" else len(copies)
    return [{"copy": copies[index % len(copies)],
             "path": videos[0] if mode == "single" else videos[index % len(videos)]}
            for index in range(length)]


def apply_pairs(state, pairs):
    # Validate the whole plan before changing the queue.
    keys = [(pair["copy"]["id"] if pair["copy"] else "", file_identity(pair["path"])) for pair in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("搭配中有重复的文案＋背景组合，请先调整；队列未变。")
    for pair in pairs:
        if not Path(pair["path"]).is_file():
            raise ValueError("背景素材不存在：" + Path(pair["path"]).name)
        if pair.get("voice_path") and not Path(pair["voice_path"]).is_file():
            raise ValueError("人声文件不存在：" + Path(pair["voice_path"]).name)
    added = updated = 0
    for pair, key in zip(pairs, keys):
        source = pair["copy"] or {"title": "", "body": "", "name": Path(pair["path"]).stem + "_文字版"}
        existing = next((job for job in state["jobs"] if tuple(job.get("pair_key", [])) == key), None)
        if not existing and pair["copy"]:
            existing = next((job for job in state["jobs"] if job.get("id") == source["id"]
                             and file_identity(job.get("path", "")) == key[1]), None)
        fields = {field: copy.deepcopy(source.get(field, "")) for field in
                  ("title", "body", "voice_path", "task_subtitle_path", "task_dir", "task_id", "task_type", "label")}
        fields.update(path=pair["path"], copy_id=key[0], pair_key=list(key),
                      requires_title=bool(source.get("requires_title")),
                      task_voice_optional=bool(source.get("task_voice_optional")), match_error=source.get("match_error", ""))
        for field in ("task_date", "task_year", "task_filename_auto"):
            if field in source: fields[field] = copy.deepcopy(source[field])
        # Keep the task's original narration when a row is temporarily replaced
        # by a pool/file, so returning to the task source is meaningful.
        fields["task_voice_path"] = source.get("task_voice_path", source.get("voice_path", ""))
        if "voice_override" in pair or "voice_override" in source or existing and "voice_override" in existing:
            fields["voice_override"] = bool(pair.get("voice_override", source.get("voice_override", False)))
        if "voice_path" in pair:
            fields["voice_path"] = pair["voice_path"]
        if existing:
            changed = any(existing.get(field, existing.get("voice_path", "") if field == "task_voice_path" else "") != value
                          for field, value in fields.items())
            existing.update(fields)
            if changed:
                existing.update(status="待生成", error="")
                updated += 1
        else:
            name = source.get("name") or source.get("label") or "文字版"
            used = {job.get("name") for job in state["jobs"]
                    if job.get("task_dir", "") == source.get("task_dir", "")}
            base, number = name, 2
            while name in used:
                name, number = f"{base}_{number}", number + 1
            state["jobs"].append({**fields, "id": uuid.uuid4().hex, "name": name,
                                  "status": "待生成", "output": "", "error": ""})
            added += 1
    return added, updated


def bind_pool_layers(layers, state):
    """Keep the next item by stable ID across edits/reordering of a pool."""
    result = copy.deepcopy(layers)
    for layer in result:
        if layer["kind"] == "watermark":
            layer["corners"] = dict(zip(layer["corners"], bind_pool_layers(list(layer["corners"].values()), state)))
            continue
        if layer.get("source") != "pool":
            continue
        old = layer.get("sequence_items", [])
        cursor = layer.get("sequence_cursor", 0)
        next_id = old[cursor % len(old)]["id"] if old else None
        if layer["kind"] in {"image", "caption"}:
            from .custom_pools import get_pool
            pool_id = layer.get("pool_id", "")
            image_pool = get_pool(state, pool_id) if pool_id else None
            allowed = {"image", "video", "visual"} if layer["kind"] == "image" else {"subtitle"}
            if image_pool and image_pool["kind"] not in allowed:
                raise ValueError("控件与来源池的类型不匹配，请在来源中重新选择。")
            image_items = image_pool["items"] if image_pool else state["image_pool"]
            layer["pool_name"] = image_pool["name"] if image_pool else "默认图片池"
            items = [{key: item[key] for key in ("id", "path", "name", "thumbnail") if key in item}
                     for item in image_items]
            if layer["kind"] == "caption":
                items = [{**item, "caption_file": item["path"]} for item in items]
        else:
            field = layer.get("pool_field", "full")
            items = []
            pool_id = layer.get("pool_id", "")
            from .custom_pools import get_pool
            text_pool = get_pool(state, pool_id, "text") if pool_id else None
            source_items = text_pool["items"] if text_pool else state["copy_pool"]
            layer["pool_name"] = text_pool["name"] if text_pool else "默认文案池"
            for item in source_items:
                if text_pool:
                    path = Path(item["path"])
                    if not path.is_file() or path.stat().st_size > 160000:
                        raise ValueError("文案池文件不存在或过大：" + path.name)
                    content = data_file(path).read_text("utf-8-sig")
                    lines = content.splitlines()
                    item = {**item, "title": lines[0] if lines else "", "body": "\n".join(lines[1:])}
                value = item.get(field, "") if field in {"title", "body"} else "\n".join(
                    text for text in (item.get("title", ""), item.get("body", "")) if text)
                if value.strip():
                    items.append({"id": item["id"], "text": value})
        layer["sequence_items"] = items
        layer["sequence_cursor"] = next((i for i, item in enumerate(items) if item["id"] == next_id),
                                        cursor % len(items) if items else 0)
    return result


def snapshot(state):
    return {key: copy.deepcopy(state[key]) for key in RESOURCE_KEYS}
