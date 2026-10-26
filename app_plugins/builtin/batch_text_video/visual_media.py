"""Visual file/pool adapter and shared FFmpeg compositing; never modify inputs."""
import math
from pathlib import Path
import subprocess
from qt_compat import QtCore, QtGui, QtWidgets
from .visual_animation import DEFAULTS as ANIMATION_DEFAULTS, SHOW_DEFAULTS, normalize_animation, show_window
from .chroma_matte import DEFAULTS as MATTE_DEFAULTS, KEY_COLORS, levels, remap, key_filters, MatteLevels, softness_width
from .chroma_key import (DEFAULTS as KEY_DEFAULTS, normalize_key, key_color, despill_type,
                         validate_mask, enhanced_alpha, add_keyed_source)

ANIMATED_SUFFIXES = frozenset({".gif", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts"})
VISUAL_SUFFIXES = ANIMATED_SUFFIXES | {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".tif", ".tiff"}
VISUAL_FILTER = "图片 / 视频 / GIF (" + " ".join("*" + item for item in sorted(VISUAL_SUFFIXES)) + ")"
DEFAULTS = {"blend_mode": "normal", "key_mode": "none", "key_similarity": .15,
            "key_softness": .08, "key_despill": False, "media_loop": True, "use_saved_matte": True,
            "media_loop_transition": "none", "media_loop_fade": .3,
            **MATTE_DEFAULTS, **ANIMATION_DEFAULTS, **KEY_DEFAULTS, **SHOW_DEFAULTS}

def is_animated(path):
    return Path(str(path)).suffix.casefold() in ANIMATED_SUFFIXES

def normalize_visual(layer):
    # Legacy inline key settings without an explicit ownership flag keep using
    # their inline configuration. An explicit saved-result choice is exclusive.
    if 'use_saved_matte' not in layer:
        layer['use_saved_matte'] = not (layer.get('key_mode', 'none') != 'none' or layer.get('key_method') == 'ai')
    for key, value in DEFAULTS.items():
        layer.setdefault(key, value)
    normalize_key(layer)
    if type(layer['use_saved_matte']) is not bool: raise ValueError('素材已存抠像开关无效。')
    if layer["blend_mode"] not in {"normal", "add", "screen"}:
        raise ValueError("视觉元素的叠加／抠像模式无效。")
    for key, low in (("key_similarity", .00001), ("key_softness", 0)):
        value = layer[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= 1:
            raise ValueError("抠像容差／边缘柔化必须为0–1。")
    if type(layer["key_despill"]) is not bool or type(layer["media_loop"]) is not bool:
        raise ValueError("视觉元素循环／去色溢开关无效。")
    if layer['media_loop_transition'] not in {'none', 'fade'}:
        raise ValueError('素材循环衔接方式无效。')
    fade = layer['media_loop_fade']
    if type(fade) not in (int, float) or not math.isfinite(fade) or not 0 <= fade <= 3:
        raise ValueError('素材循环叠化时长须为0–3秒。')
    levels(layer)
    normalize_animation(layer)

def frame(path, bounds):
    """Poster decode is used only by image/overlay workers, never a paint call."""
    from .engine import resolve_tools
    ffmpeg, _ = resolve_tools()
    from .dnx_alpha import poster
    native = poster(path, bounds, ffmpeg=ffmpeg)
    if native is not None: return native
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
    return layer["kind"] == "image" and (layer.get("_matting_binding", {}).get("mode") == "dynamic" or is_animated(layer.get("path", "")) or
        layer.get("blend_mode", "normal") != "normal" or layer.get("key_mode", "none") != "none" or
        layer.get("visual_animation", "none") != "none" or layer.get('visual_timing_enabled', False))

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
    if layer.get("key_cleanup", False):
        # Connectivity is sensitive to a 0/1 alpha rounding difference. Match
        # colorkey's double precision and uint8 truncation before reconstruction.
        squared = np.zeros(data.shape[:2], dtype=np.float64)
        for index in range(3):
            delta = data[:, :, index].astype(np.float64)-key[index]
            squared += delta*delta
        distance = np.sqrt(squared/(255*255*3))
        del squared, delta
    else:
        distance = np.sqrt(np.mean(((data[:, :, :3].astype(np.float32)-key)/255)**2, axis=2))
    softness = softness_width(layer)
    alpha = (np.clip((distance-layer.get("key_similarity", .15))/softness, 0, 1) if softness else
             distance > layer.get("key_similarity", .15))
    if layer.get("key_cleanup", False):
        alpha = np.floor(alpha*255)/255
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
        from .matte_geometry import shift
        alpha = shift(alpha, layer.get('key_matte_grow', 0))
    if layer.get("key_cleanup", False):
        alpha = np.floor(alpha*255)/255
        data[:, :, 3] = np.floor(data[:, :, 3]*alpha).astype(np.uint8)
    else:
        data[:, :, 3] = np.rint(data[:, :, 3]*alpha).astype(np.uint8)
    if layer.get("key_cleanup", False):
        from .chroma_cleanup import clean_alpha
        protected = np.floor(protection*255).astype(np.uint8)
        data[:, :, 3] = clean_alpha(data[:, :, 3], layer, protected)
    spill = despill_type(layer) if layer.get("key_despill") else ""
    from .chroma_despill import enabled, correct_rgb
    if enabled(layer):
        correct_rgb(data, layer, protection)
    elif spill and layer.get("key_despill_mode", "basic") == "basic":
        from .chroma_despill import correct_basic
        correct_basic(data, layer, protection)
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
            result.append((value["blend_mode"], compose_layers({}, [single], width, height, 0, cache,
                editor_timing=settings.get('_editor_timing'))))
        else:
            selected = [value] + [dict(default, enabled=False) for default in DEFAULT_LAYERS]
            image, _ = render_preview_overlay(title, body, width, height,
                {**settings, "layers": selected, "darkness": 0, "background_tint_strength": 0}, cache)
            result.append(("normal", image))
    return result

def add_plane(args, filters, layer, input_index, background, label, width, height, duration, fps, *, timing=None, ffmpeg=''):
    """Alpha-correct normal/add/screen, with transparent clipping at canvas edges."""
    normalize_visual(layer)
    # An explicit timing node owns the window; never apply a native one twice.
    if timing is None:
        timing = show_window(layer, duration)
    local_duration = timing.source_duration(duration) if timing else duration
    path = layer["path"]
    if not Path(path).is_file() or Path(path).suffix.casefold() not in VISUAL_SUFFIXES:
        raise ValueError("视觉元素文件不存在或格式不支持：" + str(path))
    box_w, box_h = max(2, round(width*layer["width"]/100)), max(2, round(height*layer["height"]/100))
    if box_w * box_h > 64_000_000 or max(box_w, box_h) > 16384:
        raise ValueError('视觉图层放大后像素过大，请减小宽度／高度；未启动可能耗尽内存的缩放。')
    from .matting_live import dynamic, StreamInput
    from .visual_loop import enabled as loop_fade_enabled, LoopInput
    from .chroma_key import active_mask
    # A still source with a moving trimap is still time-dependent. Live AI and
    # video/GIF sources also keep their existing stream semantics.
    static_source = not (is_animated(path) or dynamic(layer) or is_animated(active_mask(layer)))
    candidate = loop_fade_enabled(layer)
    executable = ''
    if dynamic(layer) or candidate:
        from .engine import resolve_tools
        executable = ffmpeg or (args[0] if args and not args[0].startswith('-') else resolve_tools()[0])
    loop_source = LoopInput(layer, local_duration, fps, executable, (box_w, box_h)) if candidate else None
    live_loop = loop_source is not None and loop_source.overlap > 0
    if dynamic(layer) or live_loop:
        source = (loop_source if live_loop else
                  StreamInput(layer['_matting_binding'], local_duration, layer['media_loop'], fps, executable))
        args += source.arguments()
    elif is_animated(path):
        if layer["media_loop"]:
            args += ["-stream_loop", "-1"]
        args += ["-i", path]
    else:
        args += ["-loop", "1", "-i", path]
    # Fraction gained float-style formatting after our Python 3.10 runtime.
    effect = [f"setpts=PTS-STARTPTS,fps={float(fps):.9f},format=rgba"]
    if static_source:
        effect.append('trim=end_frame=1')
    input_label = f"{input_index}:v"
    consumed = 1
    if not (dynamic(layer) or live_loop):
        from .dnx_alpha import add_source
        input_label, consumed = add_source(args, filters, path, input_index, label+'_dnx',
                                          loop=layer['media_loop'], ffmpeg=ffmpeg)
    if layer["key_mode"] != "none" and not live_loop:
        # colorkey replaces (rather than multiplies) an existing alpha channel.
        # Keep that native mask separate so transparent PNG/GIF/MOV pixels do
        # not become visible when the keyed matte is made more opaque.
        input_label, keyed_count = add_keyed_source(args, filters, layer, input_index, label, fps, is_animated,
                                                duration=local_duration, single_frame=static_source,
                                                source_label=input_label, auxiliary_index=input_index+consumed)
        consumed += keyed_count-1
        effect = []
    effect += [f"scale={box_w}:{box_h}:force_original_aspect_ratio=decrease"]
    if static_source:
        # Cache one fully keyed, despilled, scaled RGBA frame inside FFmpeg.
        # Rebuild the clock before animated scaling, movement and fades. No PNG
        # intermediates, Python frame producer, or unbounded frame cache.
        effect += ['loop=loop=-1:size=1:start=0', f'setpts=N/({float(fps):.9f}*TB)']
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
    if timing:
        from .overlay_timing import add_timed_plane
        plane = add_timed_plane(filters, label+'_source', label + "_timed", width, height, duration, fps, timing,
                                center=(width*layer['x']/100, height*layer['y']/100),
                                raw_object=True, vertical_movement=f'{box_h}*({movement})')
    else:
        filters.append(f"color=c=black@0:s={width}x{height}:r={float(fps):.9f}:d={local_duration:.9f},format=rgba[{label}_canvas]")
        filters.append(f"[{label}_canvas][{label}_source]overlay=x='{width*layer['x']/100:.4f}-overlay_w/2':"
            f"y='{height*layer['y']/100:.4f}-overlay_h/2+{box_h}*({movement})':eval=frame:"
            f"format=auto:eof_action=pass:repeatlast=0[{label}_plane]")
        plane = label + "_plane"
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
    mattingRequested = QtCore.pyqtSignal(str)
    def __init__(self, parent=None):
        super().__init__(parent)
        from .numeric_wheel import protect_numeric_wheels
        protect_numeric_wheels(self)
        self.layer, self.loading = None, False
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        from .property_sections import PropertySections
        self.sections = PropertySections()
        root.addWidget(self.sections)
        basic = QtWidgets.QWidget(); self.basic_form = QtWidgets.QFormLayout(basic)
        basic.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Maximum)
        self.basic_form.setContentsMargins(0,0,0,0)
        self.basic_form.setRowWrapPolicy(self.basic_form.RowWrapPolicy.WrapLongRows)
        self.basic_form.setFieldGrowthPolicy(self.basic_form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        root.insertWidget(0,basic)
        form = self._page("抠像")
        animation = self._page("动画")
        self.controls = {}
        self._form_heading(form, '抠像遮罩（透明范围）')
        self.matting_button = QtWidgets.QPushButton('AI抠像工作台…')
        self.matting_button.setToolTip('调整当前素材的AI遮罩；保存参数后用于预览和渲染，不生成透明视频缓存。')
        self.matting_button.clicked.connect(lambda: self.mattingRequested.emit(self.layer['id']) if self.layer else None)
        self.matting_button.hide(); form.addRow(self.matting_button)
        saved = QtWidgets.QCheckBox('使用工作台已保存的抠像结果／参数')
        saved.setToolTip('勾选后完全使用每个素材在工作台保存的参数，下面的抠像选项停用。没有保存结果的素材保留原样；取消勾选后才使用下面的统一抠像配置。')
        saved.toggled.connect(self.edit); self.controls['use_saved_matte'] = saved; form.addRow(saved)
        self.saved_matte_note = QtWidgets.QLabel(); self.saved_matte_note.setWordWrap(True)
        self.saved_matte_note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(self.saved_matte_note)
        for key, label, choices in (("blend_mode", "叠加模式", (("普通", "normal"), ("添加", "add"), ("滤色", "screen"))),
                                    ("key_mode", "抠像颜色", (("关闭", "none"), ("绿色预设", "green"), ("蓝色预设", "blue"), ("黑色预设", "black"), ("自定义颜色", "custom"))),
                                    ("key_method", "抠像算法", (("普通 · 颜色扣除", "normal"), ("增强 · 轮廓保护", "enhanced"), ("AI · 主体抠像", "ai")))):
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
        for key, label, low in (("key_similarity", "颜色容差", .00001), ("key_softness", "抠像边缘柔化", 0)):
            control = QtWidgets.QDoubleSpinBox()
            control.setDecimals(3); control.setRange(low, 1); control.setSingleStep(.01)
            control.valueChanged.connect(self.edit)
            self.controls[key] = control
            form.addRow(label, control)
        self.controls['key_softness'].setToolTip(
            '0为硬边，1为最柔。只柔化接近抠像颜色的过渡像素，主体不透明度独立控制。\n'
            '需要调整扣除的颜色范围时，请使用“颜色容差”。')
        grow = QtWidgets.QDoubleSpinBox(); grow.setRange(-20, 20); grow.setDecimals(2); grow.setSingleStep(.25); grow.setSuffix(' px')
        grow.setToolTip('调整抠像透明遮罩（Alpha）：负值收缩人物保留范围，正值扩展，0不调整；与去溢色作用范围独立。可先试-0.25或-0.5，过度收缩会损失头发和手指细节。AI模式与工作台已有收边叠加，合计范围为±20像素。')
        grow.valueChanged.connect(self.edit); self.controls['key_matte_grow'] = grow
        form.addRow('抠像遮罩收缩－／扩展＋', grow)
        self.matte_levels = MatteLevels()
        self.controls.update(self.matte_levels.controls)
        self.matte_levels.changed.connect(self.edit)
        form.addRow("抠像遮罩透明度修正", self.matte_levels)
        note = QtWidgets.QLabel("前景发虚：把右端向左拉；背景残留：把左端向右拉。中间保持平滑过渡。\n"
            "黑幕会连同前景里的黑色一起扣除，请降低颜色容差；光效／火花也可选滤色（会变亮，不是扣像）。\n"
            "这些阈值只修正扣像，图层自身不透明度仍独立控制；0 → 100保持原效果。")
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        form.addRow(note)
        cleanup = QtWidgets.QCheckBox("去碎块（清除悬空小块）")
        cleanup.toggled.connect(self.edit)
        self.controls["key_cleanup"] = cleanup
        form.addRow(cleanup)
        cleanup_size = QtWidgets.QSpinBox()
        cleanup_size.setRange(1, 12); cleanup_size.setSuffix(" px")
        cleanup_size.valueChanged.connect(self.edit)
        self.controls["key_cleanup_size"] = cleanup_size
        form.addRow("碎块尺度", cleanup_size)
        cleanup.setToolTip("去除与较粗主体分离的细小残片；相连的细发丝保留原轮廓。默认关闭，不修改源文件。")
        cleanup_size.setToolTip("按源素材像素计算，数值越大清理越强。建议先用1–2；孤立的小饰品可用增强模式白色辅助遮罩保护。")
        screen_cleanup = QtWidgets.QCheckBox('清除边缘／指缝残留幕色（AI）')
        screen_cleanup.setToolTip('结合原图的绿／蓝幕色清理AI遮罩边缘与窄缝，避免补洞把指缝填回去；保留画笔优先。需要保留边缘绿色装饰时可关闭。')
        screen_cleanup.toggled.connect(self.edit)
        self.controls['key_screen_cleanup'] = screen_cleanup
        form.addRow(screen_cleanup)
        self._form_heading(form, '去溢色（颜色修正）')
        for key, label in (("key_despill", "抑制绿／蓝色溢出"), ("media_loop", "素材不足时循环；关闭则播完隐藏")):
            control = QtWidgets.QCheckBox(label)
            control.toggled.connect(self.edit)
            self.controls[key] = control
            (self.basic_form if key == 'media_loop' else form).addRow(control)
        transition = QtWidgets.QComboBox()
        transition.addItem('直接衔接', 'none'); transition.addItem('叠化', 'fade')
        transition.currentIndexChanged.connect(self.edit)
        self.controls['media_loop_transition'] = transition
        self.basic_form.addRow('循环交界', transition)
        fade = QtWidgets.QDoubleSpinBox(); fade.setRange(0, 3); fade.setDecimals(2)
        fade.setSingleStep(.1); fade.setSuffix(' 秒'); fade.setSpecialValueText('0 秒（关闭）')
        fade.valueChanged.connect(self.edit); self.controls['media_loop_fade'] = fade
        self.basic_form.addRow('叠化时长', fade)
        transition.setToolTip('仅控制此图片／视频控件的首尾循环交界，不改变底层背景；不够长且需要循环时才叠化。')
        fade.setToolTip('首尾重叠过渡；过长时按素材长度和可用帧缓冲自动缩短。设计画面显示首帧，交界效果请看播放预览。')
        despill_mode = QtWidgets.QComboBox()
        despill_mode.addItem("基础 · 全图保守", "basic")
        despill_mode.addItem("边缘 · 仅修正边缘颜色（推荐）", "edge")
        despill_mode.currentIndexChanged.connect(self.edit)
        self.controls["key_despill_mode"] = despill_mode
        form.addRow("去色溢方式", despill_mode)
        for key, label, low, high, value_suffix in (
            ("key_despill_amount", "去色溢强度", 0, 100, " %"),
            ("key_despill_width", "去溢色作用范围", 1, 12, " px"),
            ("key_despill_luma", "亮度补偿", 0, 100, " %"),
            ("key_despill_basic_luma", "亮度补偿", 0, 100, " %"),
            ("key_despill_softness", "去溢色过渡柔和度", 0, 100, " %"),
        ):
            control = QtWidgets.QSpinBox() if key == "key_despill_width" else QtWidgets.QDoubleSpinBox()
            if isinstance(control, QtWidgets.QDoubleSpinBox): control.setDecimals(1)
            control.setRange(low, high); control.setSuffix(value_suffix)
            control.valueChanged.connect(self.edit)
            self.controls[key] = control
            form.addRow(label, control)
        despill_mode.setToolTip("边缘方式从抠像透明度生成柔和遮罩，只修正轮廓附近的绿／蓝偏色；不会改变透明度。旧项目保留基础方式。")
        self.controls["key_despill_width"].setToolTip("按源素材像素计算；范围越大，越向主体内部延伸。遮罩柔化后最远影响约两倍范围。")
        self.controls["key_despill_luma"].setToolTip("用中性色补回部分去色造成的亮度下降；0不补偿，100尽量保持亮度。过亮时降低此值。")
        self.controls['key_despill_basic_luma'].setToolTip('补回基础去色造成的亮度下降。0保留原基础效果；100尽量保持原亮度。与边缘模式的亮度参数分别保存。')
        self.controls['key_despill_softness'].setToolTip('提高后用平滑曲线降低边缘过渡带的过强修正，缓解突兀或偏色的边。0保留原边缘效果；不会额外修改透明度。')
        self.edge_preset = QtWidgets.QPushButton('抠像收边＋去溢色（组合预设）')
        self.edge_preset.setAutoDefault(False); self.edge_preset.clicked.connect(self._apply_edge_preset)
        self.edge_preset.setToolTip('同时调整抠像遮罩与颜色修正：抠像遮罩收缩0.5px、边缘去色85%、范围4px、亮度补偿75%、过渡柔和70%。不改变抠像算法或来源；可撤销后继续微调。')
        form.addRow(self.edge_preset)
        basic.setToolTip("支持图片、视频和GIF；保留原比例，不加入素材自身音轨。播放预览与成品使用相同抠像和叠加。")
        appearance = self._page('入场与退场')
        self.appearance_toggle = QtWidgets.QCheckBox('启用入场／退场动画')
        self.controls['visual_timing_enabled'] = self.appearance_toggle
        self.sections.sections['appearance'].set_enable_control(self.appearance_toggle)
        self.appearance_toggle.toggled.connect(self.edit)
        from .workflow_timing_ui import TimingNodeEditor
        self.appearance_editor = TimingNodeEditor(SHOW_DEFAULTS['visual_timing'], self._appearance_changed,
                                                  mode='layer')
        appearance.addRow(self.appearance_editor)
        from .component_ui import Combo, Spin
        from .visual_animation import CHOICES
        mode = Combo()
        for label, value in CHOICES:
            mode.addItem(label, value)
        mode.currentIndexChanged.connect(self.edit)
        self.controls["visual_animation"] = mode
        animation.addRow("动画效果", mode)
        direction = Combo()
        direction.addItem("先向外放大", "outward"); direction.addItem("先向内缩小（预先放大）", "inward")
        direction.currentIndexChanged.connect(self.edit)
        self.controls["animation_direction"] = direction
        animation.addRow("呼吸方向", direction)
        direction.setToolTip("向内从预放大开始，缩回原适配尺寸后再次放大；不会缩得比控件原尺寸更小。")
        self._last_animation = {}
        self.animation_toggle = QtWidgets.QCheckBox('启用图片／视频动画')
        self.sections.sections['animation'].set_enable_control(self.animation_toggle)
        self.animation_toggle.toggled.connect(self._toggle_animation)
        for key, label, low, high, suffix in (
            ("animation_strength", "强度", 0, 100, " %"),
            ("animation_period", "每次循环", .2, 120, " 秒"),
            ("animation_zoom_seconds", "推近／拉远用时", 0, 3600, " 秒"),
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
        self.controls["animation_zoom_seconds"].setSpecialValueText("跟随成品时长")
        self.controls["animation_zoom_seconds"].setToolTip("0跟随成品时长；指定秒数后在该用时内完成动作，随后保持。与缩放强度独立。")
        self.controls["animation_period"].setToolTip("律动、呼吸和浮动的周期；不是音乐自动踩点。周期越短动作越快。")
        self.controls["animation_start_delay"].setToolTip("开始前保持起点。按最终成品时长计算，启用人声时跟随人声时长。")
        self.controls["animation_end_early"].setToolTip("提前结束动作，余下时间保持结束姿态。与开始延迟之和必须小于最终视频时长。")
        from .property_pairs import pair_form_fields
        pair_form_fields(animation,'动画时间',[
            ('start','延迟',self.controls['animation_start_delay']),('end','提前结束',self.controls['animation_end_early'])])
        note = QtWidgets.QLabel("律动：短促放大后恢复，循环重复；呼吸：平滑放大缩小。\n"
            "推近／拉远可单独设置用时；0跟随最终成品时长。动态画面可拖动动画快览或播放预览。\n"
            "拖动动画快览查看缩放位置。放大可能超出元素框，画布之外会裁切；同样适用于图片池、GIF和视频。")
        note.setWordWrap(True)
        note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        animation.addRow(note)
        self._update_motion_controls()
        self._update_key_controls()

    @staticmethod
    def _form_heading(form, title):
        label = QtWidgets.QLabel(title)
        font = label.font(); font.setBold(True); label.setFont(font)
        form.addRow(label)

    def _page(self, title):
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setContentsMargins(4, 4, 4, 4)
        form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        form.setRowWrapPolicy(form.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.sections.add_section({'抠像': 'visual', '动画': 'animation', '入场与退场': 'appearance'}[title], title, page)
        return form

    def _appearance_changed(self, values):
        if self.loading or not self.layer:
            return
        from .overlay_timing import normalize_timing
        self.layer['visual_timing'] = normalize_timing(values)
        self.changed.emit(dict(self.layer))

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
                    key != "animation_period" or mode in {"pulse", "breathe", "float"}) and (
                    key != "animation_direction" or mode == "breathe") and (
                    key != "animation_zoom_seconds" or mode in {"zoom_in", "zoom_out"}))

    def _toggle_animation(self, enabled):
        if self.loading or not self.layer:
            return
        mode = self.controls['visual_animation']
        if mode.currentData() != 'none':
            self._last_animation[self.layer['id']] = mode.currentData()
        mode.setCurrentIndex(mode.findData(self._last_animation.get(self.layer['id'], 'pulse') if enabled else 'none'))
    def _update_key_controls(self):
        self.controls['media_loop_transition'].setEnabled(self.controls['media_loop'].isChecked())
        self.controls['media_loop_fade'].setEnabled(self.controls['media_loop'].isChecked()
            and self.controls['media_loop_transition'].currentData() == 'fade')
        mode = self.controls["key_mode"].currentData()
        ai = self.controls['key_method'].currentData() == 'ai'
        active_matte = ai or mode != 'none'
        self.controls['key_mode'].setEnabled(bool(self.layer))
        for key in ("key_similarity", "key_softness"):
            self.controls[key].setEnabled(mode != 'none' and not ai)
        self.controls["key_method"].setEnabled(bool(self.layer))
        self.controls["key_protect_inside"].setVisible(self.controls["key_method"].currentData() in {'enhanced', 'ai'})
        self.controls['key_protect_inside'].setText('补洞：填补主体内部封闭孔洞' if ai else '保护轮廓内封闭的同色装饰')
        self.controls['key_protect_inside'].setToolTip('修复AI误扣的封闭内部区域；与外部连通的缺口需在AI工作台用保留画笔修正。原图自带透明区域与去除画笔仍优先保留。')
        self.controls["key_protect_inside"].setEnabled(active_matte)
        can_despill = bool(self.layer) and (ai or mode != 'none' and bool(despill_type(self.layer)))
        self.controls["key_despill"].setEnabled(can_despill)
        active = can_despill and self.controls["key_despill"].isChecked()
        self.controls["key_despill_mode"].setEnabled(active and not ai)
        self.controls['key_screen_cleanup'].setVisible(ai or self.controls['key_method'].currentData() == 'enhanced')
        self.controls['key_screen_cleanup'].setEnabled(active and self.controls['key_despill_amount'].value() > 0)
        self.controls['key_despill_mode'].setToolTip('AI模式统一按主体遮罩边缘去色溢；上方绿色／蓝色预设指定要降低的颜色。' if ai else '选择全图保守修正或按遮罩边缘修正。')
        edge = active and (ai or self.controls["key_despill_mode"].currentData() == "edge")
        self.controls['key_despill_amount'].setEnabled(active)
        for key in ("key_despill_width", "key_despill_luma", "key_despill_softness"):
            self.controls[key].setEnabled(edge)
        self.controls['key_despill_basic_luma'].setEnabled(active and not edge)
        form = self.controls['key_despill_luma'].parentWidget().layout()
        for key, visible in (('key_despill_luma', ai or self.controls['key_despill_mode'].currentData() == 'edge'),
                             ('key_despill_basic_luma', not ai and self.controls['key_despill_mode'].currentData() == 'basic')):
            self.controls[key].setVisible(visible)
            label = form.labelForField(self.controls[key])
            if label: label.setVisible(visible)
        self.controls['key_matte_grow'].setEnabled(active_matte)
        self.edge_preset.setEnabled(can_despill)
        self.controls["key_cleanup"].setEnabled(active_matte)
        self.controls["key_cleanup_size"].setEnabled(active_matte and self.controls["key_cleanup"].isChecked())
        self.matte_levels.setEnabled(active_matte)
        self.key_details.set_layer(self.layer)
        self.key_details.setVisible(not ai)
        saved = self.controls['use_saved_matte'].isChecked()
        self.saved_matte_note.setText('以工作台保存的每素材参数为准；下方抠像选项停用，未保存的素材使用原素材。' if saved else
                                      '使用下方统一抠像参数；暂不套用工作台的已存抠像结果。')
        self.key_details.setEnabled(not saved)
        if saved:
            for key, control in self.controls.items():
                if key.startswith('key_'): control.setEnabled(False)
            self.matte_levels.setEnabled(False); self.edge_preset.setEnabled(False)

    def _apply_edge_preset(self):
        if not self.layer: return
        value = dict(self.layer, key_matte_grow=-.5, key_despill=True, key_despill_mode='edge',
                     key_despill_amount=85., key_despill_width=4, key_despill_luma=75., key_despill_softness=70.)
        self.set_layer(value)
        self.changed.emit(dict(self.layer))
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
            self.appearance_editor.set_values(self.layer['visual_timing'])
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
    panel.visual_properties.mattingRequested.connect(panel.mattingRequested)
    panel.sections.update(panel.visual_properties.sections.sections)
    return panel
