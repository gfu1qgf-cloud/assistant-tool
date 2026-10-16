"""Reusable component content sources, independent of widgets and FFmpeg.

Sequence selection is read-only. Progress is committed only with a completed
video, so previews, cancellation and failed exports cannot consume entries.
"""
from model.AppDataStorage import data_file
import copy
import hashlib
import json
import uuid
from pathlib import Path

CONTENT_FIELDS = {"title": "text", "body": "text", "text": "text", "image": "path", "caption": "caption_file"}
MAX_SEQUENCE_ITEMS = 1000
MAX_TEXT = 40000


def walk_components(layers):
    """Canvas objects and their private corner controls, without copying."""
    for layer in layers:
        yield layer
        if layer.get("kind") == "watermark":
            yield from layer.get("corners", {}).values()


def normalize_source(component):
    kind = component["kind"]
    if kind == "node_effect":
        component.setdefault("source", "workflow")
        if component["source"] != "workflow":
            raise ValueError("节点效果的来源必须是工作流。")
        return
    if kind == "caption":
        component.setdefault("source", "voice")
        if component["source"] == "voice":
            return
    if kind == "watermark":
        component.setdefault("source", "fixed")
        if component["source"] != "fixed":
            raise ValueError("四角水印不支持轮换内容来源。")
        return
    field = CONTENT_FIELDS[kind]
    default = "job" if kind in {"title", "body"} else "fixed"
    component.setdefault("source", default)
    if component["source"] not in ({"job", "fixed", "file", "sequence", "pool"} if default == "job" else {"fixed", "file", "sequence", "pool"}):
        raise ValueError("组件内容来源不支持。")
    component.setdefault("pool_field", "title" if kind == "title" else "body" if kind == "body" else "full")
    component.setdefault("source_file", "")
    if not isinstance(component["source_file"], str) or len(component["source_file"]) > 4096:
        raise ValueError("来源文件路径无效。")
    if component["pool_field"] not in {"title", "body", "full"}:
        raise ValueError("轮换文本来源无效。")
    if field == "text":
        component.setdefault("copy_binding", "task")
        if component["copy_binding"] not in {"task", "rotate"}:
            raise ValueError("文案池匹配方式无效。")
        component.setdefault("text", "")
        if not isinstance(component["text"], str) or len(component["text"]) > MAX_TEXT:
            raise ValueError("文本框内容过长或格式错误。")
    items = component.setdefault("sequence_items", [])
    if not isinstance(items, list) or len(items) > MAX_SEQUENCE_ITEMS:
        raise ValueError(f"每个序列最多允许{MAX_SEQUENCE_ITEMS}项。")
    identifiers = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get(field), str):
            raise ValueError("序列内容格式错误。")
        if len(item[field]) > MAX_TEXT or (field == "path" and not item[field]):
            raise ValueError("序列内容过长或图片路径为空。")
        item.setdefault("id", hashlib.sha256(f"{index}|{item[field]}".encode()).hexdigest()[:24])
        if not isinstance(item["id"], str) or not item["id"] or item["id"] in identifiers:
            raise ValueError("序列条目编号重复或无效。")
        identifiers.add(item["id"])
    cursor = component.setdefault("sequence_cursor", 0)
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError("序列位置无效。")
    component["sequence_cursor"] = cursor % len(items) if items else 0


def source_signature(component):
    payload = (component["kind"], component["source"], component.get("pool_id", ""),
               component.get("copy_binding", "task"), component.get("sequence_items", []))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def resolve_sources(layers, title="", body="", *, job=None):
    resolved, plan = copy.deepcopy(layers), []
    for layer in resolved:
        normalize_source(layer)
        if layer["kind"] == "node_effect":
            continue
        if layer["kind"] == "watermark":
            if layer.get("enabled", True) and layer.get("opacity", 100):
                keys = list(layer["corners"])
                children, steps = resolve_sources(list(layer["corners"].values()), title, body, job=job)
                layer["corners"] = dict(zip(keys, children))
                plan.extend({**step, "parent": layer["id"], "corner": key}
                    for key, child in zip(keys, children) for step in steps if step["id"] == child["id"])
            continue
        if layer["kind"] == "caption":
            if layer.get('caption_timing') in {'model','elevenlabs'}:
                # Explicit alignment must not be silently replaced by a stale
                # file/pool source left in a saved component.
                layer.update(source='voice',source_file='',caption_file='')
                continue
            if layer["source"] == "voice": continue
        if not layer.get("enabled", True) or not layer.get("opacity", 100):
            continue
        from .copy_binding import bind_task_copy
        bound = bind_task_copy(layer, job)
        if bound is not None:
            layer.update(bound)
        field = CONTENT_FIELDS[layer["kind"]]
        if layer["source"] == "job":
            layer[field] = str(title if layer["kind"] == "title" else body)
        elif layer["source"] == "file":
            path = Path(layer.get("source_file") or layer.get(field) or "")
            if not str(path) or not path.is_file():
                raise ValueError(layer.get("name", "控件") + "：请在来源中选择有效文件。")
            if field == "text":
                if path.stat().st_size > MAX_TEXT * 4:
                    raise ValueError("文字来源文件过大。")
                layer[field] = data_file(path).read_text("utf-8-sig")
                if len(layer[field]) > MAX_TEXT:
                    raise ValueError("文字来源超过40000字。")
            else:
                layer[field] = str(path)
        elif layer["source"] in {"sequence", "pool"}:
            items = layer["sequence_items"]
            if not items:
                raise ValueError(f"{layer.get('name', '组件')}：序列为空，请先添加内容。")
            cursor = layer["sequence_cursor"]
            item = items[cursor]
            layer[field] = item[field]
            if field == "path":
                layer["thumbnail"] = item.get("thumbnail", "")
            plan.append({"id": layer["id"], "cursor": cursor,
                         "next_cursor": (cursor+1) % len(items), "signature": source_signature(layer)})
        if layer["kind"] == "caption" and layer["caption_timing"] != "uniform":
            layer["caption_timing"] = "file"
    return resolved, plan


def commit_sequences(layers, plan):
    result = copy.deepcopy(layers)
    from .workflow_component_source import commit_steps
    commit_steps(result, [step for step in plan if step.get('node_source')])
    by_id = {layer["id"]: layer for layer in result}
    seen = set()
    for step in plan:
        if step.get('node_source'): continue
        layer = by_id.get(step["id"]) if not step.get("parent") else by_id.get(step["parent"], {}).get("corners", {}).get(step.get("corner"))
        identifier = (step.get("parent"), step.get("corner"), step["id"])
        if not layer or layer["id"] != step["id"] or identifier in seen:
            raise ValueError("序列组件已改变，未推进序列位置。")
        seen.add(identifier)
        normalize_source(layer)
        if step.get("parent") and (not by_id[step["parent"]].get("enabled") or not by_id[step["parent"]].get("opacity")):
            raise ValueError("水印组件已停用，未推进来源位置。")
        items = layer["sequence_items"]
        if (not items or layer["source"] not in {"sequence", "pool"} or not layer["enabled"] or not layer["opacity"]
                or source_signature(layer) != step["signature"]
                or layer["sequence_cursor"] != step["cursor"]
                or step["next_cursor"] != (step["cursor"]+1) % len(items)):
            raise ValueError("序列内容或位置已改变，未消耗任何序列。")
        layer["sequence_cursor"] = step["next_cursor"]
    return result


def new_text_component():
    return {"id": uuid.uuid4().hex, "kind": "text", "name": "文本框", "enabled": True,
            "opacity": 100, "source": "fixed", "text": "", "x": 50, "y": 50,
            "width": 80, "height": 30, "font": "Segoe UI", "font_min": 26,
            "font_max": 42, "color": "#ffffff", "align": "center", "vertical": "center",
            "line_spacing": 1.15, "word_spacing":20, "outline_color": "#8b0000", "sequence_items": [], "sequence_cursor": 0}
