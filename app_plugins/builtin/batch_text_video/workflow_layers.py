"""Canvas nodes are adapters to the original batch components and compositor."""
import copy
from dataclasses import asdict
from fractions import Fraction
import json
from pathlib import Path

from app_plugins.api import NodeDefinition, NodeParameter, NodePort, VideoFilterFragment
from model.node_workflow.contracts import FilterOperation, ResourceRef
from .components import new_text_component, resolve_sources
from .layers import normalize_layers, DEFAULT_LAYERS, ImageCache


def new_layer(kind):
    if kind == "text": return new_text_component()
    return {"id": "node_visual", "kind": "image", "name": "视觉元素", "enabled": True, "opacity": 100,
            "source": "file", "source_file": "", "path": "", "x": 50, "y": 50, "width": 30, "height": 30}


def normalize_layer(value, kind):
    if not isinstance(value, dict) or value.get("kind") != kind:
        raise ValueError("节点组件类型无效。")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 256000:
        raise ValueError("节点组件设置过大。")
    if value.get("source", "fixed") not in {"fixed", "file"}:
        raise ValueError("节点来源只接受手填／固定文件；资源池请连接可选输入端。")
    return next(item for item in normalize_layers([value]) if item["kind"] == kind)


def prepare_layer(_task, params, _services):
    kind = params["layer"]["kind"]
    layer = normalize_layer(params["layer"], kind)
    result = {}
    path = layer.get("source_file") or layer.get("path", "")
    if path and Path(path).is_file():
        result["file"] = asdict(ResourceRef.capture(path, "layer_file"))
    if kind == "image":
        from .matting_assets import snapshot_layers
        frozen = snapshot_layers([dict(layer, path=path)])[0]
        result['matting'] = frozen.get('_matting_snapshot', {})
        for record in result['matting'].values():
            if record and not record.get('error'):
                from .matting_assets import record_files
                for path in record_files(record):
                    result.setdefault('matting_files', []).append(asdict(ResourceRef.capture(path, 'saved_matte')))
        from .chroma_key import active_mask, MASK_SUFFIXES
        mask = active_mask(layer)
        if mask and Path(mask).is_file() and Path(mask).suffix.casefold() in MASK_SUFFIXES:
            result["key_mask"] = asdict(ResourceRef.capture(mask, "key_mask"))
    if kind == "text":
        from .workflow_caption import prepare_caption
        # Same main-thread font registration guard; no new Qt app in workers.
        from .captions import new_caption
        caption = new_caption()
        caption["caption_timing"] = "task"
        prepare_caption(None, {"caption": caption}, None)
    return result


def selected_index(inputs, params, context):
    value = inputs.get("index")
    if value is not None:
        if type(value) not in (int, float) or not float(value).is_integer() or value < 1:
            raise ValueError("资源池序号必须是从1开始的整数；可接全局数据的当前循环次数。")
        return int(value)-1
    if params.get("pool_mode", "rotate") == "first": return params.get("offset", 0)
    globals_value = getattr(context, "workflow_globals", None)
    return (globals_value.iteration-1 if globals_value else 0) + params.get("offset", 0)


def build_layer(inputs, params, context, *, media="text"):
    kind = "text" if media == "text" else "image"
    layer = normalize_layer(params["layer"], kind)
    refs = []
    if not layer["enabled"] or not layer["opacity"]:
        return {"operations": (FilterOperation("图层已禁用", None, "null"),)}
    pool = inputs.get("pool")
    if pool is not None:
        from .workflow_pools import pool_layer
        layer, ref = pool_layer(layer, pool, selected_index(inputs, params, context))
        if ref: refs.append(ref)
    elif inputs.get("content") is not None:
        content = inputs["content"]
        if not isinstance(content, str): raise TypeError("内容输入必须是文字或文件路径。")
        layer.update(source="fixed", source_file="", **({"text": content} if kind == "text" else {"path": content}))
        if kind == 'image' and layer.get('use_saved_matte', True) and hasattr(content, 'binding'):
            layer['_matting_binding'] = copy.deepcopy(content.binding)
        if kind == "image": refs.append(ResourceRef.capture(content, "layer_file"))
    else:
        path = layer.get("source_file") or layer.get("path", "")
        if kind == "image" or layer["source"] == "file":
            stored = context.node_prepared.get("file")
            ref = ResourceRef(**stored) if stored and str(Path(path).resolve()) == stored["path"] else ResourceRef.capture(path, "layer_file")
            ref.verify()
            refs.append(ref)
        if kind == 'image' and 'matting' in context.node_prepared:
            layer['_matting_snapshot'] = context.node_prepared['matting']
        layer = resolve_sources([layer])[0][0]
    if kind == 'image':
        from .matting_assets import apply_saved
        layer = apply_saved(layer)
        if layer.get('_matting_binding'):
            for label in ('source', 'result') if 'result' in layer['_matting_binding'] else ('source',):
                ref = ResourceRef.capture(layer['_matting_binding'][label]['path'], 'saved_matte_'+label)
                ref.verify(); refs.append(ref)
        layer.pop('_matting_snapshot', None)
    layer.update(source="fixed", source_file="", sequence_items=[], sequence_cursor=0)
    layer = normalize_layer(layer, kind)
    if kind == "text" and not layer["text"].strip():
        if params.get('skip_empty', False):
            return {"operations": (FilterOperation('空白文字 · 跳过', None, 'null'),)}
        raise ValueError("添加文字没有内容，请连接文字／文案池，或在来源里填写。")
    if kind == "text":
        from .text_colors import resolve_colors
        task, globals_value = getattr(context, "task", None), getattr(context, "workflow_globals", None)
        scope = f"node:{getattr(task, 'directory', '')}:{getattr(task, 'key', '')}:{getattr(globals_value, 'iteration', 1)}"
        layer = resolve_colors([layer], scope)[0]
        from .workflow_colors import apply_color_inputs
        layer = apply_color_inputs(layer, inputs)
    if kind == "image":
        from .visual_media import is_animated, VISUAL_SUFFIXES
        from .chroma_key import validate_mask
        if inputs.get("key_mask") is not None:
            if not isinstance(inputs["key_mask"], str): raise TypeError("辅助遮罩输入必须是文件路径。")
            layer["key_mask_path"] = inputs["key_mask"]
        mask = validate_mask(layer)
        if mask:
            stored = context.node_prepared.get("key_mask")
            ref = ResourceRef(**stored) if stored and str(Path(mask).resolve()) == stored["path"] else ResourceRef.capture(mask, "key_mask")
            ref.verify(); refs.append(ref)
        path = Path(layer["path"])
        if path.suffix.casefold() not in VISUAL_SUFFIXES: raise ValueError("视觉素材格式不支持。")
        if media == "image" and is_animated(path): raise ValueError("此节点用于静态图片；GIF／MP4请使用“添加GIF／视频”节点。")
        if media == "animated" and not is_animated(path): raise ValueError("此节点用于GIF／视频；静态图片请使用“添加图片”节点。")
    return {"operations": (FilterOperation(layer["name"], None, "batch_text_video." + media + "_layer",
        (("layer", json.dumps(layer, ensure_ascii=False, allow_nan=False)),),
        tuple(("source" + str(index), ref) for index, ref in enumerate(refs))),)}


def compile_layer(operation, directory, context, video_info, *, timing=None):
    from .captions import static_planes
    from .store import DEFAULTS
    from .visual_media import add_plane
    from uuid import uuid4
    options = dict(operation.options)
    if set(options) != {"layer"} or not isinstance(options["layer"], str): raise ValueError("图层操作格式无效。")
    value = json.loads(options["layer"])
    layer = normalize_layer(value, value["kind"])
    if layer.get('_matting_binding', {}).get('mode') == 'dynamic':
        from .matting_live import Inputs
        context.deferred_input_factory = Inputs
    for _, ref in operation.assets: ref.verify()
    context.check_canceled()
    stream = video_info["video"]
    width, height = stream.get("display_width", stream["width"]), stream.get("display_height", stream["height"])
    fps = Fraction(stream.get("avg_frame_rate", "25/1"))
    if fps <= 0: fps = Fraction(25)
    duration = video_info["duration"]
    local_duration = timing.source_duration(duration) if timing else duration
    settings = {**copy.deepcopy(DEFAULTS), "darkness": 0, "background_tint_strength": 0,
                "layers": [layer] + [dict(item, enabled=False) for item in DEFAULT_LAYERS]}
    planes = static_planes("", "", width, height, settings, ImageCache())
    args, filters, background, input_index = [], [], "input", 0
    for index, (kind, plane) in enumerate(planes):
        label = "output" if index == len(planes)-1 else "plane" + str(index)
        if kind == "visual":
            input_index += add_plane(args, filters, plane, input_index, background, label, width, height, duration, fps,
                                     timing=timing, ffmpeg=getattr(context, 'ffmpeg', ''))-1
        elif kind == "scroll":
            from .scrolling_text import prepare_scroll_plane, add_scroll_plane
            assets = prepare_scroll_plane(plane, width, height, local_duration, directory)
            if timing:
                assets.update(canvas_width=width, canvas_height=height)
            input_index += add_scroll_plane(args, filters, assets, input_index, background, label, duration, fps, timing=timing)-1
        elif kind == "image":
            path = Path(directory) / ("layer_" + uuid4().hex + ".png")
            if not plane.save(str(path), "PNG"): raise RuntimeError("文字／图片图层保存失败。")
            args += ["-loop", "1", "-i", str(path)]
            if timing:
                from .overlay_timing import add_timed_plane
                source = label + "_static"
                filters.append(f"[{input_index}:v]fps={float(fps):.9f},format=rgba,"
                               f"trim=duration={local_duration:.9f},setpts=PTS-STARTPTS[{source}]")
                timed = add_timed_plane(filters, source, label + "_timed", width, height, duration, fps, timing,
                    center=(width*layer["x"]/100, height*layer["y"]/100))
                filters.append(f"[{background}][{timed}]overlay=0:0:format=auto:eof_action=pass[{label}]")
            else:
                filters.append(f"[{background}][{input_index}:v]overlay=0:0:shortest=1:format=auto[{label}]")
        else:
            raise ValueError("静态／视觉节点不能生成字幕层。")
        input_index += 1
        background = label
    groups, current = [], []
    for item in args:
        current.append(item)
        if len(current) >= 2 and current[-2] == "-i":
            groups.append(tuple(current)); current = []
    if current: raise ValueError("图层输入参数格式异常。")
    context.check_canceled()
    for _, ref in operation.assets: ref.verify()
    return (VideoFilterFragment(operation.video, tuple(groups), tuple(filters)),)


def layer_node(media):
    from .workflow_layer_ui import layer_inspector
    from .workflow_colors import COLOR_PORTS
    title = {"text": "添加文字", "image": "添加图片", "animated": "添加GIF／视频", "visual": "添加视觉素材 · 图片／GIF／视频"}[media]
    kind = "text" if media == "text" else "image"
    return NodeDefinition("batch_text_video." + media + "_layer", title,
        inputs=(NodePort("content", "文字" if media == "text" else "素材路径", "text", required=False),
                NodePort("pool", "资源池 · 优先使用", "resource_pool", required=False),
                NodePort("index", "池序号 · 从1开始", "number", required=False)) + (COLOR_PORTS if media == 'text' else
                (NodePort("key_mask", "增强辅助遮罩", "text", required=False),)),
        outputs=(NodePort("operations", "图层操作", "operations"),),
        parameters=(NodeParameter("layer", "组件设置", "object", new_layer(kind)),
                    NodeParameter("pool_mode", "资源池选取", "choice", "rotate",
                        choices=(("rotate", "随当前循环次数轮换"), ("first", "固定使用第一项"))),
                    NodeParameter("offset", "轮换起点偏移", "integer", 0, 0, 100000))
                    + ((NodeParameter('skip_empty', '空白文字时跳过', 'boolean', False),) if kind == 'text' else ()),
        build=lambda i, p, c: build_layer(i, p, c, media=media), prepare=prepare_layer,
        inspector_factory=layer_inspector, compile_operation=compile_layer,
        validate_parameters=lambda p: normalize_layer(p["layer"], kind),
        description="直接复用文案视频的组件设置、自动字号、绘制／抠像与叠加。只输出操作，视频接解算器。"
                    + ("任务正文／标题可从“文案视频输入”直接连接文字输入，不需要来源节点。" if kind == 'text' else '')
                    + "可选池输入优先，其次内容输入，再使用节点固定来源；池按当前循环次数选取，不写回池进度。"
                    "同一解算器按操作连接顺序从底到顶叠加，预览与导出使用同一套合成代码。")
