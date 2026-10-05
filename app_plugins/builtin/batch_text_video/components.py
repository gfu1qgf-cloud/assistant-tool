"""Reusable component content sources, independent of widgets and FFmpeg.

Sequence selection is read-only. Progress is committed only with a completed
video, so previews, cancellation and failed exports cannot consume entries.
"""
import copy
import hashlib
import json
import uuid

CONTENT_FIELDS = {"title": "text", "body": "text", "text": "text", "image": "path"}
MAX_SEQUENCE_ITEMS = 1000
MAX_TEXT = 40000


def normalize_source(component):
    kind = component["kind"]
    field = CONTENT_FIELDS[kind]
    default = "job" if kind in {"title", "body"} else "fixed"
    component.setdefault("source", default)
    if component["source"] not in ({"job", "fixed", "sequence", "pool"} if default == "job" else {"fixed", "sequence", "pool"}):
        raise ValueError("组件内容来源不支持。")
    component.setdefault("pool_field", "title" if kind == "title" else "body" if kind == "body" else "full")
    if component["pool_field"] not in {"title", "body", "full"}:
        raise ValueError("轮换文本来源无效。")
    if field == "text":
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
    payload = (component["kind"], component["source"], component.get("sequence_items", []))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def resolve_sources(layers, title="", body=""):
    resolved, plan = copy.deepcopy(layers), []
    for layer in resolved:
        normalize_source(layer)
        if not layer.get("enabled", True) or not layer.get("opacity", 100):
            continue
        field = CONTENT_FIELDS[layer["kind"]]
        if layer["source"] == "job":
            layer[field] = str(title if layer["kind"] == "title" else body)
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
    return resolved, plan


def commit_sequences(layers, plan):
    result = copy.deepcopy(layers)
    by_id = {layer["id"]: layer for layer in result}
    seen = set()
    for step in plan:
        layer = by_id.get(step["id"])
        if not layer or step["id"] in seen:
            raise ValueError("序列组件已改变，未推进序列位置。")
        seen.add(step["id"])
        normalize_source(layer)
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
            "line_spacing": 1.15, "outline_color": "#8b0000", "sequence_items": [], "sequence_cursor": 0}
