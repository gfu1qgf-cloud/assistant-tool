"""Visual file/pool adapter and shared FFmpeg compositing; never modify inputs."""
import math
from pathlib import Path
import subprocess
from qt_compat import QtCore, QtGui, QtWidgets
from .visual_animation import DEFAULTS as ANIMATION_DEFAULTS, normalize_animation
from .chroma_matte import DEFAULTS as MATTE_DEFAULTS, KEY_COLORS, levels, remap, key_filters, MatteLevels
from .chroma_key import (DEFAULTS as KEY_DEFAULTS, normalize_key, key_color, despill_type,
                         validate_mask, enhanced_alpha, add_keyed_source)

ANIMATED_SUFFIXES = frozenset({".gif", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts"})
VISUAL_SUFFIXES = ANIMATED_SUFFIXES | {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".tif", ".tiff"}
VISUAL_FILTER = "图片 / 视频 / GIF (" + " ".join("*" + item for item in sorted(VISUAL_SUFFIXES)) + ")"
DEFAULTS = {"blend_mode": "normal", "key_mode": "none", "key_similarity": .15,
            "key_softness": .08, "key_despill": False, "media_loop": True,
            **MATTE_DEFAULTS, **ANIMATION_DEFAULTS, **KEY_DEFAULTS}

def is_animated(path):
    return Path(str(path)).suffix.casefold() in ANIMATED_SUFFIXES

def normalize_visual(layer):
    for key, value in DEFAULTS.items():
        layer.setdefault(key, value)
    normalize_key(layer)
    if layer["blend_mode"] not in {"normal", "add", "screen"}:
        raise ValueError("视觉元素的叠加／抠像模式无效。")
    for key, low in (("key_similarity", .00001), ("key_softness", 0)):
        value = layer[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= 1:
            raise ValueError("抠像容差／边缘柔化必须为0–1。")
    if type(layer["key_despill"]) is not bool or type(layer["media_loop"]) is not bool:
        raise ValueError("视觉元素循环／去色溢开关无效。")
    levels(layer)
    normalize_animation(layer)

def frame(path, bounds):
    """Poster decode is used only by image/overlay workers, never a paint call."""
    from .engine import resolve_tools
    ffmpeg, _ = resolve_tools()
    result = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vf", f"scale={bounds.width()}:{bounds.height()}:force_original_aspect_ratio=decrease",
        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    image = QtGui.QImage.fromData(result.stdout)
    if result.returncode or image.isNull():
        raise ValueError("无法读取视觉元素首帧：" + Path(path).name)
    return image

def import_visuals(paths, directory, cancel=None):
    from .layers import import_images
    import uuid
    layers, errors = [], []
    for raw in paths:
        if cancel and cancel.is_set():
            break
        path = Path(raw).resolve()
        if is_animated(path):
            try:
                if not path.is_file():
                    raise FileNotFoundError("素材不存在。")
                frame(path, QtCore.QSize(160, 160))
                layers.append({"id": uuid.uuid4().hex, "kind": "image", "name": path.stem,
                    "path": str(path), "thumbnail": "", "source": "fixed", "enabled": True, "opacity": 100,
                    "x": 50, "y": 50, "width": 30, "height": 30})
            except Exception as error:
                errors.append(path.name + "：" + str(error))
        else:
            result = import_images([str(path)], directory, cancel)
            layers.extend(result["layers"])
            errors.extend(result["errors"])
    return {"layers": layers, "errors": errors, "cancelled": bool(cancel and cancel.is_set())}

def needs_plane(layer):
    return layer["kind"] == "image" and (is_animated(layer.get("path", "")) or
        layer.get("blend_mode", "normal") != "normal" or layer.get("key_mode", "none") != "none" or
        layer.get("visual_animation", "none") != "none")

def keyed_image(image, layer):
    if layer.get("key_mode", "none") == "none":
        return image
    # Vectorized RGB distance matches colorkey's similarity domain. This is a
    # poster only; continuous preview/final use FFmpeg, not Python frame loops.
    import numpy as np
    rgba = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    data = np.frombuffer(rgba.constBits().asstring(rgba.sizeInBytes()), dtype=np.uint8).reshape(
        rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4].reshape(rgba.height(), rgba.width(), 4).copy()
    key = np.array(key_color(layer), dtype=np.float32)
    distance = np.sqrt(np.mean(((data[:, :, :3].astype(np.float32)-key)/255)**2, axis=2))
    softness = layer.get("key_softness", .08)
    alpha = (np.clip((distance-layer.get("key_similarity", .15))/softness, 0, 1) if softness else
             distance > layer.get("key_similarity", .15))
    protection = np.zeros_like(alpha, dtype=np.float32)
    if layer.get("key_method", "normal") == "enhanced":
        mask_path = validate_mask(layer)
        trimap = None
        if mask_path:
            from .layers import read_image
            mask = read_image(mask_path, rgba.size()).scaled(rgba.size(),
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            mask = mask.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
            trimap = np.frombuffer(mask.constBits().asstring(mask.sizeInBytes()), dtype=np.uint8).reshape(
                mask.height(), mask.bytesPerLine())[:, :mask.width()*4].reshape(mask.height(), mask.width(), 4)
        alpha, protection = enhanced_alpha(alpha, layer, trimap)
    else:
        alpha = remap(alpha, layer)
    data[:, :, 3] = np.rint(data[:, :, 3]*alpha).astype(np.uint8)
    spill = despill_type(layer) if layer.get("key_despill") else ""
    if spill:
        channel = 1 if spill == "green" else 2
        other = [index for index in range(3) if index != channel]
        original = data[:, :, channel].astype(np.float32)
        cleaned = np.minimum(original, np.maximum(data[:, :, other[0]], data[:, :, other[1]]))
        data[:, :, channel] = np.rint(cleaned*(1-protection)+original*protection).astype(np.uint8)
    return QtGui.QImage(data.tobytes(), rgba.width(), rgba.height(), rgba.width()*4,
                        QtGui.QImage.Format.Format_RGBA8888).copy()

def preview_planes(title, body, width, height, settings, cache):
    from .captions import static_planes
    from .layers import compose_layers, DEFAULT_LAYERS
    from .layout import render_preview_overlay
    result = []
    for kind, value in static_planes(title, body, width, height, settings, cache):
        if kind == "node_effect":
            continue  # Editing poster is not a graph execution side effect.
        if kind == "image":
            result.append(("normal", value))
        elif kind == "visual":
            single = dict(value, blend_mode="normal")
            result.append((value["blend_mode"], compose_layers({}, [single], width, height, 0, cache)))
        else:
            selected = [value] + [dict(default, enabled=False) for default in DEFAULT_LAYERS]
            image, _ = render_preview_overlay(title, body, width, height,
                {**settings, "layers": selected, "darkness": 0, "background_tint_strength": 0}, cache)
            result.append(("normal", image))
    return result

def add_plane(args, filters, layer, input_index, background, label, width, height, duration, fps, *, timing=None):
    """Alpha-correct normal/add/screen, with transparent clipping at canvas edges."""
    normalize_visual(layer)
    local_duration = timing.source_duration(duration) if timing else duration
    path = layer["path"]
    if not Path(path).is_file() or Path(path).suffix.casefold() not in VISUAL_SUFFIXES:
        raise ValueError("视觉元素文件不存在或格式不支持：" + str(path))
    if is_animated(path):
        if layer["media_loop"]:
            args += ["-stream_loop", "-1"]
        args += ["-i", path]
    else:
        args += ["-loop", "1", "-i", path]
    box_w, box_h = max(2, round(width*layer["width"]/100)), max(2, round(height*layer["height"]/100))
    if box_w * box_h > 64_000_000 or max(box_w, box_h) > 16384:
        raise ValueError('视觉图层放大后像素过大，请减小宽度／高度；未启动可能耗尽内存的缩放。')
    # Fraction gained float-style formatting after our Python 3.10 runtime.
    effect = [f"setpts=PTS-STARTPTS,fps={float(fps):.9f},format=rgba"]
    input_label = f"{input_index}:v"
    consumed = 1
    if layer["key_mode"] != "none":
        # colorkey replaces (rather than multiplies) an existing alpha channel.
        # Keep that native mask separate so transparent PNG/GIF/MOV pixels do
        # not become visible when the keyed matte is made more opaque.
        input_label, consumed = add_keyed_source(args, filters, layer, input_index, label, fps, is_animated,
                                                duration=local_duration)
        effect = []
    effect += [f"scale={box_w}:{box_h}:force_original_aspect_ratio=decrease"]
    movement = "0"
    if layer["visual_animation"] != "none":
        from .visual_animation import expressions
        scale, movement = expressions(layer, local_duration)
        if scale != "1":
            # Fixed input dimensions, changing output dimensions; preserve the
            # source aspect and re-center at every frame, not at filter init.
            effect.append(f"scale=w='max(1,round(iw*({scale})))':h='max(1,round(ih*({scale})))':eval=frame")
    effect += [f"colorchannelmixer=aa={layer['opacity']/100:.8f}", f"trim=duration={local_duration:.9f}", "setsar=1"]
    filters.append(f"[{input_label}]" + ",".join(effect) + f"[{label}_source]")
    filters.append(f"color=c=black@0:s={width}x{height}:r={float(fps):.9f}:d={local_duration:.9f},format=rgba[{label}_canvas]")
    # With looping disabled, hide the overlay when it ends instead of freezing it.
    filters.append(f"[{label}_canvas][{label}_source]overlay=x='{width*layer['x']/100:.4f}-overlay_w/2':"
        f"y='{height*layer['y']/100:.4f}-overlay_h/2+{box_h}*({movement})':eval=frame:"
        f"format=auto:eof_action=pass:repeatlast=0[{label}_plane]")
    plane = label + "_plane"
    if timing:
        from .overlay_timing import add_timed_plane
        plane = add_timed_plane(filters, plane, label + "_timed", width, height, duration, fps, timing,
                                center=(width*layer['x']/100, height*layer['y']/100))
    if layer["blend_mode"] == "normal":
        filters.append(f"[{background}][{plane}]overlay=0:0:format=auto:eof_action=pass[{label}]")
    else:
        mode = "addition" if layer["blend_mode"] == "add" else "screen"
        filters += [f"[{background}]split=2[{label}_base][{label}_original]",
            f"[{plane}]split=2[{label}_rgb][{label}_alpha_source]",
            f"[{label}_alpha_source]alphaextract[{label}_alpha]",
            f"[{label}_base]format=gbrp[{label}_base_rgb]",
            f"[{label}_rgb]format=gbrp[{label}_source_rgb]",
            f"[{label}_base_rgb][{label}_source_rgb]blend=all_mode={mode}[{label}_blended]",
            f"[{label}_blended][{label}_alpha]alphamerge[{label}_masked]",
            f"[{label}_original][{label}_masked]overlay=0:0:format=auto[{label}]"]
    return consumed

class VisualProperties(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal(object)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layer, self.loading = None, False
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        from .property_sections import PropertySections
        self.sections = PropertySections()
        root.addWidget(self.sections)
        basic = QtWidgets.QWidget(); self.basic_form = QtWidgets.QFormLayout(basic)
        self.basic_form.setContentsMargins(0,0,0,0)
        self.basic_form.setRowWrapPolicy(self.basic_form.RowWrapPolicy.WrapLongRows)
        self.basic_form.setFieldGrowthPolicy(self.basic_form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        root.insertWidget(0,basic)
        form = self._page("抠像")
        animation = self._page("动画")
        self.controls = {}
        for key, label, choices in (("blend_mode", "叠加模式", (("普通", "normal"), ("添加", "add"), ("滤色", "screen"))),
                                    ("key_mode", "抠像颜色", (("关闭", "none"), ("绿色预设", "green"), ("蓝色预设", "blue"), ("黑色预设", "black"), ("自定义颜色", "custom"))),
                                    ("key_method", "抠像算法", (("普通 · 颜色扣除", "normal"), ("增强 · 轮廓保护", "enhanced")))):
            control = QtWidgets.QComboBox()
            for name, value in choices:
                control.addItem(name, value)
            control.currentIndexChanged.connect(self.edit)
            self.controls[key] = control
            (self.basic_form if key == 'blend_mode' else form).addRow(label, control)
        protect = QtWidgets.QCheckBox("保护轮廓内封闭的同色装饰")
        protect.toggled.connect(self.edit)
        self.controls["key_protect_inside"] = protect
        form.addRow(protect)
        from .chroma_key_ui import KeyDetails
        self.key_details = KeyDetails()
        self.key_details.changed.connect(self._key_details_changed)
        form.addRow(self.key_details)
        for key, label, low in (("key_similarity", "颜色容差", .00001), ("key_softness", "边缘柔化", 0)):
            control = QtWidgets.QDoubleSpinBox()
            control.setDecimals(3); control.setRange(low, 1); control.setSingleStep(.01)
            control.valueChanged.connect(self.edit)
            self.controls[key] = control
            form.addRow(label, control)
        for key, label in (("key_despill", "抑制绿／蓝色溢出"), ("media_loop", "素材不足时循环；关闭则播完隐藏")):
            control = QtWidgets.QCheckBox(label)
            control.toggled.connect(self.edit)
            self.controls[key] = control
            (self.basic_form if key == 'media_loop' else form).addRow(control)
        self.matte_levels = MatteLevels()
        self.controls.update(self.matte_levels.controls)
        self.matte_levels.changed.connect(self.edit)
        form.addRow("扣像透明度修正", self.matte_levels)
        note = QtWidgets.QLabel("前景发虚：把右端向左拉；背景残留：把左端向右拉。中间保持平滑过渡。\n"
            "黑幕会连同前景里的黑色一起扣除，请降低颜色容差；光效／火花也可选滤色（会变亮，不是扣像）。\n"
            "这些阈值只修正扣像，图层自身不透明度仍独立控制；0 → 100保持原效果。")
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(note)
        basic.setToolTip("支持图片、视频和GIF；保留原比例，不加入素材自身音轨。播放预览与成品使用相同抠像和叠加。")
        from .component_ui import Combo, Spin
        from .visual_animation import CHOICES
        mode = Combo()
        for label, value in CHOICES:
            mode.addItem(label, value)
        mode.currentIndexChanged.connect(self.edit)
        self.controls["visual_animation"] = mode
        animation.addRow("动画效果", mode)
        self._last_animation = {}
        self.animation_toggle = QtWidgets.QCheckBox('启用图片／视频动画')
        self.sections.sections['animation'].set_enable_control(self.animation_toggle)
        self.animation_toggle.toggled.connect(self._toggle_animation)
        for key, label, low, high, suffix in (
            ("animation_strength", "强度", 0, 100, " %"),
            ("animation_period", "每次循环", .2, 120, " 秒"),
            ("animation_start_delay", "开始延迟", 0, 86400, " 秒"),
            ("animation_end_early", "提前结束", 0, 86400, " 秒"),
        ):
            control = Spin()
            control.setRange(low, high); control.setDecimals(2)
            control.setSingleStep(1 if suffix == " %" else .1)
            control.setSuffix(suffix); control.valueChanged.connect(self.edit)
            self.controls[key] = control
            animation.addRow(label, control)
        self.controls["animation_strength"].setToolTip("缩放类：最大放大比例；浮动：相对元素框高度的移动幅度。0为不变化，原素材不会被修改。")
        self.controls["animation_period"].setToolTip("律动、呼吸和浮动的周期；不是音乐自动踩点。周期越短动作越快。")
        self.controls["animation_start_delay"].setToolTip("开始前保持起点。按最终成品时长计算，启用人声时跟随人声时长。")
        self.controls["animation_end_early"].setToolTip("提前结束动作，余下时间保持结束姿态。与开始延迟之和必须小于最终视频时长。")
        from .property_pairs import pair_form_fields
        pair_form_fields(animation,'动画时间',[
            ('start','延迟',self.controls['animation_start_delay']),('end','提前结束',self.controls['animation_end_early'])])
        note = QtWidgets.QLabel("律动：短促放大后恢复，循环重复；呼吸：平滑放大缩小。\n"
            "推近／拉远覆盖最终成品时长；动态画面请点击“播放预览”。\n"
            "设计视图显示原尺寸。放大可能超出蓝色元素框，画布之外会裁切；同样适用于图片池、GIF和视频。")
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        animation.addRow(note)
        self._update_motion_controls()
        self._update_key_controls()

    def _page(self, title):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setContentsMargins(4, 4, 4, 4)
        form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.sections.add_section('visual' if title == '抠像' else 'animation', title, page)
        return form

    def _update_motion_controls(self):
        mode = self.controls["visual_animation"].currentData()
        active = mode != 'none'
        if self.layer and active:
            self._last_animation[self.layer['id']] = mode
        blocker = QtCore.QSignalBlocker(self.animation_toggle)
        self.animation_toggle.setChecked(active)
        del blocker
        self.sections.sections['animation'].content.setEnabled(active)
        for key in ANIMATION_DEFAULTS:
            if key != "visual_animation":
                self.controls[key].setEnabled(mode != "none" and (
                    key != "animation_period" or mode in {"pulse", "breathe", "float"}))

    def _toggle_animation(self, enabled):
        if self.loading or not self.layer:
            return
        mode = self.controls['visual_animation']
        if mode.currentData() != 'none':
            self._last_animation[self.layer['id']] = mode.currentData()
        mode.setCurrentIndex(mode.findData(self._last_animation.get(self.layer['id'], 'pulse') if enabled else 'none'))
    def _update_key_controls(self):
        mode = self.controls["key_mode"].currentData()
        for key in ("key_similarity", "key_softness"):
            self.controls[key].setEnabled(mode != "none")
        self.controls["key_method"].setEnabled(mode != "none")
        self.controls["key_protect_inside"].setVisible(self.controls["key_method"].currentData() == "enhanced")
        self.controls["key_protect_inside"].setEnabled(mode != "none")
        self.controls["key_despill"].setEnabled(mode != "none" and bool(self.layer) and bool(despill_type(self.layer)))
        self.matte_levels.setEnabled(mode != "none")
        self.key_details.set_layer(self.layer)
    def _key_details_changed(self, changes):
        if self.loading or not self.layer: return
        self.layer.update(changes)
        self.loading = True
        self.controls["key_mode"].setCurrentIndex(self.controls["key_mode"].findData(self.layer["key_mode"]))
        self.loading = False
        self._update_key_controls()
        self.changed.emit(dict(self.layer))


    def set_layer(self, layer):
        self.loading = True
        self.layer = dict(layer) if layer else None
        self.setEnabled(bool(layer))
        if layer:
            normalize_visual(self.layer)
            for key, control in self.controls.items():
                if key in MATTE_DEFAULTS: continue
                value = self.layer[key]
                if isinstance(control, QtWidgets.QComboBox): control.setCurrentIndex(control.findData(value))
                elif isinstance(control, QtWidgets.QCheckBox): control.setChecked(value)
                else: control.setValue(value)
            self.matte_levels.set_levels(self.layer["key_alpha_low"], self.layer["key_alpha_high"])
        self.loading = False
        self._update_motion_controls()
        self._update_key_controls()
    def edit(self, *_args):
        if self.loading or not self.layer: return
        for key, control in self.controls.items():
            self.layer[key] = (control.currentData() if isinstance(control, QtWidgets.QComboBox) else
                               control.isChecked() if isinstance(control, QtWidgets.QCheckBox) else control.value())
        self._update_motion_controls()
        self._update_key_controls()
        self.changed.emit(dict(self.layer))


def visual_property_panel(services=None, *, mode='batch'):
    from .component_inspector import ComponentPropertyPanel
    panel = ComponentPropertyPanel(mode=mode)
    from .inspector_layout_contract import require_flat_layout
    require_flat_layout(panel)
    panel.add_source()
    panel.add_geometry()
    panel.visual_properties = panel.add_bound('visual', '', VisualProperties(), fields=set(DEFAULTS), section=False)
    panel.sections.update(panel.visual_properties.sections.sections)
    return panel
