"""Whole-text scrolling: bounded Qt assets, one clipped FFmpeg animation plane.

Offsets are percentages of the text box's height, not the whole canvas.
No per-frame Python rendering, intermediate video, or resource-pool mutation.
"""
import math
from pathlib import Path
from uuid import uuid4

from qt_compat import QtCore, QtGui

DEFAULTS = {
    "text_animation": "none", "scroll_start_delay": 0., "scroll_end_early": 0.,
    "scroll_start_offset": 0., "scroll_end_offset": 0., "scroll_edge_fade": 8.,
    "scroll_offset_units": "percent",
    "scroll_start_anchor": "outside", "scroll_end_anchor": "outside",
}
MAX_STRIP_PIXELS = 16_777_216
MAX_STRIP_HEIGHT = 32768


def normalize_scroll(layer):
    for key, value in DEFAULTS.items():
        layer.setdefault(key, value)
    if layer["text_animation"] not in {"none", "up", "down"}:
        raise ValueError("文字动画方向无效。")
    if layer["scroll_offset_units"] not in {"percent", "pixels"}:
        raise ValueError("文字动画位置单位无效。")
    for key in ('scroll_start_anchor', 'scroll_end_anchor'):
        if layer[key] not in {'outside', 'top', 'center', 'bottom'}:
            raise ValueError('文字滚动起点／终点位置无效。')
    for key, low, high in (("scroll_start_delay", 0, 86400), ("scroll_end_early", 0, 86400),
                           ("scroll_start_offset", -10000, 10000), ("scroll_end_offset", -10000, 10000),
                           ("scroll_edge_fade", 0, 50)):
        try:
            value = float(layer[key])
        except (TypeError, ValueError):
            raise ValueError("文字动画参数无效："+key) from None
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError("文字动画参数超出范围："+key)
        layer[key] = value


def is_scrolling(layer):
    return layer.get("kind") in {"text", "title", "body"} and layer.get("text_animation", "none") in {"up", "down"}


def new_scrolling_component():
    from .components import new_text_component
    layer = new_text_component()
    layer.update(DEFAULTS, name="滚动文字框", text_animation="up", scroll_start_anchor="top")
    return layer


def box_geometry(layer, width, height):
    # Keep the original box, even when partially off-canvas. The final overlay
    # clips it to the canvas; off-canvas placement must not change scroll speed.
    return (round(width*(layer["x"]-layer["width"]/2)/100),
            round(height*(layer["y"]-layer["height"]/2)/100),
            max(1, round(width*layer["width"]/100)), max(1, round(height*layer["height"]/100)))


def scroll_endpoints(layer, box_height, text_height):
    first, last = (box_height, -text_height) if layer["text_animation"] == "up" else (-text_height, box_height)
    positions = {'top': 0., 'center': (box_height-text_height)/2, 'bottom': box_height-text_height}
    first = positions.get(layer.get('scroll_start_anchor', 'outside'), first)
    last = positions.get(layer.get('scroll_end_anchor', 'outside'), last)
    return (first + layer['scroll_start_offset']*box_height/100,
            last + layer['scroll_end_offset']*box_height/100)


def scroll_timing(layer, duration, box_height, text_height):
    duration = float(duration)
    start, finish = layer["scroll_start_delay"], duration-layer["scroll_end_early"]
    if not math.isfinite(duration) or duration <= 0 or finish-start < .001:
        raise ValueError(f"{layer['name']}：开始延迟＋提前结束必须小于最终视频时长（{duration:.3f} 秒）。")
    first, last = scroll_endpoints(layer, box_height, text_height)
    return start, finish, first, last


def scroll_position(layer, duration, box_height, text_height, time):
    start, finish, first, last = scroll_timing(layer, duration, box_height, text_height)
    fraction = max(0., min(1., (float(time)-start)/(finish-start)))
    return first+(last-first)*fraction


def _image(width, height):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise ValueError("滚动文字图层分配失败，请降低字号或缩短文案。")
    image.fill(QtCore.Qt.GlobalColor.transparent)
    return image


def render_scroll_assets(layer, width, height):
    from .layout import _text_layout, TextDoesNotFit
    from .text_components import text_padding, draw_text_groups
    from .text_background import draw_background
    x, y, bw, bh = box_geometry(layer, width, height)
    if bw*bh > MAX_STRIP_PIXELS:
        raise TextDoesNotFit(layer["name"]+"：滚动文字框面积过大。")
    scale, size = width/1080, layer["font_max"]
    padding = text_padding(layer, scale)
    if bw-2*padding <= 1 or bh-2*padding <= 1:
        raise TextDoesNotFit(layer["name"]+"：文本框没有足够的可见空间。")
    text = layer["text"]
    if len(text) > 40000:
        raise TextDoesNotFit(layer["name"]+"：文案过长，最多40000字符。")
    if layer.get("writing_mode", "horizontal") != "horizontal":
        from .vertical_text import text_layout
        # Scrolling vertical text keeps newline-separated columns intact and
        # grows downwards. It must not silently fall back to horizontal text.
        groups, total, used_width = text_layout(text, layer["font"], size*scale, bw-2*padding,
            None, layer["align"], layer["line_spacing"], layer, scale)
        if used_width > bw-2*padding+1e-6:
            raise TextDoesNotFit(layer["name"]+"：竖排滚动的列宽放不下，请扩大框宽或降低最大字号。")
    else:
        groups, total = _text_layout(text, layer["font"], size*scale, bw-2*padding,
                                     layer["align"], layer["line_spacing"], layer, scale)
    th = max(1, math.ceil(total+2*padding))
    if th > MAX_STRIP_HEIGHT or bw*th > MAX_STRIP_PIXELS:
        raise TextDoesNotFit(layer["name"]+"：滚动文案图层过大，请降低最大字号或拆成多个文本框。")
    following = layer.get("background_enabled") and layer.get("background_mode","box") == "text"
    bleed = [math.ceil(layer.get("background_extra_"+side,0)*scale) if following else 0
             for side in ("left","right","top","bottom")]
    sw, sh = bw+bleed[0]+bleed[1], th+bleed[2]+bleed[3]
    if sh > MAX_STRIP_HEIGHT or sw*sh > MAX_STRIP_PIXELS:
        raise TextDoesNotFit(layer["name"]+"：底色扩展后的滚动图层过大，请缩小扩展值或拆分文案。")
    strip, background = _image(sw, sh), _image(bw, bh)
    painter = QtGui.QPainter(strip)
    try:
        if following:
            draw_background(painter,layer,groups,padding+bleed[0],padding+bleed[2],scale,QtCore.QRectF(0,0,bw,bh))
        draw_text_groups(painter, groups, layer, padding+bleed[0], padding+bleed[2], scale)
    finally:
        painter.end()
    if text.strip() and layer.get("background_enabled") and not following:
        painter = QtGui.QPainter(background)
        try:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            draw_background(painter,layer,groups,padding,padding,scale,QtCore.QRectF(0,0,bw,bh))
        finally:
            painter.end()
    return {"strip": strip, "background": background, "x": x, "y": y,
            "width": bw, "height": bh, "text_height": th, "font_size": size,
            "strip_left":bleed[0],"strip_top":bleed[2]}


def render_scroll_preview(layer, width, height, *, duration=None, time=None):
    assets = render_scroll_assets(layer, width, height)
    bw, bh, th = assets["width"], assets["height"], assets["text_height"]
    if duration is None:
        # Show the actual start, including its anchor and offset. A hardcoded
        # y=0 previously made every position edit appear to have no effect.
        y = scroll_endpoints(layer, bh, th)[0]
    else:
        y = scroll_position(layer, duration, bh, th, 0 if time is None else time)
    viewport = _image(bw, bh)
    painter = QtGui.QPainter(viewport)
    try:
        painter.drawImage(QtCore.QPointF(-assets["strip_left"], y-assets["strip_top"]), assets["strip"])
        fade = bh*layer["scroll_edge_fade"]/100
        if fade > 0:
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_DestinationIn)
            gradient = QtGui.QLinearGradient(0, 0, 0, max(1, bh-1))
            ratio = min(.5, fade/max(1, bh-1))
            for position, alpha in ((0, 0), (ratio, 255), (1-ratio, 255), (1, 0)):
                gradient.setColorAt(position, QtGui.QColor(255, 255, 255, alpha))
            painter.fillRect(viewport.rect(), QtGui.QBrush(gradient))
    finally:
        painter.end()
    image = _image(width, height)
    painter = QtGui.QPainter(image)
    try:
        point = QtCore.QPointF(assets["x"], assets["y"])
        painter.drawImage(point, assets["background"])
        painter.drawImage(point, viewport)
    finally:
        painter.end()
    return image, assets["font_size"]


def prepare_scroll_plane(layer, width, height, duration, directory):
    assets = render_scroll_assets(layer, width, height)
    timing = scroll_timing(layer, duration, assets["height"], assets["text_height"])
    token = "scroll_"+uuid4().hex
    for key in ("strip", "background"):
        path = Path(directory)/(token+"_"+key+".png")
        if not assets[key].save(str(path), "PNG"):
            raise RuntimeError("滚动文字图层保存失败。")
        assets[key] = str(path)
    assets.update(timing=timing, fade=assets["height"]*layer["scroll_edge_fade"]/100,
                  opacity=layer["opacity"]/100)
    return assets


def add_scroll_plane(args, filters, assets, input_index, background, label, duration, fps, *, timing=None):
    """Shared batch/node backend. Consumes two PNG inputs; returns their count."""
    bw, bh = assets["width"], assets["height"]
    start, finish, first, last = assets["timing"]
    expression = f"{first:.9f}+({last-first:.9f})*clip((t-{start:.9f})/{finish-start:.9f},0,1)"
    if assets.get("strip_top",0): expression += f"-{assets['strip_top']}"
    rate = f"{fps.numerator}/{fps.denominator}" if hasattr(fps, "numerator") else str(fps)
    local_duration = timing.source_duration(duration) if timing else duration
    for key in ("background", "strip"):
        args += ["-loop", "1", "-i", assets[key]]
    prefix = label+"_scroll"
    filters += [
        f"color=c=black@0:s={bw}x{bh}:r={rate}:d={local_duration:.9f},format=rgba[{prefix}_blank]",
        f"[{input_index+1}:v]fps={rate},format=rgba,setpts=PTS-STARTPTS[{prefix}_text]",
        f"[{prefix}_blank][{prefix}_text]overlay=x={-assets.get('strip_left',0)}:y='{expression}':eval=frame:shortest=1:format=auto[{prefix}_clip]",
    ]
    moving = prefix+"_clip"
    if assets["fade"] > 0:
        fade = assets["fade"]
        alpha = f"alpha(X,Y)*min(1,max(0,min(Y,{bh-1}-Y)/{fade:.9f}))"
        filters.append(f"[{moving}]geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha}'[{prefix}_fade]")
        moving = prefix+"_fade"
    filters += [
        f"[{input_index}:v]fps={rate},format=rgba,setpts=PTS-STARTPTS[{prefix}_box]",
        f"[{prefix}_box][{moving}]overlay=0:0:shortest=1:format=auto,format=rgba,colorchannelmixer=aa={assets['opacity']:.9f}[{prefix}_plane]",
    ]
    if timing:
        from .overlay_timing import add_timed_plane
        width, height = assets["canvas_width"], assets["canvas_height"]
        filters += [f"color=c=black@0:s={width}x{height}:r={rate}:d={local_duration:.9f},format=rgba[{prefix}_full_blank]",
            f"[{prefix}_full_blank][{prefix}_plane]overlay=x={assets['x']}:y={assets['y']}:shortest=1:format=auto[{prefix}_full]"]
        timed = add_timed_plane(filters, prefix + "_full", prefix + "_timed", width, height, duration, fps, timing,
                               center=(assets['x']+bw/2, assets['y']+bh/2))
        filters.append(f"[{background}][{timed}]overlay=0:0:format=auto:eof_action=pass[{label}]")
    else:
        filters.append(f"[{background}][{prefix}_plane]overlay=x={assets['x']}:y={assets['y']}:shortest=1:format=auto[{label}]")
    return 2
