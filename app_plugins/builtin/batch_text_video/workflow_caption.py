"""Full caption adapter: a plain deferred plan, compiled only at settlement."""
import copy
import json
from pathlib import Path
import re
from uuid import uuid4

from qt_compat import QtCore, QtGui
from model.node_workflow.contracts import FilterOperation, SubtitleTrack
from model.node_workflow.operations import validate_subtitle


def normalize_node_caption(value):
    from .layers import normalize_layers
    if not isinstance(value, dict) or value.get("kind") != "caption":
        raise ValueError("节点的字幕设置必须是字幕组件。")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("字幕组件设置超过64KB。")
    layer = copy.deepcopy(value)
    if layer.get("caption_timing", "task") not in {"task", "uniform"}:
        raise ValueError("字幕节点只使用输入字幕；请选择原时间戳或匀速动画。")
    layer.update(source="voice", source_file="", caption_file="", sequence_items=[],
                 caption_timing=layer.get("caption_timing", "task"))
    return next(item for item in normalize_layers([layer]) if item["kind"] == "caption")


def prepare_caption(_task, params, _services):
    if params.get("caption") is not None:
        normalize_node_caption(params["caption"])
        if QtGui.QGuiApplication.instance() is None:
            raise ValueError("动态字幕需要程序的字体环境，请从主程序执行工作流。")
        if QtCore.QThread.currentThread() != QtGui.QGuiApplication.instance().thread():
            raise ValueError("字幕字体准备必须在主线程完成。")
        # Application-font registration is GUI-thread work, not worker work.
        from .layout import ensure_fonts
        ensure_fonts()
    return {}


def validate_caption_parameters(params):
    if params.get("caption") is not None:
        normalize_node_caption(params["caption"])


def subtitle_words(asset, duration, uniform=False):
    from .captions import read_task_subtitle, validate_words, uniform_word_timings, word_srt_timings
    validate_subtitle(asset, duration)
    if isinstance(asset, SubtitleTrack):
        words = [{"text": word, "start": cue.start, "end": cue.end, "cue": index}
                 for index, cue in enumerate(asset.cues) for word in re.findall(r"\S+", cue.text)]
        if asset.timing == 'word_aligned' and not uniform:
            for word in words: word.pop('cue', None)
        words = validate_words(words, duration)
    else:
        words = read_task_subtitle(asset.path, duration)
    return uniform_word_timings(words, duration) if uniform else word_srt_timings(words)


def compile_caption(operation, directory, context, video_info, *, timing=None):
    from .captions import write_caption_ass
    from .caption_styles import uses_style_renderer
    if QtGui.QGuiApplication.instance() is None:
        raise ValueError("动态字幕缺少字体环境；不会在后台线程创建Qt窗口。")
    options, assets = dict(operation.options), dict(operation.assets)
    if (set(options) != {"layer"} or set(assets) != {"filename"}
            or not isinstance(options["layer"], str) or len(options["layer"].encode("utf-8")) > 65536):
        raise ValueError("动态字幕操作格式无效。")
    layer = normalize_node_caption(json.loads(options["layer"]))
    context.report(f"字幕节点：每组最多{layer['caption_words']}词，每行最多{layer['caption_per_line']}词；使用输入字幕时间。", None)
    context.check_canceled()
    words = subtitle_words(assets["filename"], video_info["duration"], layer["caption_timing"] == "uniform")
    if layer["caption_timing"] == "uniform":
        context.report("字幕：按输入字幕块近似均分词时间（匀速动画，不调用模型）", None)
    stream = video_info["video"]
    width, height = stream.get("display_width", stream["width"]), stream.get("display_height", stream["height"])
    name = "caption_" + uuid4().hex + ".ass"
    path = Path(directory) / name
    if layer["enabled"] and layer["opacity"] > 0:
        write_caption_ass(path, words, layer, width, height, video_info["duration"])
    else:
        return (FilterOperation("字幕已禁用", operation.video, "null"),)
    context.check_canceled()
    assets["filename"].verify()
    from .caption_clip import geometry as clip_geometry
    soft_clip=clip_geometry(layer,width,height) is not None
    if timing or soft_clip:
        from .workflow_timing import caption_plane
        return caption_plane(operation, name, "ass", "", context, video_info, timing,
                             center=(width*layer['x']/100, height*layer['y']/100),
                             clip_layer=layer if soft_clip else None,directory=directory)
    native = (FilterOperation(operation.title, operation.video, "ass", (("filename", name),)),)
    # Exactly the full-chroma blending used by the existing batch renderer.
    return ((FilterOperation("字幕全色彩混合", operation.video, "format", (("pix_fmts", "rgba"),)),) + native
            if uses_style_renderer(layer) else native)
