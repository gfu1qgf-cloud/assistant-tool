"""Plugin-owned node interfaces to the shared batch components and renderer."""
import re

from app_plugins.api import NodeDefinition, NodeParameter, NodePort
from model.node_workflow.contracts import FilterOperation
from model.node_workflow.operations import validate_subtitle


def build_caption(inputs, params, context):
    from .workflow_colors import apply_color_inputs
    subtitles = inputs.get("subtitles")
    if subtitles is None:
        raise ValueError("必须提供字幕，导入节点还没有指定 SRT。")
    # Validate the asset only. Video ownership/timing belongs to the settler.
    validate_subtitle(subtitles)
    if params.get("caption") is not None:
        from .workflow_caption import normalize_node_caption
        import json
        layer = normalize_node_caption(params["caption"])
        layer = apply_color_inputs(layer, inputs)
        return {"operations": (FilterOperation("添加字幕 · " + layer["caption_preset"], None,
            "batch_text_video.caption", options=(("layer", json.dumps(layer, ensure_ascii=False, allow_nan=False)),),
            assets=(("filename", subtitles),)),)}
    font = str(params.get("font", "Arial"))
    if not re.fullmatch(r"[\w -]{1,80}", font):
        raise ValueError("字体名称不能包含表达式或分隔符。")
    color = str(params.get("color", "#ffffff"))
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("字体颜色请使用 #RRGGBB。")
    blue, green, red = color[5:7], color[3:5], color[1:3]
    # libass force_style uses internal SSA alignment codes, not ASS file codes.
    alignment = {"bottom": 2, "middle": 10, "top": 6}[params.get("position", "bottom")]
    size = params.get("size", 24)
    outline = params.get("outline", 2)
    if type(size) is not int or not 8 <= size <= 120 or type(outline) is not int or not 0 <= outline <= 8:
        raise ValueError("字幕字号或描边超出范围。")
    style = (f"FontName={font},Fontsize={size},PrimaryColour=&H00{blue}{green}{red},"
             f"OutlineColour=&H00404080,Outline={outline},Shadow=0,Alignment={alignment},MarginV=24")
    from model.node_workflow.contracts import ColorValue
    for key, field in (('color', 'PrimaryColour'), ('outline_color', 'OutlineColour'),
                       ('shadow_color', 'BackColour'), ('background_color', 'BackColour')):
        value = inputs.get(key)
        if value is not None:
            if not isinstance(value, ColorValue):
                raise TypeError('字幕颜色需要 RGB／RGBA 颜色对象。')
            rgb = value.hex_rgb
            style += f',{field}=&H{round((1-value.alpha)*255):02x}{rgb[5:7]}{rgb[3:5]}{rgb[1:3]}'
    return {"operations": (FilterOperation("添加字幕", None, "subtitles",
        options=(("force_style", style),), assets=(("filename", subtitles),)),)}


def caption_node():
    from .workflow_colors import COLOR_PORTS
    from .workflow_caption import compile_caption, prepare_caption, validate_caption_parameters
    from .workflow_caption_ui import caption_inspector
    return NodeDefinition("batch_text_video.caption", "添加字幕 · 全部样式",
        inputs=(NodePort("subtitles", "字幕 SRT", "subtitles"),) + COLOR_PORTS,
        outputs=(NodePort("operations", "字幕操作", "operations"),),
        parameters=(NodeParameter("caption", "文案视频完整字幕设置", "object", None, required=False),
                    NodeParameter("font", "字体", default="Arial"),
                    NodeParameter("size", "字号（ASS 基准）", "integer", 24, 8, 120),
                    NodeParameter("color", "文字颜色", "color", "#ffffff"),
                    NodeParameter("outline", "暗红描边厚度", "integer", 2, 0, 8),
                    NodeParameter("position", "垂直位置", "choice", "bottom",
                        choices=(("bottom", "下方"), ("middle", "中间"), ("top", "上方")))),
        build=build_caption, version=2, prepare=prepare_caption,
        inspector_factory=caption_inspector, compile_operation=compile_caption,
        validate_parameters=validate_caption_parameters,
        description="基础静态字幕＋文案视频完整动态／VideoKit／自制达芬奇样式库，共用样式图库、字体、位置和效果参数。"
                    "只接字幕；视频接解算器，按实际视频尺寸编译同一套ASS动画，预览与导出一致，不生成中间视频。"
                    "直接使用输入字幕的时间戳；可显式选择匀速动画近似逐词，不偷偷对齐或调用AI。")


def canvas_nodes():
    from .workflow_layers import layer_node
    from .workflow_pools import pool_node
    from .workflow_timing import timing_node
    from .workflow_component_source import source_node
    return (caption_node(), layer_node("text"), layer_node("image"), layer_node("animated"),
            layer_node("visual"), pool_node(), timing_node(), source_node())
