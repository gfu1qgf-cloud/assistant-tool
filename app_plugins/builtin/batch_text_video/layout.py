"""The preview and exported PNG use exactly the same bounded text layout."""
from qt_compat import QtCore, QtGui
from pathlib import Path
import os


def ensure_fonts():
    """Windows' offscreen platform may have no system font database."""
    if not QtGui.QFontDatabase.families():
        directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("segoeui.ttf", "msyh.ttc"):
            path = directory / name
            if path.is_file():
                QtGui.QFontDatabase.addApplicationFont(str(path))


class TextDoesNotFit(ValueError):
    pass


def render_preview_overlay(title, body, width, height, settings, image_cache=None):
    """Keep valid components visible; never relax the strict export validation."""
    try:
        return render_overlay(title, body, width, height, settings, image_cache)
    except TextDoesNotFit as error:
        from .layers import normalize_layers, compose_layers
        from .components import resolve_sources
        from .text_components import render_text_box
        layers, _ = resolve_sources(normalize_layers(settings.get("layers")), title, body)
        active = [layer for layer in layers if layer["kind"] != "image" and layer["enabled"] and layer["opacity"]]
        if not active or any("font_min" not in layer for layer in active) or sum(len(layer["text"]) for layer in active) > 40000:
            raise
        images, sizes, warnings = {}, {}, []
        for layer in active:
            try:
                images[layer["id"]], size = render_text_box(layer, width, height)
                if size:
                    sizes[layer["id"]] = size
            except TextDoesNotFit as layer_error:
                warnings.append(str(layer_error))
                image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(QtCore.Qt.GlobalColor.transparent)
                painter = QtGui.QPainter(image)
                try:
                    box = QtCore.QRectF(width*(layer["x"]-layer["width"]/2)/100,
                        height*(layer["y"]-layer["height"]/2)/100,
                        width*layer["width"]/100,height*layer["height"]/100).intersected(QtCore.QRectF(0,0,width,height))
                    painter.fillRect(box,QtGui.QColor(110,0,0,190))
                    painter.setPen(QtGui.QPen(QtGui.QColor("#ff6060"),max(2,width/270)))
                    painter.drawRect(box)
                    font = QtGui.QFont()
                    font.setPixelSize(max(10,round(26*width/1080)))
                    painter.setFont(font)
                    painter.setPen(QtGui.QColor("#ffffff"))
                    painter.drawText(box.adjusted(8,4,-8,-4),QtCore.Qt.AlignmentFlag.AlignCenter |
                        QtCore.Qt.TextFlag.TextWordWrap,layer["name"]+"放不下\n请扩大文本框或减小字号")
                finally:
                    painter.end()
                images[layer["id"]] = image
        warnings = warnings or [str(error)]
        return compose_layers(images,layers,width,height,settings["darkness"],image_cache,
            settings.get("background_tint_color","#000000"),settings.get("background_tint_strength",0)),{
            "title_size":sizes.get("title",0),"body_size":sizes.get("body",0),
            "component_sizes":sizes,"warnings":warnings,"export_blocked":True}


def _text_layout(text, family, size, width, alignment, spacing,font_options=None,scale=1):
    font = QtGui.QFont(family)
    font.setPixelSize(max(1, round(size)))
    options = font_options or {}
    font.setWeight(QtGui.QFont.Weight(int(options.get("font_weight",400))))
    font.setItalic(options.get("italic",False))
    font.setUnderline(options.get("underline",False))
    font.setStrikeOut(options.get("strikeout",False))
    font.setCapitalization({"normal":QtGui.QFont.Capitalization.MixedCase,
        "uppercase":QtGui.QFont.Capitalization.AllUppercase,"lowercase":QtGui.QFont.Capitalization.AllLowercase,
        "smallcaps":QtGui.QFont.Capitalization.SmallCaps}[options.get("capitalization","normal")])
    if options.get("letter_spacing"):
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing,options["letter_spacing"]*scale)
    if options.get("word_spacing"):
        font.setWordSpacing(options["word_spacing"]*scale)
    layouts, height = [], 0.0
    flags = {"left": QtCore.Qt.AlignmentFlag.AlignLeft,
             "center": QtCore.Qt.AlignmentFlag.AlignHCenter,
             "right": QtCore.Qt.AlignmentFlag.AlignRight}
    paragraphs = str(text).replace("\r\n", "\n").split("\n")
    for index,paragraph in enumerate(paragraphs):
        if index:
            height += options.get("paragraph_spacing",0)*scale
        if not paragraph:
            height += QtGui.QFontMetricsF(font).height() * spacing * 0.65
            continue
        layout = QtGui.QTextLayout(paragraph, font)
        option = QtGui.QTextOption()
        option.setWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        option.setAlignment(flags[alignment])
        layout.setTextOption(option)
        layout.beginLayout()
        local_height = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QtCore.QPointF(0, local_height))
            local_height += line.height() * spacing
        layout.endLayout()
        layouts.append((layout, height))
        height += local_height
    return layouts, height


def render_overlay(title, body, width, height, settings, image_cache=None):
    from .layers import normalize_layers, compose_layers
    from .components import resolve_sources
    layers, _ = resolve_sources(normalize_layers(settings.get("layers")), title, body)
    enabled = {layer["kind"] for layer in layers if layer["enabled"] and layer["opacity"] > 0}
    title = next((layer["text"] for layer in layers if layer["kind"] == "title" and "font_min" not in layer), "") if "title" in enabled else ""
    body = next((layer["text"] for layer in layers if layer["kind"] == "body" and "font_min" not in layer), "") if "body" in enabled else ""
    custom = [layer for layer in layers if layer["kind"] != "image" and "font_min" in layer and layer["enabled"] and layer["opacity"]]
    if not title.strip() and not body.strip() and not any(layer["text"].strip() for layer in custom) and "image" not in enabled:
        raise TextDoesNotFit("没有启用的文字或图片图层，请填写文案或添加图片。")
    if len(title) + len(body) + sum(len(layer["text"]) for layer in custom) > 40000:
        raise TextDoesNotFit("文案过长，请拆分后生成。")
    if all("font_min" in layer for layer in layers if layer["kind"] in {"title","body"}):
        # Fully independent components never consult obsolete global text fields.
        from .text_components import render_text_box
        text_images,component_sizes = {},{}
        for layer in custom:
            text_images[layer["id"]],size = render_text_box(layer,width,height)
            if size:
                component_sizes[layer["id"]] = size
        return compose_layers(text_images,layers,width,height,settings["darkness"],image_cache,
            settings.get("background_tint_color","#000000"),settings.get("background_tint_strength",0)),{
            "title_size":component_sizes.get("title",0),"body_size":component_sizes.get("body",0),
            "text_height":0,"available_height":height,"component_sizes":component_sizes}
    for kind in ("title", "body"):
        if not 8 <= settings[kind + "_min"] <= settings[kind + "_max"] <= 200:
            raise ValueError("最小字号不能超过最大字号（范围8～200，按1080宽画面计算）。")
    scale = width / 1080.0
    x = width * settings["margin_x"] / 100
    available_width = width - 2 * x
    top = height * settings["margin_top"] / 100
    available_height = height * (1 - (settings["margin_top"] + settings["margin_bottom"]) / 100)
    if available_width < 100 or available_height < 100:
        raise ValueError("安全边距太大，没有足够的排版空间。")
    gap = settings["gap"] * scale if title.strip() and body.strip() else 0

    def measure(fraction):
        sizes = {kind: round((settings[kind + "_min"] + fraction * (
            settings[kind + "_max"] - settings[kind + "_min"])) * scale)
                 for kind in ("title", "body")}
        groups, heights = {}, {}
        for kind, text in (("title", title), ("body", body)):
            groups[kind], heights[kind] = _text_layout(
                text, settings["font"], sizes[kind], available_width,
                settings[kind + "_align"], settings["line_spacing"]
            ) if text.strip() else ([], 0)
        return groups, heights, sizes, heights["title"] + gap + heights["body"]

    minimum = measure(0)
    if minimum[3] > available_height:
        raise TextDoesNotFit("最小字号仍放不下全部文案：请缩短文案、减小边距或降低最小字号；未裁掉任何文字。")
    chosen = measure(1)
    if chosen[3] > available_height:
        low, high = 0.0, 1.0
        chosen = minimum
        for _ in range(12):
            middle = (low + high) / 2
            candidate = measure(middle)
            if candidate[3] <= available_height:
                low, chosen = middle, candidate
            else:
                high = middle
    groups, heights, sizes, total = chosen
    anchor = {"top": 0, "center": 0.5, "bottom": 1}[settings["vertical"]]
    y = top + (available_height - total) * anchor
    text_images = {}
    outline = max(1, round(2 * scale))
    for kind in ("title", "body"):
        image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(image)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        try:
            for layout, offset in groups[kind]:
                painter.setPen(QtGui.QColor(0, 0, 0, 215))
                for dx, dy in ((-outline, 0), (outline, 0), (0, -outline), (0, outline)):
                    layout.draw(painter, QtCore.QPointF(x + dx, y + offset + dy))
                painter.setPen(QtGui.QColor(settings[kind + "_color"]))
                layout.draw(painter, QtCore.QPointF(x, y + offset))
        finally:
            painter.end()
        text_images[kind] = image
        y += heights[kind] + (gap if kind == "title" else 0)
    component_sizes = {}
    from .text_components import render_text_box
    for layer in custom:
        text_images[layer["id"]], component_sizes[layer["id"]] = render_text_box(layer,width,height)
    image = compose_layers(text_images, layers, width, height, settings["darkness"], image_cache,
        settings.get("background_tint_color","#000000"),settings.get("background_tint_strength",0))
    return image, {"title_size": sizes["title"] / scale, "body_size": sizes["body"] / scale,
                   "text_height": total, "available_height": available_height,
                   "component_sizes": component_sizes}
