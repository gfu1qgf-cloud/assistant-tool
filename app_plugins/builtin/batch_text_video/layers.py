"""Static layer schema and bounded image assets; never alter source pictures."""
from model.AppDataStorage import data_directory, data_file, data_io
from collections import OrderedDict
import copy
import hashlib
import math
import os
from pathlib import Path
import tempfile
import uuid

from qt_compat import QtCore, QtGui
from .components import normalize_source

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".tif", ".tiff"}
MAX_IMAGES = 32
MAX_VISUAL_SIZE_PERCENT = 400
MAX_TEXT_BOXES = 32
TEXT_KINDS = {"title", "body", "text", "caption"}
TEXT_STYLE_DEFAULTS = {
    "writing_mode":"horizontal",
    "font_weight":400,"italic":False,"underline":False,"strikeout":False,
    "capitalization":"normal","letter_spacing":0,"word_spacing":0,"paragraph_spacing":0,
    # Migration fallback: pre-style configurations rendered with a black edge.
    "outline_width":2,"outline_color":"#000000","outline_opacity":84.31,"outline_blur":0,
    "shadow_enabled":False,"shadow_color":"#000000","shadow_opacity":65,"shadow_x":5,"shadow_y":5,"shadow_blur":0,
    "background_enabled":False,"background_color":"#000000","background_opacity":35,
    "background_radius":12,"padding":4,
}
# Stored bottom-to-top; the UI displays the reverse, like ordinary layer panels.
DEFAULT_LAYERS = [
    {"id": "body", "kind": "body", "name": "正文", "enabled": True, "opacity": 100, "outline_color": "#8b0000", "word_spacing":20},
    {"id": "title", "kind": "title", "name": "标题", "enabled": True, "opacity": 100, "outline_color": "#8b0000", "word_spacing":20},
]


def normalize_layers(value=None):
    layers = copy.deepcopy(DEFAULT_LAYERS if value is None else value)
    from model.PrivateDataPaths import remap_path_fields
    remap_path_fields(layers,{'path','thumbnail','source_file','caption_file','key_mask_path'})
    from .watermarks import migrate_watermarks, normalize_watermark
    layers = migrate_watermarks(layers)
    if not isinstance(layers, list) or len(layers) > MAX_IMAGES + MAX_TEXT_BOXES + 15:
        raise ValueError(f"图层格式不正确，最多允许{MAX_IMAGES}个图片图层。")
    ids, kinds = set(), set()
    for layer in layers:
        if not isinstance(layer, dict) or layer.get("kind") not in {"title", "body", "image", "text", "watermark", "caption", "node_effect"}:
            raise ValueError("图层类型不支持，请检查插件记录。")
        kind = layer["kind"]
        if kind in {"title", "body"}:
            if kind in kinds:
                raise ValueError("标题或正文图层重复。")
            kinds.add(kind)
            layer["id"] = kind
        elif not isinstance(layer.get("id"), str) or not layer["id"] or len(layer["id"]) > 128:
            raise ValueError("图片图层缺少编号。")
        if layer["id"] in ids:
            raise ValueError("图层编号重复。")
        ids.add(layer["id"])
        layer.setdefault("name", {"title": "标题", "body": "正文", "image": "图片", "text": "文本框", "watermark": "四角水印", "caption": "动态字幕", "node_effect": "节点效果"}[kind])
        normalize_source(layer)
        if kind == "node_effect":
            from .node_effects import normalize_effect
            normalize_effect(layer)
        if kind == "image":
            from .visual_media import normalize_visual
            normalize_visual(layer)
        layer.setdefault("enabled", True)
        if not isinstance(layer["enabled"], bool):
            raise ValueError("图层显示开关必须为true或false。")
        layer.setdefault("opacity", 100)
        layer["name"] = str(layer["name"])[:120]
        fields = [("opacity", 0, 100)]
        positioned = kind in {"image", "text", "caption"} or kind in TEXT_KINDS and "font_min" in layer
        if positioned:
            from .geometry_anchors import normalize_anchors
            normalize_anchors(layer)
            if kind == "image" and layer["source"] not in {"file", "sequence", "pool"} and not layer.get("path"):
                raise ValueError("图片图层缺少文件路径。")
            for field, default in (("x", 50), ("y", 50), ("width", 30), ("height", 30)):
                layer.setdefault(field, default)
            maximum = MAX_VISUAL_SIZE_PERCENT if kind == 'image' else 100
            fields += [("x", 0, 100), ("y", 0, 100), ("width", 1, maximum), ("height", 1, maximum)]
        if kind in TEXT_KINDS and positioned:
            for key, default in (("font", "Segoe UI"), ("font_min", 26), ("font_max", 42),
                                 ("color", "#ffffff"), ("align", "center"),
                                 ("vertical", "center"), ("line_spacing", 1.15)):
                layer.setdefault(key, default)
            if kind == 'caption' and 'shadow_blur' not in layer:
                from .caption_styles import is_catalog, effects
                if is_catalog(layer):
                    layer['shadow_blur'] = effects(layer)['shadow_blur']
            for key,default in TEXT_STYLE_DEFAULTS.items():
                layer.setdefault(key,default)
            from .text_background import normalize_background
            normalize_background(layer)
            from .text_colors import normalize_color
            normalize_color(layer)
            fields += [("font_min", 8, 200), ("font_max", 8, 200), ("line_spacing", .8, 3)]
            fields += [("letter_spacing",-5,30),("word_spacing",-5,60),("paragraph_spacing",0,120),
                ("outline_width",0,15),("outline_opacity",0,100),("outline_blur",0,150),("shadow_opacity",0,100),
                ("shadow_x",-80,80),("shadow_y",-80,80),("shadow_blur",0,150),("background_opacity",0,100),
                ("background_radius",0,150),("padding",0,100)]
            if (not isinstance(layer["font"], str) or len(layer["font"]) > 200
                    or not QtGui.QColor(str(layer["color"])).isValid()
                    or layer["align"] not in {"left", "center", "right"}
                    or layer["vertical"] not in {"top", "center", "bottom"}):
                raise ValueError("文本框字体、颜色或对齐方式无效。")
            if (type(layer["font_weight"]) is not int or not 1 <= layer["font_weight"] <= 1000
                    or layer["capitalization"] not in {"normal","uppercase","lowercase","smallcaps"}):
                raise ValueError("文本框字重或大小写样式无效。")
            from .vertical_text import WRITING_MODES
            if layer["writing_mode"] not in WRITING_MODES:
                raise ValueError("文本框文字方向无效。")
            if kind == "caption" and layer["writing_mode"] != "horizontal":
                raise ValueError("逐词字幕暂不支持竖排，请使用普通文本框。")
            for key in ("italic","underline","strikeout","shadow_enabled","background_enabled"):
                if not isinstance(layer[key],bool):
                    raise ValueError("文本框样式开关必须为true或false。")
            for key in ("outline_color","shadow_color","background_color"):
                if not QtGui.QColor(str(layer[key])).isValid():
                    raise ValueError("文本框效果颜色无效。")
        for field, low, high in fields:
            try:
                number = float(layer[field])
            except (TypeError, ValueError):
                raise ValueError(f"图层的{field}数值无效。") from None
            if not math.isfinite(number) or not low <= number <= high:
                raise ValueError(f"图层的{field}超出允许范围。")
            layer[field] = number
        if kind in TEXT_KINDS and positioned and layer["font_min"] > layer["font_max"]:
            raise ValueError("文本框最小字号不能超过最大字号。")
        if kind == "watermark":
            normalize_watermark(layer)
        if kind == "caption":
            from .captions import normalize_caption
            normalize_caption(layer)
        elif kind in TEXT_KINDS and positioned:
            from .scrolling_text import normalize_scroll
            normalize_scroll(layer)
    for default in DEFAULT_LAYERS:
        # A converted title/body owns that original slot; do not resurrect a
        # second drawing of the same text. Removing the effect restores defaults.
        converted = any(item['kind'] == 'node_effect' and item['id'] == default['id'] for item in layers)
        if default["kind"] not in kinds and not converted:
            layers.append(copy.deepcopy(default))
    if sum(layer["kind"] == "image" for layer in layers) > MAX_IMAGES:
        raise ValueError(f"最多允许{MAX_IMAGES}个图片图层。")
    if sum(layer["kind"] == "text" for layer in layers) > MAX_TEXT_BOXES:
        raise ValueError(f"最多允许{MAX_TEXT_BOXES}个文本框。")
    if sum(layer["kind"] == "watermark" for layer in layers) > 1:
        raise ValueError("每个配置只需一个四角水印组件。")
    if sum(layer["kind"] == "caption" for layer in layers) > 4:
        raise ValueError("每个配置最多允许4个动态字幕组件。")
    if sum(layer["kind"] == "node_effect" for layer in layers) > 8:
        raise ValueError("每个配置最多允许8个节点效果组件。")
    return layers


def componentize_layers(value, settings):
    """Transfer legacy text settings once; component settings win thereafter.

    Old headless rendering remains supported. Opening the editor upgrades its
    preset text layers to independent boxes, without rewriting source tables.
    """
    layers = normalize_layers(value)
    top = float(settings.get("margin_top", 8))
    bottom = 100-float(settings.get("margin_bottom", 18))
    area = max(2, bottom-top)
    width = max(1,100-2*float(settings.get("margin_x",8)))
    canvas_height = 1080 if settings.get("aspect") == "landscape" else 1920
    gap = min(area/4,float(settings.get("gap",22))/canvas_height*100)
    title_height = (area-gap)*.24
    body_height = area-gap-title_height
    for layer in layers:
        kind = layer["kind"]
        if kind not in {"title","body"} or "font_min" in layer:
            continue
        h = title_height if kind == "title" else body_height
        y = top+h/2 if kind == "title" else top+title_height+gap+h/2
        layer.update(x=50,y=y,width=width,height=h,
                     font=settings.get("font","Segoe UI"),
                     font_min=settings.get(kind+"_min",36 if kind == "title" else 26),
                     font_max=settings.get(kind+"_max",64 if kind == "title" else 42),
                     color=settings.get(kind+"_color","#d3e55a" if kind == "title" else "#ffffff"),
                     align=settings.get(kind+"_align","center" if kind == "title" else "left"),
                     vertical=settings.get("vertical","center"),
                     line_spacing=settings.get("line_spacing",1.15))
    return normalize_layers(layers)


def read_image(path, bounds=None):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError("图层图片不存在：" + path.name)
    from .visual_media import is_animated, frame
    if is_animated(path):
        return frame(path, bounds or QtCore.QSize(4096, 4096))
    if path.stat().st_size > 200 * 1024**2:
        raise ValueError("图层图片文件过大：" + path.name)
    reader = QtGui.QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid() or size.width()*size.height() > 40_000_000:
        raise ValueError("图片损坏、不支持或像素过大（上限4000万）：" + path.name)
    if bounds and (size.width() > bounds.width() or size.height() > bounds.height()):
        size.scale(bounds, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(size)
    image = reader.read()
    if image.isNull():
        raise ValueError("无法读取图层图片：" + path.name + "；" + reader.errorString())
    image.setDevicePixelRatio(1)
    return image


class ImageCache:
    """One preview cache, bounded to 64MiB and invalidated by file changes."""
    def __init__(self, limit=64*1024**2):
        self.limit, self.items = limit, OrderedDict()

    def get(self, path, bounds):
        path = Path(path)
        info = path.stat()
        key = (str(path.resolve()), info.st_mtime_ns, info.st_size, bounds.width(), bounds.height())
        if key not in self.items:
            self.items[key] = read_image(path, bounds)
        self.items.move_to_end(key)
        image = self.items[key]
        total = sum(value.sizeInBytes() for value in self.items.values())
        while total > self.limit and self.items:
            _, old = self.items.popitem(last=False)
            total -= old.sizeInBytes()
        return image


def image_rect(layer, image, width, height):
    size = QtCore.QSizeF(image.size())
    size.scale(QtCore.QSizeF(width*layer["width"]/100, height*layer["height"]/100),
               QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    return QtCore.QRectF(width*layer["x"]/100-size.width()/2,
                        height*layer["y"]/100-size.height()/2, size.width(), size.height())


def compose_layers(text_images, layers, width, height, darkness, cache=None,tint_color="#000000",tint_strength=0):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    # Darkness belongs to the video background, not the overlaid pictures or text.
    image.fill(QtGui.QColor(0, 0, 0, round(darkness*2.55)))
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
    try:
        if tint_strength:
            color = QtGui.QColor(tint_color)
            strength = float(tint_strength)
            if not color.isValid() or not math.isfinite(strength) or not 0 <= strength <= 75:
                raise ValueError("背景叠色颜色或强度无效。")
            color.setAlpha(round(strength*2.55))
            painter.fillRect(image.rect(),color)
        for layer in layers:
            if not layer["enabled"] or layer["opacity"] == 0:
                continue
            if layer["kind"] == "node_effect":
                continue  # Computed by the shared FFmpeg operation compiler.
            painter.setOpacity(layer["opacity"]/100)
            if layer["kind"] == "image":
                bounds = QtCore.QSize(max(1, math.ceil(width*layer["width"]/100)),
                                     max(1, math.ceil(height*layer["height"]/100)))
                # Native composition keys before resizing. Do the same for the
                # poster, otherwise downsampling can erase a small green prop
                # or open its contour before region protection is calculated.
                decode_bounds = QtCore.QSize(4096, 4096) if layer.get("key_mode", "none") != "none" else bounds
                source = cache.get(layer["path"], decode_bounds) if cache else read_image(layer["path"], decode_bounds)
                from .visual_media import keyed_image
                source = keyed_image(source, layer)
                if source.width() > bounds.width() or source.height() > bounds.height():
                    source = source.scaled(bounds, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                           QtCore.Qt.TransformationMode.SmoothTransformation)
                painter.drawImage(image_rect(layer, source, width, height), source)
            else:
                painter.drawImage(0, 0, text_images[layer["id"]])
    finally:
        painter.end()
    return image


def import_images(paths, directory, cancel=None):
    """Decode off the GUI thread and save immutable, orientation-correct PNG copies."""
    directory = Path(directory)/"layer-assets"
    data_directory(directory).ensure(parents=True, exist_ok=True)
    layers, errors, created = [], [], []
    for raw in paths:
        if cancel and cancel.is_set():
            break
        path = Path(raw)
        temporary = None
        try:
            if path.suffix.casefold() not in IMAGE_SUFFIXES:
                raise ValueError("不支持的图片格式：" + path.name)
            image = read_image(path, QtCore.QSize(4096, 4096))
            # Orientation transforms can swap dimensions; bound again after decode.
            if max(image.width(), image.height()) > 4096:
                image = image.scaled(4096, 4096, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                     QtCore.Qt.TransformationMode.SmoothTransformation)
            from model.AppDataStorage import application_storage
            storage = application_storage()
            if storage.protected:
                # QImage.save writes through Qt, not the Python handle broker.
                # Reserve worst-case pixel bytes plus PNG/thumbnail overhead.
                storage.check_capacity(directory, image.sizeInBytes() + 1024**2)
            handle, temporary = data_io.mkstemp(prefix="import-", suffix=".png", dir=directory)
            os.close(handle)
            if not image.save(temporary, "PNG"):
                raise OSError("保存图片副本失败：" + path.name)
            digest = hashlib.sha256(data_file(Path(temporary)).read_bytes()).hexdigest()
            asset = directory/("asset-" + digest + ".png")
            if not asset.exists():
                data_io.replace(temporary, asset)
                created.append(asset)
            thumbnail = asset.with_suffix(".thumb.png")
            if not thumbnail.exists():
                small = image.scaled(64, 64, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                     QtCore.Qt.TransformationMode.SmoothTransformation)
                if not small.save(str(thumbnail), "PNG"):
                    raise OSError("保存图片缩略图失败：" + path.name)
                created.append(thumbnail)
            layers.append({"id": uuid.uuid4().hex, "kind": "image", "name": path.stem,
                           "path": str(asset.resolve()), "thumbnail": str(thumbnail.resolve()), "enabled": True, "opacity": 100,
                           "x": 50, "y": 50, "width": 30, "height": 30})
        except Exception as error:
            errors.append(f"{path.name}：{error}")
        finally:
            if temporary and Path(temporary).exists():
                data_file(Path(temporary)).remove()
    cancelled = bool(cancel and cancel.is_set())
    if cancelled:
        for asset in created:
            data_file(asset).remove(missing_ok=True)
        layers = []
    return {"layers": layers, "errors": errors, "cancelled": cancelled}
