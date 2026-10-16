"""Bounded same-family colors, stable per composition and never per frame."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import random
from qt_compat import QtCore, QtGui, QtWidgets

DEFAULTS = {"color_mode": "fixed", "color_hue_variation": 8.0,
            "color_saturation_variation": 8.0, "color_value_variation": 10.0, "color_seed": 0}


def normalize_color(layer):
    for key, value in DEFAULTS.items(): layer.setdefault(key, value)
    if layer["color_mode"] not in {"fixed", "family"}:
        raise ValueError("文字颜色模式无效。")
    for key, high in (("color_hue_variation", 30), ("color_saturation_variation", 30), ("color_value_variation", 30)):
        value = layer[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= high:
            raise ValueError("同色系随机的色相、饱和度和明暗变化须为0–30。")
    if type(layer["color_seed"]) is not int or not 0 <= layer["color_seed"] <= 2147483647:
        raise ValueError("随机颜色组编号必须为0–2147483647的整数。")
    if layer.get("color_mode") == "family" and not QtGui.QColor(str(layer.get("color", ""))).isValid():
        raise ValueError("请先选择有效的基准文字颜色。")


def job_color_scope(job):
    if isinstance(job.get("_color_scope"), str) and job["_color_scope"]:
        return job["_color_scope"]
    # Independent of queue UUID, status, cache directory and source timestamps.
    # A pool preview and its newly-created quick-export row therefore agree.
    data = [os.path.normcase(str(Path(job[key]).resolve())) if job.get(key) else ""
            for key in ("path", "task_dir")]
    data += [str(job.get(key, "")) for key in ("title", "body")]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode("utf8")).hexdigest()


def family_color(layer, scope="design"):
    normalize_color(layer)
    base = QtGui.QColor(layer["color"])
    if layer["color_mode"] == "fixed": return base.name(base.NameFormat.HexArgb if base.alpha() != 255 else base.NameFormat.HexRgb)
    seed = hashlib.sha256(json.dumps(["text-color-v1", layer.get("id", ""), layer["color_seed"], str(scope)],
                                   ensure_ascii=False).encode("utf8")).digest()
    rng = random.Random(seed)
    hue, saturation, value, alpha = base.getHsvF()
    if hue < 0 or saturation < 1/255:
        # White/gray has no hue family. Never invent a red/pink family for it.
        hue, saturation = 0, 0
    else:
        hue = (hue + rng.uniform(-layer["color_hue_variation"], layer["color_hue_variation"])/360) % 1
        saturation = min(1, max(0, saturation+rng.uniform(-layer["color_saturation_variation"], layer["color_saturation_variation"])/100))
    value = min(1, max(0, value+rng.uniform(-layer["color_value_variation"], layer["color_value_variation"])/100))
    result = QtGui.QColor.fromHsvF(hue, saturation, value, alpha)
    return result.name(result.NameFormat.HexArgb if result.alpha() != 255 else result.NameFormat.HexRgb)


def resolve_colors(layers, scope="design"):
    result = copy.deepcopy(layers)
    from .components import walk_components
    for layer in walk_components(result):
        if layer.get("kind") in {"title", "body", "text", "caption"} and layer.get("color_mode", "fixed") == "family":
            layer["color"] = family_color(layer, scope)
            # Runtime clone only: prevents a second render pass re-randomizing
            # an already-picked color. Stored settings/base color stay intact.
            layer["color_mode"] = "fixed"
    return result


class ColorSamples(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.colors = []
        self.setMinimumHeight(28)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setToolTip("随机范围示例，不代表当前视频的最终颜色；请看画布预览。")

    def set_layer(self, layer):
        self.colors = [QtGui.QColor(family_color(dict(layer), "sample-"+str(i))) for i in range(7)]
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        for index, color in enumerate(self.colors):
            left, right = self.width()*index/7, self.width()*(index+1)/7
            painter.setBrush(color)
            painter.drawRoundedRect(QtCore.QRectF(left+2, 3, max(1, right-left-4), 22), 3, 3)
        painter.end()
