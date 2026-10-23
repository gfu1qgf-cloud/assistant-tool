"""A deferred timing adapter for the plugin's existing overlay compilers."""
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

from app_plugins.api import NodeDefinition, NodeParameter, NodePort, VideoFilterFragment
from model.AppDataStorage import data_io, data_file
from model.node_workflow.contracts import FilterOperation, SubtitleTrack
from model.node_workflow.operations import flatten_operations, validate_subtitle
from .overlay_timing import DEFAULTS, ENTER_CHOICES, EXIT_CHOICES, normalize_timing, resolve_window, add_timed_plane


KIND = "batch_text_video.display_window"
LAYER_OPERATIONS = frozenset({"batch_text_video.text_layer", "batch_text_video.image_layer",
                              "batch_text_video.animated_layer", "batch_text_video.visual_layer"})
SUPPORTED = LAYER_OPERATIONS | {"batch_text_video.caption", "subtitles", "null"}


def read_operation(operation):
    options = dict(operation.options)
    if (set(options) != {"source_operation", "source_options", "timing"}
            or any(not isinstance(value, str) for value in options.values())
            or len(options["source_options"].encode("utf-8")) > 256000
            or len(options["timing"].encode("utf-8")) > 4096):
        raise ValueError("显示时段操作格式无效。")
    name = options["source_operation"]
    source_options = json.loads(options["source_options"])
    if name not in SUPPORTED or not isinstance(source_options, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in source_options.items()):
        raise ValueError("显示时段只接文字、图片、GIF／视频或字幕的叠加操作，不接整个视频或压缩操作。")
    timing = normalize_timing(json.loads(options["timing"]))
    return replace(operation, name=name, options=tuple(source_options.items())), timing


def build_timing(inputs, params, _context):
    settings = {key: params.get(key, value) for key, value in DEFAULTS.items()}
    for key in ("start", "end"):
        if inputs.get(key) is not None:
            settings[key] = inputs[key]
    settings = normalize_timing(settings)
    operations = flatten_operations(inputs.get("operations"))
    if len(operations) > 128:
        raise ValueError("一个显示时段最多处理128项叠加操作。")
    result = []
    for operation in operations:
        # A second window replaces the first; no recursive plugin filter chain.
        if operation.name == KIND:
            operation, _previous = read_operation(operation)
        if operation.name not in SUPPORTED:
            raise ValueError(f"“{operation.title}”不是可控制出现时段的叠加内容；请在文字／图片／GIF／字幕之后连接此节点。")
        options = {"source_operation": operation.name,
                   "source_options": json.dumps(dict(operation.options), ensure_ascii=False, allow_nan=False),
                   "timing": json.dumps(settings, ensure_ascii=False, allow_nan=False)}
        if len(options["source_options"].encode("utf-8")) > 256000:
            raise ValueError("叠加设置过大。")
        result.append(replace(operation, title="显示时段 · " + operation.title,
                              name=KIND, options=tuple(options.items())))
    return {"operations": tuple(result)}


def compile_timing(operation, directory, context, video_info):
    source, timing = read_operation(operation)
    if source.name == "null":
        return (source,)
    window = resolve_window(timing, video_info["duration"], automatic_clock=(
        "global" if source.name in {"subtitles", "batch_text_video.caption"} else "restart"),
        report=context.report)
    if source.name in LAYER_OPERATIONS:
        from .workflow_layers import compile_layer
        return compile_layer(source, directory, context, video_info, timing=window)
    if source.name == "batch_text_video.caption":
        from .workflow_caption import compile_caption
        return compile_caption(source, directory, context, video_info, timing=window)
    return compile_basic_caption(source, directory, context, video_info, window)


def caption_plane(operation, name, filter_name, style, context, video_info, timing, center=None):
    stream = video_info["video"]
    width, height = stream.get("display_width", stream["width"]), stream.get("display_height", stream["height"])
    from fractions import Fraction
    fps = Fraction(stream.get("avg_frame_rate", "25/1"))
    if fps <= 0: fps = Fraction(25)
    duration = video_info["duration"]
    local_duration = timing.source_duration(duration)
    caption_filter = f"{filter_name}=filename='{name}':alpha=1" + (f":force_style='{style}'" if style else "")
    filters = [f"color=c=black@0:s={width}x{height}:r={float(fps):.9f}:d={local_duration:.9f},"
               f"format=rgba,{caption_filter}[caption_plane]"]
    timed = add_timed_plane(filters, "caption_plane", "caption_timed", width, height, duration, fps, timing, center=center)
    filters.append(f"[input][{timed}]overlay=0:0:format=auto:eof_action=pass[output]")
    context.check_canceled()
    return (VideoFilterFragment(operation.video, (), tuple(filters)),)


def compile_basic_caption(operation, directory, context, info, timing):
    options, assets = dict(operation.options), dict(operation.assets)
    if set(options) != {"force_style"} or set(assets) != {"filename"}:
        raise ValueError("基础字幕操作格式无效。")
    style = options["force_style"]
    if not isinstance(style, str) or len(style) > 2000 or any(ch in style for ch in "'\\:;[]\r\n\x00"):
        raise ValueError("字幕样式参数无效。")
    asset = assets["filename"]
    validate_subtitle(asset, info["duration"])
    context.check_canceled()
    name = "timed_caption_" + uuid4().hex + ".srt"
    path = Path(directory) / name
    if isinstance(asset, SubtitleTrack):
        data_file(path).write_text_direct(asset.to_srt(), encoding="utf-8")
    else:
        data_io.copyfile(asset.path, path)
    asset.verify()
    return caption_plane(operation, name, "subtitles", style, context, info, timing)


def timing_node():
    from .workflow_timing_ui import timing_inspector
    return NodeDefinition(KIND, "显示时段 · 出入场",
        inputs=(NodePort("operations", "叠加内容", "operations", multiple=True),
                NodePort("start", "开始秒 · 覆盖设置", "number", required=False),
                NodePort("end", "结束秒 · 0到视频末尾", "number", required=False)),
        outputs=(NodePort("operations", "定时叠加操作", "operations"),),
        parameters=(NodeParameter("start", "开始显示（秒）", "number", 0., 0, 86400),
                    NodeParameter("end", "结束显示（秒；0＝视频末尾）", "number", 0., 0, 86400),
                    NodeParameter("enter", "入场方式", "choice", "cut", choices=ENTER_CHOICES+(("fade", "旧版淡入"),)),
                    NodeParameter("enter_duration", "入场耗时（秒）", "number", .4, 0, 60),
                    NodeParameter("fade_in", "淡入时长（秒；0关闭）", "number", 0., 0, 60),
                    NodeParameter("exit", "退场方式", "choice", "cut", choices=EXIT_CHOICES+(("fade", "旧版淡出"),)),
                    NodeParameter("exit_duration", "退场耗时（秒）", "number", .4, 0, 60),
                    NodeParameter("fade_out", "淡出时长（秒；0关闭）", "number", 0., 0, 60),
                    NodeParameter("distance", "滑动／缩放幅度（%）", "number", 12., 0, 80),
                    NodeParameter("clock", "素材播放时钟", "choice", "auto", choices=(
                        ("auto", "自动：素材出现时播放；字幕保留原时间"),
                        ("restart", "出现时从头播放（字幕时间相对开始秒）"),
                        ("global", "跟随整段视频时间（隐藏时也继续）")))),
        build=build_timing, compile_operation=compile_timing, validate_parameters=normalize_timing,
        inspector_factory=timing_inspector,
        category="视频与画面/时间控制", keywords=("时间线", "延迟", "出现", "消失", "淡入", "退场"),
        description="控制叠加内容的出现／消失，不延迟背景。文字／图片／GIF／字幕→本节点→解算器操作端；"
                    "视频直连解算器。开始20、结束0＝20秒后显示至片尾；可接多项操作，重复本节点覆盖前一时段。")
