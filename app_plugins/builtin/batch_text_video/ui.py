from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
import copy
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
import traceback
from model.BackgroundTasks import BackgroundTaskHandle
from .background import batch_resources, execute_batch, run_batch_video_job

from qt_compat import QtCore, QtGui, QtWidgets
from .engine import AUDIO_SUFFIXES, VIDEO_SUFFIXES, Cancelled, RENDERERS, output_size, probe, resolve_tools
from .layout import ensure_fonts, render_preview_overlay
from .store import SCHEMES, Store, add_paths, file_identity
from .matching import scan_inputs
from .adaptation import adaptation_plan, source_fps, target_duration
from .layers import IMAGE_SUFFIXES, ImageCache
from .layer_ui import LayerPanel
from .components import resolve_sources
from .preview import Preview
from .pool_controller import PoolController
from .pool_ui import ResourceTable
from .pools import add_videos, merge_copies, bind_pool_layers
from .playback import PlaybackController
from .effects import blur_filter, blur_strength
from .quick_export import prepare_current_export
from .undo import UndoController
from .audio_sources import resolve_voice
from .audio_ui import AudioSourceCard
from .component_nodes import ComponentNodeController
from .voice_effects_preview import VoiceEffectController
from .workspace_navigation import WorkspaceNavigation


class OverlayWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)

    def __init__(self, request, cache, parent=None, *, design_job=None, design_bundle=None, design_registry=None, directory=None):
        super().__init__(parent)
        self.request, self.cache = request, cache
        self.design_job = copy.deepcopy(design_job)
        self.design_bundle, self.design_registry = copy.deepcopy(design_bundle), design_registry
        self.directory, self.cancel = directory, threading.Event()

    def run(self):
        title, body, width, height, settings = self.request
        try:
            settings = dict(settings)
            settings['layers'], _ = resolve_sources(settings['layers'], title, body, job=self.design_job)
            from .design_preview import design_layers
            settings, notices = design_layers(settings, self.design_job, self.design_bundle,
                self.design_registry, width, height, self.directory, self.cancel)
            from .caption_follow_preview import prepare as prepare_follow_preview
            settings,follow_notices=prepare_follow_preview(settings,self.design_job,self.directory,self.cancel)
            notices=list(notices)+follow_notices
            image, info = render_preview_overlay(title, body, width, height, settings, self.cache)
            from .visual_media import needs_plane, preview_planes
            layers, _ = resolve_sources(settings["layers"], title, body)
            planes = preview_planes(title, body, width, height, settings, self.cache) if any(
                needs_plane(layer) and layer["enabled"] and layer["opacity"] for layer in layers) else []
            self.ready.emit({"request": self.request, "image": image, "info": info, "planes": planes,
                             'design_notices': notices})
        except Exception as error:
            self.ready.emit({"request": self.request, "error": str(error), "traceback": traceback.format_exc()})


class Combo(QtWidgets.QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class FontCombo(QtWidgets.QFontComboBox):
    def wheelEvent(self, event):
        event.ignore()


class BatchWorker(QtCore.QThread):
    message = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(str, int)
    jobDone = QtCore.pyqtSignal(str, str)
    result = QtCore.pyqtSignal(object)

    def __init__(self, store, state, ids, parent=None, *, node_bundle=None, node_registry=None, elevenlabs_binding=None):
        super().__init__(parent)
        self.store, self.state, self.ids = store, copy.deepcopy(state), list(ids)
        self.cancel = threading.Event()
        self.node_bundle, self.node_registry = node_bundle, node_registry
        self.elevenlabs_binding = elevenlabs_binding

    def run(self):
        effects = None
        if self.node_bundle:
            from .node_effects import EffectRuntime
            effects = EffectRuntime(self.node_bundle, self.node_registry, self.cancel, self.message.emit, self.message.emit)
        result, self.state = execute_batch(self.store, self.state, self.ids, self.cancel,
            self.progress.emit, self.message.emit, self.jobDone.emit, RENDERERS, node_effects=effects,
            elevenlabs_binding=self.elevenlabs_binding)
        self.result.emit(result)


class PreviewWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)

    def __init__(self, path, configured, parent=None, voice_path="", blur=0, frame_settings=None, cache_directory=None):
        super().__init__(parent)
        self.path, self.configured = path, configured
        self.voice_path = voice_path
        self.blur = blur
        self.frame_settings = frame_settings
        self.cache_directory = cache_directory
        self.cancel = threading.Event()

    def run(self):
        identity = {"path": self.path, "voice_path": self.voice_path, "configured": self.configured, "blur": self.blur}
        if self.frame_settings:
            identity.update(self.frame_settings)
        try:
            if self.cancel.is_set():
                raise Cancelled("背景预览已取消。")
            ffmpeg, ffprobe = resolve_tools(self.configured)
            voice_duration = probe(self.voice_path, ffprobe, audio_only=True)["duration"] if self.voice_path else None
            if not self.path:
                self.ready.emit({**identity, "image": b"", "media": None, "voice_duration": voice_duration})
                return
            from .image_background import probe_background, is_image
            media = probe_background(self.path, ffprobe, self.frame_settings) if is_image(self.path) else probe(self.path, ffprobe)
            crop = None
            if self.frame_settings:
                from .background_framing import prepare_background_settings, framing_filter, preview_size
                width,height = output_size(self.frame_settings,media)
                prepared = prepare_background_settings(self.path, media, self.frame_settings, ffmpeg, self.cache_directory, self.cancel)
                crop = prepared.get("_background_crop")
                frame_filter = framing_filter(width, height, prepared)
                encoded_width, encoded_height = preview_size(width, height, 720, 1280)
            else:
                width,frame_filter = 720,"scale=720:-2"
            from model.MediaColor import sdr_input_filter
            color = sdr_input_filter(media['video'])
            if color:
                frame_filter = color + ',' + frame_filter
            effect = blur_filter({"background_blur":self.blur},width)
            frame_filter += ","+effect if effect else ""
            if self.frame_settings and (encoded_width, encoded_height) != (width, height):
                frame_filter += f",scale={encoded_width}:{encoded_height}:flags=lanczos"
            if self.cancel.is_set():
                raise Cancelled("背景预览已取消。")
            from model.AppDataStorage import data_io
            from .image_background import materialize
            with data_io.TemporaryDirectory(prefix="background-poster-") as temporary:
                source = materialize(self.path, Path(temporary)/"background.png") if media.get("still_image") else self.path
                seek = [] if media.get("still_image") else ["-ss", "0.1"]
                result = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *seek,
                                         "-i", source, "-vf", frame_filter, "-frames:v", "1",
                                         "-f", "image2pipe", "-c:v", "png", "pipe:1"],
                                        capture_output=True, timeout=20,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode or not result.stdout:
                raise ValueError("无法读取预览帧。")
            if self.cancel.is_set():
                raise Cancelled("背景预览已取消。")
            self.ready.emit({**identity, "image": result.stdout, "media": media, "voice_duration": voice_duration, "source_crop": crop})
        except Cancelled:
            self.ready.emit({**identity, "cancelled": True})
        except Exception as error:
            self.ready.emit({**identity, "error": str(error)})


class ImportWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)
    message = QtCore.pyqtSignal(str)

    def __init__(self, paths, targets, parent=None):
        super().__init__(parent)
        self.paths, self.targets = list(paths), copy.deepcopy(targets)
        self.cancel = threading.Event()

    def run(self):
        try:
            self.ready.emit(scan_inputs(self.paths, self.targets, self.cancel, self.message.emit))
        except Exception:
            self.message.emit(traceback.format_exc())
            self.ready.emit({"entries": [], "issues": ["导入异常，请查看程序日志。"], "cancelled": False})


class BatchTextVideoDialog(ComponentNodeController, UndoController, PlaybackController, PoolController, VoiceEffectController, WorkspaceNavigation, QtWidgets.QDialog):
    def __init__(self, context=None, store=None, parent=None):
        from .component_inspector import require_editor_api
        require_editor_api(LayerPanel)
        super().__init__(parent)
        ensure_fonts()
        self.context, self.store = context, store or Store()
        if not callable(getattr(self.store,'sync_render_selection',None)):
            raise RuntimeError('文案视频源码已更新，当前运行仍是旧存储模块；请方便时正常重启后使用新面板，现有任务未修改。')
        self.state = self.store.load()
        from .render_slots import configure
        configure(file_identity(self.store.path), self.state['render_settings']['export_workers'])
        from model.MediaEncoding import normalize_export_settings
        normalize_export_settings(self.state['settings'])
        from .task_naming import naming_rules, refresh_task_names
        loader = getattr(context, "load_config", None)
        if callable(loader):
            # Only the public filename templates; never copy API credentials.
            self.state["settings"]["task_naming_rules"] = naming_rules(loader())
            refresh_task_names(self.state)
        self.worker = self.preview_worker = self.import_worker = None
        self._host_plans = {}  # profile id -> host-owned task handle
        self._render_progress = {}  # profile id -> {job id: percent}
        self._render_stages = {}  # phase is not an encoding percentage
        self._quick_export_ids = {}
        self.text_import_dialog = None
        self.overlay_worker = None
        self._overlay_request = self._overlay_pending = None
        self._image_cache = ImageCache()
        self._pending_layer_paths = []
        self.current_id, self._loading = "", False
        self._quick_export_id = ""
        self._preview_path, self._preview_pending = None, None
        self._media = None
        self._voice_duration = None
        self._background_error = ""
        self._framing_status = ""
        self._overlay_status = ""
        self._last_preview_warning = ""
        self._force_preview_pending = False
        self._design_cache = None
        self.setWindowTitle("批量文案视频 · 静态文字版")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowMinMaxButtonsHint)
        self.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self.resize(1250, 850)
        self.setAcceptDrops(True)
        layout = QtWidgets.QVBoxLayout(self)
        self.scheme = Combo()
        for key, label in SCHEMES.items():
            self.scheme.addItem(label, key)
        self.scheme.setCurrentIndex(max(0, self.scheme.findData(self.state["settings"]["scheme"])))
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.tabs = QtWidgets.QTabWidget()
        self.jobs = ResourceTable(["任务／成品", "状态", "进度", "耗时"])
        self.jobs.setColumnWidth(1, 85)
        self.jobs.setColumnWidth(2, 55)
        self.jobs.setColumnWidth(3, 76)
        self.jobs.itemChanged.connect(self._render_check_changed)
        self.jobs.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.jobs.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 80), (2, 50)):
            self.jobs.setColumnWidth(column, width)
        from .row_progress import install_row_progress
        install_row_progress(self.jobs)
        self.jobs.currentCellChanged.connect(self._selection_changed)
        self.jobs.cellDoubleClicked.connect(self.open_result)
        music_page = QtWidgets.QWidget()
        music_layout = QtWidgets.QVBoxLayout(music_page)
        note = QtWidgets.QLabel("按列表分配音乐并记住播放位置。\n原生任务编码前固定分配，失败重试沿用；箭头表示下一份可分配位置。")
        note.setWordWrap(True)
        music_layout.addWidget(note)
        self.music = ResourceTable(["音乐", "续用位置", "上次使用"])
        self.music.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.music.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.music.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.music.setColumnWidth(1, 75)
        self.music.setColumnWidth(2, 115)
        music_layout.addWidget(self.music)
        self.init_resources(music_page)
        self.tabs.setMinimumWidth(250)
        self.left_column = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.resource_pane = QtWidgets.QWidget()
        self.resource_pane_layout = QtWidgets.QVBoxLayout(self.resource_pane)
        self.resource_pane_layout.setContentsMargins(0, 0, 0, 0)
        self.resource_pane_layout.addWidget(self.tabs)
        self.left_column.addWidget(self.resource_pane)
        self.splitter.addWidget(self.left_column)
        preview_page = QtWidgets.QWidget()
        preview_layout = QtWidgets.QVBoxLayout(preview_page)
        self.preview = Preview()
        self.init_playback(preview_layout)
        self.preview_status = QtWidgets.QLabel("预览使用导出时相同的排版，视频背景暂显示首帧。")
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_status)
        self.duration_status = QtWidgets.QLabel("成品长度按“时长适配”设置；启用人声时跟随人声。")
        self.duration_status.setWordWrap(True)
        preview_layout.addWidget(self.duration_status)
        self.refresh_preview_button = QtWidgets.QPushButton("刷新预览")
        self.refresh_preview_button.setToolTip("保留动画快览时间，重新读取当前视频背景／已保存节点，并更新图层；不生成视频。")
        self.refresh_preview_button.clicked.connect(self.refresh_preview)
        preview_tools = QtWidgets.QHBoxLayout()
        self.show_bounds_checkbox = QtWidgets.QCheckBox("显示框边界")
        self.show_bounds_checkbox.setChecked(self.state["settings"]["show_layer_bounds"])
        self.show_bounds_checkbox.setToolTip("默认显示框的范围和实际宽高；辅助线只用于编辑，不会出现在导出视频中。")
        preview_tools.addWidget(self.show_bounds_checkbox)
        self.guide_checkbox = QtWidgets.QCheckBox('辅助线')
        self.guide_checkbox.setChecked(True)
        self.guide_checkbox.setToolTip('拖动时显示画布及其他控件的对齐线，仅用于编辑，不会导出。')
        self.snap_checkbox = QtWidgets.QCheckBox('吸附')
        self.snap_checkbox.setChecked(True)
        self.snap_checkbox.setToolTip('拖动、缩放时吸附到画布中心／边缘及其他控件；按住Alt临时关闭。')
        preview_tools.addWidget(self.guide_checkbox)
        preview_tools.addWidget(self.snap_checkbox)
        preview_tools.addWidget(self.refresh_preview_button,1)
        preview_layout.addLayout(preview_tools)
        self.splitter.addWidget(preview_page)
        editor_page = QtWidgets.QGroupBox('当前任务文案')
        self.task_text_panel = editor_page
        editor_layout = QtWidgets.QVBoxLayout(editor_page)
        self.name = QtWidgets.QLineEdit()
        self.title = QtWidgets.QPlainTextEdit()
        self.title.setAcceptDrops(False)
        self.title.setMaximumHeight(65)
        self.body = QtWidgets.QPlainTextEdit()
        self.body.setAcceptDrops(False)
        self.body.setMinimumHeight(65)
        self.body.setPlaceholderText("填写正文，换行与段落会保留。")
        for label, widget in (("输出名称", self.name), ("标题", self.title), ("正文", self.body)):
            editor_layout.addWidget(QtWidgets.QLabel(label))
            editor_layout.addWidget(widget)
        voice_row = QtWidgets.QHBoxLayout()
        self.voice_label = QtWidgets.QLabel("当前任务人声：无")
        self.voice_label.setWordWrap(True)
        voice_row.addWidget(self.voice_label, 1)
        replace_voice = QtWidgets.QPushButton("更换…")
        replace_voice.clicked.connect(self.replace_voice)
        voice_row.addWidget(replace_voice)
        editor_layout.addLayout(voice_row)
        self.left_column.addWidget(editor_page)
        self.left_column.setSizes([440,260])
        settings_widget = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(settings_widget)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.controls = {}
        form.addRow("方案",self.scheme)
        self._combo(form, "aspect", "画布", [("竖屏 1080×1920", "portrait"), ("横屏 1920×1080", "landscape"), ("原素材规格", "source")])
        self.controls["aspect"].setToolTip("没有底层背景时，请选择需要的竖屏或横屏；“原素材规格”在无背景时使用竖屏1080×1920。")
        self._combo(form, "fit", "画面", [("完整显示（不裁切）", "contain"), ("铺满导出画布（会裁边）", "cover")])
        self.controls["fit"].setToolTip("铺满的是所选导出画布，不是整个软件窗口。完整显示可能留黑边；铺满会裁掉超出画布的内容。")
        crop_check = QtWidgets.QCheckBox("自动去除素材自带黑边")
        crop_check.setChecked(self.state["settings"].get("auto_crop_black_bars", True))
        crop_check.setEnabled(self.controls["fit"].currentData() == "cover")
        crop_check.setToolTip("仅在铺满模式生效。检查视频多处，只裁稳定的两侧黑边；不修改原文件。暗场景或艺术黑框可关闭。预览和导出使用相同裁边结果。")
        self.controls["auto_crop_black_bars"] = crop_check
        form.addRow(crop_check)
        self.controls["fit"].currentIndexChanged.connect(lambda *_: crop_check.setEnabled(self.controls["fit"].currentData() == "cover"))
        self._combo(form, "background_pool_id", "背景池", [("按任务已搭配背景", ""), ("无底层背景（使用控件合成）", "none"), ("默认背景池（图片／视频）", "builtin:background")])
        self.controls["background_pool_id"].setToolTip("全项目的底层背景来源。指定池后按队列顺序取用；按任务时使用各任务已搭配的背景。可选无底层背景，也允许背景池为空，直接使用图片等控件合成；没有控件覆盖的区域为黑色。动画在对应视觉控件中调整。")
        for key, label, low, high in (("darkness", "背景压暗 %", 0, 75),):
            form.addRow(label, self._spin(key, low, high))
        form.addRow("背景模糊",self._spin("background_blur",0,40,floating=True))
        self.controls["background_blur"].setSingleStep(1)
        self.controls["background_blur"].setToolTip("0关闭；建议先试4–10。仅模糊背景视频，文字和叠加图片仍清晰。数值按1080宽画布缩放，播放预览和导出都会生效。")
        self.tint_preset = Combo()
        for name,color,strength in (("手动调整",None,None),("无叠色","#000000",0),
                ("暖金","#c88236",12),("冷蓝","#497dcc",14),("复古棕","#806447",20),("柔和粉","#d98baf",12)):
            self.tint_preset.addItem(name,(color,strength))
        self.tint_preset.activated.connect(self._apply_tint_preset)
        form.addRow("色彩预设",self.tint_preset)
        self.tint_color = QtWidgets.QPushButton(self.state["settings"]["background_tint_color"])
        self.tint_color.clicked.connect(self._pick_tint_color)
        self.controls["background_tint_color"] = self.tint_color
        form.addRow("背景叠色",self.tint_color)
        form.addRow("叠色强度 %",self._spin("background_tint_strength",0,75))
        for index in range(1,self.tint_preset.count()):
            color,strength = self.tint_preset.itemData(index)
            if color == self.tint_color.text() and strength == self.controls["background_tint_strength"].value():
                self.tint_preset.setCurrentIndex(index)
                break
        self.tint_color.setToolTip("仅给视频背景叠加颜色，不染色文字和图片图层。强度0为关闭；不是专业LUT调色。")
        render_widget = QtWidgets.QWidget()
        render_form = QtWidgets.QFormLayout(render_widget)
        render_form.setRowWrapPolicy(render_form.RowWrapPolicy.WrapLongRows)
        render_form.setFieldGrowthPolicy(render_form.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        check = QtWidgets.QCheckBox("导出时允许覆盖同名文件")
        check.setToolTip('所有项目共用；仅覆盖目标成品文件，原始素材和抠像管理文件继续保护。')
        check.setChecked(self.state["settings"]["overwrite"])
        self.controls["overwrite"] = check
        render_form.addRow(check)
        note = QtWidgets.QLabel("点击画布或左侧图层选择控件，在右侧“控件属性”中修改字体、颜色、位置和动画。")
        note.setWordWrap(True)
        form.addRow(note)
        component_button = QtWidgets.QPushButton("设置文字组件…")
        component_button.clicked.connect(self.edit_text_components)
        form.addRow(component_button)
        self.ffmpeg = QtWidgets.QLineEdit(self.state["settings"]["ffmpeg_path"])
        self.ffmpeg.setPlaceholderText("留空使用随程序提供的标准FFmpeg")
        from model.MediaEncoding import ENCODERS, EXPORT_QUALITIES, MAX_EXPORT_WORKERS
        self._combo(render_form, 'video_encoder', '视频编码', [(label, key) for key, label in ENCODERS])
        self._combo(render_form, 'export_quality', '导出质量', [(label, key) for key, label in EXPORT_QUALITIES])
        self.controls['export_quality'].setToolTip('所有项目共用。高：画质优先；中：画质与体积均衡；低：体积优先。分辨率与帧率由项目决定。')
        workers_spin = self._spin('export_workers', 1, MAX_EXPORT_WORKERS)
        workers_spin.setSuffix(' 路')
        workers_spin.setToolTip('1–64路；所有项目共享同一上限。实际并发取决于任务准备与硬件处理速度。已有数值保持不变，新批次使用新设置。')
        render_form.addRow('全局并发上限', workers_spin)
        fallback = QtWidgets.QCheckBox('显卡失败／编码停滞时用CPU重试')
        fallback.setChecked(self.state['settings']['encoder_fallback'])
        self.controls['encoder_fallback'] = fallback
        render_form.addRow(fallback)
        render_form.addRow('FFmpeg（可选）', self.ffmpeg)
        encode_note = QtWidgets.QLabel('所有项目共用；新提交的任务采用当前设置。显卡用于最终H.264编码，合成和字幕仍按原画质处理。')
        encode_note.setWordWrap(True)
        render_form.addRow(encode_note)
        from .property_sections import PropertySections
        self.project_sections = PropertySections()
        self.project_sections.add_section('canvas','画布与背景',settings_widget,expanded=True)
        voice_widget = QtWidgets.QWidget()
        voice_form = QtWidgets.QFormLayout(voice_widget)
        voice_check = QtWidgets.QCheckBox("使用已分配人声，成品长度跟随人声")
        voice_check.setChecked(self.state["settings"]["use_voice"])
        self.controls["use_voice"] = voice_check
        self._combo(voice_form, "unvoiced_duration_mode", "无人声时长度",
                    [("视频原长／图片用下方时长", "source"), ("统一固定时长", "fixed")])
        duration_spin = self._spin("unvoiced_duration_seconds", .1, 86400, floating=True)
        duration_spin.setSingleStep(1); duration_spin.setSuffix(" 秒")
        voice_form.addRow("固定／图片时长", duration_spin)
        def update_duration_controls(*_):
            unvoiced = not voice_check.isChecked()
            self.controls["unvoiced_duration_mode"].setEnabled(unvoiced)
            duration_spin.setEnabled(unvoiced)
        voice_check.toggled.connect(update_duration_controls)
        self.controls["unvoiced_duration_mode"].currentIndexChanged.connect(update_duration_controls)
        update_duration_controls()
        self._combo(voice_form, "duration_strategy", "背景适配", [("循环播放", "loop"), ("放慢填满", "slow"), ("减速＋循环", "hybrid")])
        self._combo(voice_form, "loop_transition", "循环交界", [("直接衔接", "none"), ("叠化", "fade")])
        voice_form.addRow("叠化时长 秒", self._spin("transition_seconds", 0.05, 3, floating=True))
        voice_form.addRow("混合最低速度", self._spin("min_speed", 0.1, 1, floating=True))
        self._combo(voice_form, "slow_interpolation", "减速补帧", [("帧采样（重复帧，快）", "repeat"), ("帧混合", "blend"), ("运动补偿（较慢）", "motion")])
        self.controls["slow_interpolation"].setToolTip("运动补偿使用FFmpeg运动估计插帧，并不等同于达芬奇光流算法。运动较大时可能产生扭曲，建议先生成一个样片。")
        note = QtWidgets.QLabel("图片背景始终使用上方时长；视频选择固定时长后，长素材截短、短素材按背景适配补足。\n"
            "启用人声后仍跟随人声，不用固定时长截断朗读。人声不足视频长度时截短背景；已有任务人声时忽略背景原声。\n"
            "保留背景原声时会跟随背景变速／循环；原声不参与视频循环交界的叠化。")
        note.setWordWrap(True)
        voice_form.addRow(note)
        mix_widget = QtWidgets.QWidget()
        mix_form = QtWidgets.QFormLayout(mix_widget)
        self.controls["music_pool_id"] = Combo()
        self.sync_custom_pools()
        for key,label,low,high in (("silence_db","静音阈值 dB",-80,-20),):
            mix_form.addRow(label,self._spin(key,low,high))
        for key,label,low,high in (("silence_seconds","最短静音 秒",.1,5),
                                   ("fade_seconds","音乐淡入淡出 秒",0,5)):
            mix_form.addRow(label,self._spin(key,low,high,floating=True))
        for key,label in (("skip_silence","自动跳过音乐静音"),("keep_audio","保留视频原声")):
            check = QtWidgets.QCheckBox(label)
            check.setChecked(self.state["settings"][key])
            self.controls[key] = check
            mix_form.addRow(check)
        self.controls["silence_db"].setToolTip("默认-50dB，仅跳过明显无声部分。修改后该音乐从新的有效内容起点计位置。")
        for key, label, low, high in (("voice_music_volume", "有人声时音乐 %", 0, 100),
                                     ("eq_frequency", "音乐EQ中心 Hz", 300, 6000), ("eq_gain", "音乐EQ衰减 dB", -24, 0),
                                     ("duck_threshold_db", "压低触发 dB", -60, -6), ("duck_ratio", "压低比例", 1, 20),
                                     ("duck_attack_ms", "响应 ms", 1, 500), ("duck_release_ms", "恢复 ms", 50, 3000)):
            mix_form.addRow(label, self._spin(key, low, high))
        mix_form.addRow("EQ宽度 八度", self._spin("eq_width", 0.2, 4, floating=True))
        for key, label in (("voice_eq", "添加人声时，音乐自动开启人声频段EQ"), ("voice_duck", "说话时自动压低背景音乐")):
            check = QtWidgets.QCheckBox(label)
            check.setChecked(self.state["settings"][key])
            self.controls[key] = check
            mix_form.addRow(check)
        audio_widget = QtWidgets.QWidget()
        audio_layout = QtWidgets.QVBoxLayout(audio_widget)
        audio_layout.setContentsMargins(5, 5, 5, 5)
        music_enabled = QtWidgets.QCheckBox("启用")
        music_enabled.setChecked(self.state["settings"]["music_enabled"])
        self.controls["music_enabled"] = music_enabled
        self.audio_cards = {
            "voice": AudioSourceCard("voice", self.state, enabled=voice_check,
                                      volume=self._spin("voice_volume", 0, 200)),
            "music": AudioSourceCard("music", self.state, enabled=music_enabled,
                                      volume=self._spin("music_volume", 0, 200), pool=self.controls["music_pool_id"]),
        }
        self.controls["voice_pool_id"] = self.audio_cards["voice"].pool
        self.controls["voice_pool_mode"] = self.audio_cards["voice"].mode
        voice_card = self.audio_cards["voice"]
        self.init_voice_effects(voice_card)
        voice_card.menu_button.show()
        for label, scope in (("当前条目跟随控件", "current"), ("选中队列跟随控件", "selected"),
                             ("全部队列跟随控件", "all")):
            voice_card.menu.addAction(label, lambda _checked=False, s=scope: self.follow_voice_card(s))
        for card in self.audio_cards.values():
            audio_layout.addWidget(card)
        note = QtWidgets.QLabel("逐行指定优先；人声卡片 ... 可恢复跟随控件。")
        note.setToolTip("直接选择自建池即可绑定。预览与导出共用来源；音频控件不占画布。")
        note.setWordWrap(True)
        audio_layout.addWidget(note)
        audio_layout.addStretch()
        self.mix_toggle = QtWidgets.QToolButton()
        self.mix_toggle.setText("混音细节")
        self.mix_toggle.setCheckable(True)
        self.mix_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.mix_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        mix_widget.setVisible(False)
        self.mix_toggle.toggled.connect(mix_widget.setVisible)
        self.mix_toggle.toggled.connect(lambda opened: self.mix_toggle.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if opened else QtCore.Qt.ArrowType.RightArrow))
        audio_layout.insertWidget(audio_layout.count()-1, self.mix_toggle)
        audio_layout.insertWidget(audio_layout.count()-1, mix_widget)
        for key, widget, label in (('audio',audio_widget,'音频设置'),('duration',voice_widget,'时长适配')):
            self.project_sections.add_section(key,label,widget)
        self.layer_panel = LayerPanel(self.state["settings"]["layers"], self.store.directory,settings=self.state["settings"], services=self.context)
        self.layer_panel.set_pool_state(self.state)
        self.layer_scroll = QtWidgets.QScrollArea()
        self.layer_scroll.setWidgetResizable(True)
        self.layer_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.layer_scroll.setWidget(self.layer_panel)
        catalog_scroll = QtWidgets.QScrollArea()
        catalog_scroll.setWidgetResizable(True)
        catalog_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        catalog_scroll.setWidget(self.layer_panel.catalog)
        self.layer_tab_index = self.tabs.addTab(catalog_scroll,'图层')
        self.right_tabs = QtWidgets.QTabWidget()
        self.right_tabs.addTab(self.layer_scroll,'控件属性')
        project_scroll = QtWidgets.QScrollArea()
        project_scroll.setWidgetResizable(True)
        project_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        project_scroll.setWidget(self.project_sections)
        self.right_tabs.addTab(project_scroll,'项目设置')
        from .global_queue_ui import GlobalRenderQueuePanel
        self.render_queue_panel = GlobalRenderQueuePanel(self, render_widget)
        self.right_tabs.addTab(self.render_queue_panel, '渲染队列')
        self.right_tabs.setMinimumWidth(365)
        self.splitter.addWidget(self.right_tabs)
        self.splitter.setSizes([260, 500, 440])
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter, 1)
        output_row = QtWidgets.QHBoxLayout()
        self.output = QtWidgets.QLineEdit(self.state["settings"]["output_dir"])
        output_row.addWidget(QtWidgets.QLabel("默认导出文件夹"))
        self.output.setPlaceholderText('任务指定目录优先；普通独立视频使用此目录')
        self.output.setToolTip("表格/人声任务始终导出到各自任务目录，不使用这里的路径。普通独立视频留空则导出到源视频目录。")
        output_row.addWidget(self.output, 1)
        browse = QtWidgets.QPushButton("选择…")
        browse.clicked.connect(self.choose_output)
        output_row.addWidget(browse)
        self.open_directory_button = QtWidgets.QPushButton("打开目录")
        self.open_directory_button.setAutoDefault(False)
        self.open_directory_button.setToolTip("打开当前预览对应的成品目录；表格/人声任务优先使用实际任务目录，不打开预览缓存。")
        self.open_directory_button.clicked.connect(self.open_output_directory)
        output_row.addWidget(self.open_directory_button)
        layout.addLayout(output_row)
        footer = QtWidgets.QHBoxLayout()
        footer.addWidget(QtWidgets.QLabel('批量导出：在渲染队列勾选任务，再点击“开始全部计划”。'))
        self.global_queue_button = QtWidgets.QPushButton("渲染队列")
        self.stop_button = QtWidgets.QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.cancel_work)
        self.global_queue_button.clicked.connect(self.open_global_queue)
        footer.addWidget(self.stop_button)
        footer.addWidget(self.global_queue_button)
        self.progress_bar = QtWidgets.QProgressBar()
        footer.addWidget(self.progress_bar, 1)
        layout.addLayout(footer)
        self.status = QtWidgets.QLabel("普通文字自动适配字号；放不下就提示。滚动文字按框范围裁切。关闭窗口会保留列表与音乐进度。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.init_menus(layout)
        self.init_workspace_navigation(layout)
        self.edit_timer = QtCore.QTimer(self)
        self.edit_timer.setSingleShot(True)
        self.edit_timer.setInterval(600)
        self.edit_timer.timeout.connect(self._edit_changed)
        self.render_settings_timer = QtCore.QTimer(self)
        self.render_settings_timer.setSingleShot(True)
        self.render_settings_timer.setInterval(400)
        self.render_settings_timer.timeout.connect(self.save_render_settings)
        self.geometry_preview_timer = QtCore.QTimer(self)
        self.geometry_preview_timer.setSingleShot(True)
        self.geometry_preview_timer.setInterval(80)
        self.geometry_preview_timer.timeout.connect(self.update_preview)
        self.animation_preview_timer = QtCore.QTimer(self)
        self.animation_preview_timer.setSingleShot(True)
        self.animation_preview_timer.setInterval(80)
        self.animation_preview_timer.timeout.connect(self.update_preview)
        self.title.textChanged.connect(self.schedule_edit)
        self.body.textChanged.connect(self.schedule_edit)
        self.name.textChanged.connect(self.schedule_edit)
        self.output.textChanged.connect(self.schedule_edit)
        self.ffmpeg.textChanged.connect(self.schedule_render_settings)
        self.scheme.currentIndexChanged.connect(self.schedule_edit)
        self.layer_panel.changed.connect(self.schedule_edit)
        self.layer_panel.convertRequested.connect(self.convert_component_to_nodes)
        self.layer_panel.polygonRequested.connect(self._edit_wrap_polygons)
        self.layer_panel.changed.connect(self._sync_preview_layers)
        self.layer_panel.selectionChanged.connect(self._sync_preview_layers)
        self.layer_panel.background_button.clicked.connect(lambda: self.right_tabs.setCurrentIndex(0))
        self.preview.layerSelected.connect(self.layer_panel.select_layer_id)
        self.preview.geometryEdited.connect(self._preview_geometry_edited)
        self.preview.geometryCommitted.connect(self._finish_geometry_edit)
        self.guide_checkbox.toggled.connect(self.preview.set_guides_visible)
        self.snap_checkbox.toggled.connect(self.preview.set_snapping_enabled)
        self.preview.guidesToggled.connect(self.guide_checkbox.setChecked)
        self.preview.snappingToggled.connect(self.snap_checkbox.setChecked)
        self.preview.orderRequested.connect(self._preview_order_requested)
        self.preview.layerSelected.connect(lambda _identifier:self.right_tabs.setCurrentIndex(0))
        self.show_bounds_checkbox.toggled.connect(self._sync_preview_layers)
        self.show_bounds_checkbox.toggled.connect(self.schedule_edit)
        self.layer_panel.busyChanged.connect(lambda _busy: self._set_work_controls(self.is_batch_busy()))
        self.layer_panel.message.connect(self._log)
        for key, control in self.controls.items():
            from .render_settings import KEYS
            changed = self.schedule_render_settings if key in KEYS else self.schedule_edit
            if isinstance(control, QtWidgets.QAbstractSpinBox):
                control.valueChanged.connect(changed)
            elif isinstance(control, QtWidgets.QCheckBox):
                control.toggled.connect(changed)
            elif isinstance(control, QtWidgets.QComboBox):
                control.currentIndexChanged.connect(changed)
        for key in ("aspect", "fit"):
            self.controls[key].currentIndexChanged.connect(self._frame_geometry_changed)
        crop_check.toggled.connect(self._frame_geometry_changed)
        self._sync_preview_layers()
        self.refresh_tables()
        if self.jobs.rowCount():
            self.right_tabs.setCurrentWidget(self.render_queue_panel)
            first = next((row for row in range(self.jobs.rowCount()) if not self.jobs.isRowHidden(row)), 0)
            self.jobs.setCurrentCell(first, 0)
        else:
            self.clear_editor()
        self.apply_profile_title()
        self.init_editor_history()

    def _combo(self, form, key, label, choices):
        combo = Combo()
        for text, value in choices:
            combo.addItem(text, value)
        combo.setCurrentIndex(max(0, combo.findData(self.state["settings"][key])))
        self.controls[key] = combo
        form.addRow(label, combo)

    def edit_text_components(self):
        self.tabs.setCurrentIndex(self.layer_tab_index)
        current = self.layer_panel.current()
        if not current or current["kind"] not in {"title", "body", "text"}:
            self.layer_panel.rebuild("title")
        self.right_tabs.setCurrentIndex(0)
        self.layer_panel.reveal_property('font')

    def _apply_tint_preset(self,index):
        self.tint_preset.setCurrentIndex(index)
        color,strength = self.tint_preset.itemData(index)
        if color is not None:
            self.tint_color.setText(color)
            self.controls["background_tint_strength"].setValue(strength)
            self.schedule_edit()

    def _pick_tint_color(self):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.tint_color.text()),self,"背景叠色")
        if color.isValid():
            self.tint_color.setText(color.name())
            self.tint_preset.setCurrentIndex(0)
            self.schedule_edit()

    def import_sheet_text(self):
        if self.is_batch_busy():
            return
        try:
            path = self.context.current_task_table_path() if self.context else None
            if path is None:
                self.status.setText("请先在主程序加载任务，再读取当前任务文案。")
                return False
            if not path.is_file():
                raise FileNotFoundError("当前项目的任务登记表不存在："+str(path))
            return self.import_task_copy(path=path)
        except Exception as error:
            self._log("读取当前任务文案失败："+traceback.format_exc())
            self.status.setText("读取当前任务文案失败："+str(error))
            return False

    def import_task_copy(self,path=None,records=None,selection_keys=None):
        if self.is_batch_busy():
            self.status.setText("已有生成或导入操作，请稍后再试。")
            return False
        from .task_text_ui import TaskTextImportDialog
        self.edit_timer.stop()
        if not self.persist():
            return False
        dialog = TaskTextImportDialog(path=path,records=records,parent=self,selection_keys=selection_keys)
        self.text_import_dialog = dialog
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            if dialog.toolTip():
                self._log("表格文案导入失败："+dialog.toolTip())
            dialog.deleteLater()
            self.text_import_dialog = None
            return False
        try:
            from .task_naming import refresh_task_names
            window = getattr(self.context, "parent_widget", None)
            project = getattr(window, "loaded_project_dir", None)
            date_edit = getattr(window, "dateEdit", None)
            for entry in dialog.entries:
                if project and date_edit is not None and Path(entry["task_dir"]).parent.parent.resolve() == Path(project).resolve():
                    entry["task_year"] = date_edit.date().year()
            added,updated = merge_copies(self.state,dialog.entries,sheet=True)
            refresh_task_names(self.state)
            voices = sum(bool(entry.get("voice_path")) for entry in dialog.entries)
            subtitles = sum(bool(entry.get("task_subtitle_path")) for entry in dialog.entries)
            ambiguous = sum(bool(entry.get("match_error")) for entry in dialog.entries)
            if self.controls["voice_pool_mode"].currentData() == "task":
                self.controls["use_voice"].setChecked(bool(voices))
            current = next((job for job in self.state["copy_pool"] if job["id"] == self.current_copy_id),None)
            if current:
                self._show_job({**current, "path":self.copy_preview_path(current)})
            if not self.persist():
                return False
            self.refresh_tables()
            self.tabs.setCurrentWidget(self.resources)
            self.resources.setCurrentWidget(self.copy_pool)
            if self.copy_pool.rowCount():
                self.copy_pool.setCurrentCell(0,0)
                self.select_copy(0)
            self.status.setText(f"文案池新增{added}条、更新{updated}条；附带人声{voices}条、字幕{subtitles}条。"
                                "在“批量→搭配资源”中与视频配对；字幕控件可直接使用任务时间戳，成品仍放入任务目录。"
                                + (f" 有{ambiguous}条人声无法唯一匹配，未自动选择，请手动指定。" if ambiguous else ""))
            return True
        except Exception:
            self._log("表格文案导入失败："+traceback.format_exc())
            return False
        finally:
            dialog.deleteLater()
            self.text_import_dialog = None

    def _spin(self, key, low, high, floating=False):
        spin = QtWidgets.QDoubleSpinBox() if floating else QtWidgets.QSpinBox()
        spin.setRange(low, high)
        spin.setValue(self.state["settings"][key])
        if floating:
            spin.setDecimals(2)
            spin.setSingleStep(0.05)
        self.controls[key] = spin
        return spin

    def read_settings(self):
        settings = dict(self.state["settings"])
        for key, control in self.controls.items():
            if isinstance(control, QtWidgets.QAbstractSpinBox):
                settings[key] = control.value()
            elif isinstance(control, QtWidgets.QCheckBox):
                settings[key] = control.isChecked()
            elif isinstance(control, QtWidgets.QFontComboBox):
                settings[key] = control.currentFont().family()
            elif isinstance(control, QtWidgets.QComboBox):
                settings[key] = control.currentData()
            else:
                settings[key] = control.text()
        settings.update(output_dir=self.output.text().strip(), ffmpeg_path=self.ffmpeg.text().strip(), scheme=self.scheme.currentData(),
                        background_visual=self.layer_panel.background_values(),
                        layers=bind_pool_layers(self.layer_panel.values(),self.state),geometry_units=self.layer_panel.units.currentData(),
                        show_layer_bounds=self.show_bounds_checkbox.isChecked())
        return settings

    def _edit_wrap_polygons(self, identifier):
        from .polygon_editor import edit_polygons
        edit_polygons(self, identifier)

    def _sync_preview_layers(self,*_args):
        values = self.layer_panel.values()
        bound = bind_pool_layers(values,self.state)
        if bound != values:
            blocker = QtCore.QSignalBlocker(self.layer_panel)
            self.layer_panel.set_layers(bound)
            del blocker
        current = self.layer_panel.current()
        self.preview.show_bounds = self.show_bounds_checkbox.isChecked()
        self.preview.set_layers(self.layer_panel.values(),current["id"] if current else "")
        if getattr(self, 'pool_preview', None) is not None:
            self._sync_pool_preview(bound, current)

    def _sync_pool_preview(self, layers, current):
        source = self.layer_panel.content_editor if current and current.get('kind') == 'image' else None
        self.pool_preview.attach_to_source(source)
        self.pool_preview.sync(layers, current['id'] if current else '', scope=self.state.get('active_profile', ''))

    def _preview_geometry_edited(self,identifier,values):
        if not self._loading and not self.is_batch_busy() and self.layer_panel.set_geometry(identifier,values):
            if not self.geometry_preview_timer.isActive():
                self.geometry_preview_timer.start()

    def _preview_order_requested(self,identifier,direction):
        if self._loading or self.is_batch_busy(): return
        self.layer_panel.select_layer_id(identifier)
        self.layer_panel.move_layer(direction)
        self._finish_geometry_edit()

    def _finish_geometry_edit(self):
        self.geometry_preview_timer.stop()
        self.edit_timer.stop()
        if not self._loading and not self.is_batch_busy():
            self.persist()
            self.update_preview()

    def _commit_edit(self):
        key, identifier = ("copy_pool", self.current_copy_id) if self.current_copy_id else ("jobs", self.current_id)
        job = next((item for item in self.state[key] if item["id"] == identifier), None)
        if job:
            changed = (job.get("title", ""), job.get("body", ""), job.get("name", "")) != (
                self.title.toPlainText(), self.body.toPlainText(), self.name.text())
            if job.get("task_dir") and job.get("name") != self.name.text():
                job["task_filename_auto"] = False
            job.update(title=self.title.toPlainText(), body=self.body.toPlainText(), name=self.name.text())
            if changed and key == "jobs":
                job.update(status="待生成",error="")
            if changed and key == "copy_pool" and not job.get("task_id"):
                job["label"] = job["name"] or "未命名文案"
        self.state["settings"] = self.read_settings()
        self.capture_editor_change()

    def schedule_edit(self, *_args):
        if not self._loading and not self.is_batch_busy():
            self.mark_editor_pending()
            self.invalidate_playback()
            self.edit_timer.start()

    def schedule_render_settings(self, *_args):
        if not self._loading:
            self.render_queue_panel.refresh_settings_summary()
            self.render_settings_timer.start()

    def save_render_settings(self):
        self.render_settings_timer.stop()
        try:
            from .render_settings import KEYS
            values = {}
            for key in KEYS:
                control = self.ffmpeg if key == 'ffmpeg_path' else self.controls[key]
                values[key] = (control.currentData() if isinstance(control, QtWidgets.QComboBox) else
                    control.isChecked() if isinstance(control, QtWidgets.QCheckBox) else
                    control.value() if isinstance(control, QtWidgets.QAbstractSpinBox) else control.text().strip())
            saved = self.store.save_render_settings(values, expected=self.state.get('render_settings'))
            self.state['render_settings'] = saved
            self.state['settings'].update(copy.deepcopy(saved))
            from .render_slots import configure
            configure(file_identity(self.store.path), saved['export_workers'])
            return True
        except Exception as error:
            self._log('保存全局渲染设置失败：' + str(error))
            self.status.setText('渲染选项未保存：' + str(error))
            return False

    def _edit_changed(self):
        self.persist()
        self.refresh_pools()
        self.update_preview()
        job = self.active_entry()
        if job:
            self.request_background(job["path"])

    def _frame_geometry_changed(self, *_):
        if not self._loading and not self.is_batch_busy():
            self.update_preview()

    def persist(self):
        if self.is_batch_busy():
            return False
        try:
            self._commit_edit()
            self.state = self.store.save(self.state, self.state["revision"])
            self.render_settings_timer.stop()
            from .render_slots import configure
            configure(file_identity(self.store.path), self.state['render_settings']['export_workers'])
            return True
        except Exception as error:
            self.status.setText("保存失败：" + str(error))
            self._log("保存失败：" + traceback.format_exc())
            return False

    def _selection_changed(self, row, *_args):
        if self._loading or self.jobs.read_only:
            return
        self.edit_timer.stop()
        self._commit_edit()
        self.current_copy_id = ""
        if row < 0:
            self.current_id = ""
            self.clear_editor()
            return
        self.current_id = self.jobs.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        job = next(item for item in self.state["jobs"] if item["id"] == self.current_id)
        self._show_job(job)

    def _show_job(self, job):
        self._loading = True
        for widget in (self.title,self.body,self.name):
            widget.setEnabled(True)
        self.title.setPlainText(job["title"])
        self.body.setPlainText(job["body"])
        self.name.setText(job["name"])
        self.name.setToolTip("任务成品按主程序整理规则命名，保存到本任务目录。手动改名后使用自定义名称，可能不再被整理规则识别。" if job.get("task_dir") else "自定义成品文件名。")
        try:
            voice = self.active_voice(job)
            displayed = voice or (job.get("voice_path", "") if not self.controls["use_voice"].isChecked() else "")
            self.voice_label.setText("当前人声：" + (Path(displayed).name if displayed else "无") +
                                     ("（未启用）" if displayed and not self.controls["use_voice"].isChecked() else ""))
            self.voice_label.setToolTip(displayed)
        except (ValueError, OSError) as error:
            self.voice_label.setText("人声来源需调整：" + str(error))
        self._loading = False
        self.request_background(job["path"])
        self.update_preview()
        self.refresh_editor_baseline()

    def _pool_preview_changed(self):
        if self._loading or self.is_batch_busy(): return
        self.invalidate_playback()
        self.update_preview()

    def update_preview(self):
        if self.is_batch_busy():
            return
        try:
            settings = self.read_settings()
            job = self.active_entry()
            if job.get('render_assignment'):
                from .render_assignment import apply as apply_assignment
                job, bound_state = apply_assignment(job, {**self.state, 'settings': settings})
                settings = bound_state['settings']
            if getattr(self, 'pool_preview', None) is not None:
                current = self.layer_panel.current()
                self._sync_pool_preview(settings['layers'], current)
                settings = self.pool_preview.apply(settings)
            from .text_colors import job_color_scope
            settings["_color_scope"] = job_color_scope(job)
            voice = resolve_voice(job, {**self.state, "settings": settings})
            from .preview_timeline import editor_duration
            from .image_background import is_image, duration as image_duration, scale_at
            still = is_image(job.get("path", ""))
            settings['_still_background'] = still
            empty = not job.get("path")
            source_duration = image_duration(settings) if still or empty else self._media.get("duration") if self._media else None
            duration = editor_duration(settings, source_duration,
                                       self._voice_duration, has_voice=bool(voice))
            self.animation_timeline.set_duration(duration)
            timing = self.animation_timeline.snapshot()
            if timing is not None:
                settings['_editor_timing'] = timing
                settings['_preview_voice'] = voice
            displayed = voice or (job.get("voice_path", "") if not settings["use_voice"] else "")
            self.voice_label.setText("当前人声：" + (Path(displayed).name if displayed else "无") +
                                     ("（未启用）" if displayed and not settings["use_voice"] else ""))
            self.voice_label.setToolTip(displayed)
            if ((job.get("path") and (self._preview_path or settings.get('background_pool_id')))
                    or (empty and (self._preview_path is not None or voice))):
                self.request_background(job.get("path", ""))
            width, height = output_size(settings, self._media)
            self.preview.canvas_size = QtCore.QSize(width,height)
            self.layer_panel.set_canvas_size(width,height)
            self._sync_preview_layers()
            self.preview.cover = settings["fit"] == "cover"
            self.preview.background_zoom = 1.0 if empty else scale_at(settings, timing["time"] if timing else 0, duration)
            request = (self.title.toPlainText(), self.body.toPlainText(), width, height, settings)
            self._overlay_request = request
            if self.overlay_worker:
                self._overlay_pending = request
                if request != self.overlay_worker.request:
                    self.overlay_worker.cancel.set()
            else:
                self._start_overlay(request)
            if settings["use_voice"] and self._voice_duration:
                text = f"成品跟随人声：{self._voice_duration:.2f}秒"
                if self._media and not (still or empty):
                    plan = adaptation_plan(self._media["duration"], self._voice_duration, settings, source_fps(self._media))
                    text += f"；背景{self._media['duration']:.2f}秒 / {plan['speed']:.2f}×"
                    if plan["repeating"]:
                        text += " / 循环" + ("叠化" if plan["fade"] else "直切")
                self.duration_status.setText(text)
            elif voice:
                self.duration_status.setText("正在读取人声长度。")
            elif empty:
                self.duration_status.setText(f"无底层背景：{image_duration(settings):.2f}秒；图片控件的大小和动画在控件属性中调整。")
            elif still:
                self.duration_status.setText(f"图片背景：{image_duration(settings):.2f}秒；选中底层背景视觉控件，在控件属性→动画调整。")
            elif settings["unvoiced_duration_mode"] == "fixed":
                duration = target_duration(self._media["duration"] if self._media else 0, settings)
                text = f"无人声固定成品：{duration:.2f}秒"
                if self._media and not (still or empty):
                    plan = adaptation_plan(self._media["duration"], duration, settings, source_fps(self._media))
                    text += f"；素材{self._media['duration']:.2f}秒 / {plan['speed']:.2f}×"
                    text += " / 循环" + ("叠化" if plan["fade"] else "直切") if plan["repeating"] else " / 截短" if self._media["duration"] > duration else ""
                self.duration_status.setText(text)
            else:
                self.duration_status.setText("没有启用人声，保持原视频长度。")
        except Exception as error:
            self.preview.overlay = QtGui.QImage()
            self.preview.composite_planes = []
            self._overlay_status = str(error)
            self._update_preview_status()
        self.preview.update()

    def _start_overlay(self, request):
        job = {**self.active_entry(), 'title': request[0], 'body': request[1]}
        bundle, registry = self._design_preview_bundle(request[4], job)
        self.overlay_worker = OverlayWorker(request, self._image_cache, self, design_job=job,
            design_bundle=bundle, design_registry=registry, directory=str(self.store.directory))
        self.overlay_worker.ready.connect(self._overlay_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.overlay_worker, self, 'overlay_worker', self._overlay_finished)
        self.overlay_worker.start()

    def _animation_preview_changed(self):
        if self._loading or self.is_batch_busy():
            return
        self._overlay_request = None  # An older frame must not land after a seek.
        self._overlay_pending = None
        if self.overlay_worker:
            self.overlay_worker.cancel.set()
        if not self.animation_preview_timer.isActive():
            self.animation_preview_timer.start()

    def _design_preview_bundle(self, settings, job):
        from .node_effects import active_effects
        effects = active_effects(settings)
        if not effects or '_editor_timing' not in settings:
            return None, None
        try:
            import json
            from model.node_workflow.library import WorkflowLibrary
            library = getattr(self.context, 'workflow_library', None) or WorkflowLibrary()
            stamps = []
            for layer in effects:
                path = library._path(layer['workflow_id'])
                stat = path.stat()
                stamps.append((layer['workflow_id'], stat.st_mtime_ns, stat.st_size))
            key = json.dumps([effects, job, self.state['revision'], stamps], sort_keys=True, ensure_ascii=False)
            if self._design_cache is None or self._design_cache[0] != key:
                from .design_preview import prepare_design_bundle
                state = {**self.state, 'settings': self.read_settings()}
                bundle, registry = prepare_design_bundle(state, job, self.context)
                self._design_cache = (key, bundle, registry)
            return self._design_cache[1:]
        except Exception as error:
            settings['_design_notice'] = '节点动画快览未执行：' + str(error)
            return None, None

    def _overlay_ready(self, result):
        if result["request"] != self._overlay_request:
            return
        if result.get("error"):
            self.preview.overlay = QtGui.QImage()
            self.preview.composite_planes = []
            self._overlay_status = "图层预览失败：" + result["error"]
            self._log("图层预览失败：" + result["error"] + "\n" + result.get("traceback", ""))
        else:
            _, _, width, height, preview_settings = result["request"]
            background_label = "图片背景缩放快览" if preview_settings.get('_still_background') else "背景首帧"
            self.preview.overlay = result["image"]
            self.preview.composite_planes = result.get("planes", [])
            info = result["info"]
            sizes = info.get("component_sizes",{})
            text = ("文本框字号："+" / ".join(f"{size:.0f}" for size in list(sizes.values())[:3])
                    +(f"（共{len(sizes)}个）" if len(sizes)>3 else "")) if sizes else (
                    f"自适应字号：标题 {info['title_size']:.0f} / 正文 {info['body_size']:.0f}")
            warnings = result["info"].get("warnings",[])
            self._overlay_status = ("需调整（导出仍会阻止）："+"；".join(warnings)) if warnings else f"{text}；{width}×{height}（{background_label}）"
            if any(layer["kind"] == "caption" and layer["enabled"] and layer["opacity"] for layer in result["request"][4]["layers"]):
                self._overlay_status += "；动态字幕为排版示意，点击播放预览检查真实时机。"
            from .scrolling_text import is_scrolling
            if any(is_scrolling(layer) and layer["enabled"] and layer["opacity"] for layer in result["request"][4]["layers"]):
                self._overlay_status += "；滚动位置已跟随下方动画快览时间轴。"
            from .node_effects import active_effects
            if active_effects(result['request'][4]):
                self._overlay_status += '；节点文字按已保存工作流及当前任务快览，不推进资源池。'
            timing = result['request'][4].get('_editor_timing')
            if timing:
                self._overlay_status += f"；动画时刻 {timing['time']:.2f}/{timing['duration']:.2f} 秒（{background_label}）。"
            if result.get('design_notices'):
                self._overlay_status += '；' + '；'.join(result['design_notices'])
            warning = "；".join(warnings)
            if warning and warning != self._last_preview_warning:
                self._log("预览排版提示："+warning)
            self._last_preview_warning = warning
        self._update_preview_status()
        self.preview.update()

    def _update_preview_status(self):
        self.preview_status.setText("\n".join(part for part in (self._background_error,self._framing_status,self._overlay_status) if part))

    def refresh_preview(self):
        if self.is_batch_busy():
            return
        self._design_cache = None
        self.update_preview()
        job = self.active_entry()
        if job:
            self.request_background(job["path"],force=True)

    def _overlay_finished(self):
        self.overlay_worker.deleteLater()
        self.overlay_worker = None
        if self._overlay_pending:
            request, self._overlay_pending = self._overlay_pending, None
            self._start_overlay(request)

    def active_voice(self, job=None):
        bound_job = job or self.active_entry()
        if bound_job.get('render_assignment'):
            return bound_job['render_assignment']['voice']
        settings = dict(self.state["settings"])
        settings.update(use_voice=self.controls["use_voice"].isChecked(),
                        voice_pool_id=self.controls["voice_pool_id"].currentData(),
                        voice_pool_mode=self.controls["voice_pool_mode"].currentData())
        return resolve_voice(job or self.active_entry(), {**self.state, "settings": settings})

    def follow_voice_card(self, scope):
        if self.is_batch_busy():
            return
        key = "copy_pool" if scope == "current" and self.current_copy_id else "jobs"
        identifier = self.current_copy_id if key == "copy_pool" else self.current_id
        selected = set(self.selected_ids())
        targets = [job for job in self.state[key] if scope == "all" or
                   scope == "selected" and job["id"] in selected or scope == "current" and job["id"] == identifier]
        if not targets:
            self.status.setText("请先选中要更改的人声条目。")
            return
        for job in targets:
            job.pop("voice_override", None)
            job.update(status="待生成", error="")
        if self.persist():
            self.refresh_tables()
            self.refresh_preview()
            self.status.setText(f"{len(targets)} 项已改为跟随当前人声控件。")

    def request_background(self, path, force=False):
        job = self.active_entry()
        try:
            voice = self.active_voice(job)
        except (ValueError, OSError) as error:
            self._background_error = "人声来源需调整：" + str(error)
            self._update_preview_status()
            return
        request = (path, voice, self.ffmpeg.text().strip())
        frame_settings = {key:self.controls[key].currentData() for key in ("aspect","fit")}
        frame_settings["auto_crop_black_bars"] = self.controls["auto_crop_black_bars"].isChecked()
        request += (frame_settings["aspect"],frame_settings["fit"],frame_settings["auto_crop_black_bars"],blur_strength({"background_blur":self.controls["background_blur"].value()}))
        if not force:
            cancel = getattr(self.preview_worker, "cancel", None)
            if self._preview_pending == request or (self._preview_path == request and not (cancel and cancel.is_set())):
                return
        # Never rescale an old, already fitted poster while a new one is queued.
        self.preview.background = QtGui.QImage()
        self._framing_status = "正在更新背景画幅…" if path else ""
        self._update_preview_status()
        self.preview.update()
        if self.preview_worker is not None:
            self._preview_pending = request
            self._force_preview_pending = self._force_preview_pending or force
            if getattr(self.preview_worker, "cancel", None) is not None:
                self.preview_worker.cancel.set()
            return
        same_media = self._preview_path and self._preview_path[:3] == request[:3]
        if not same_media or force:
            self.invalidate_playback()
        self._preview_path = request
        if not same_media:
            self._media = self._voice_duration = None
            self.preview.background = QtGui.QImage()
        self._background_error = ""
        self.preview_worker = PreviewWorker(path, self.ffmpeg.text().strip(), self, voice_path=voice, blur=request[-1], frame_settings=frame_settings, cache_directory=self.store.directory)
        self.preview_worker.ready.connect(self._background_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.preview_worker, self, 'preview_worker', self._background_finished)
        self.preview_worker.start()

    def _background_ready(self, result):
        if result.get("cancelled"):
            return
        job = self.active_entry()
        try:
            voice = self.active_voice(job)
        except (ValueError, OSError):
            return  # An obsolete result must not revive an invalid binding.
        if (result["path"], result.get("voice_path", ""), result.get("configured", ""),
                result.get("aspect",self.controls["aspect"].currentData()),result.get("fit",self.controls["fit"].currentData()),result.get("blur",0)) != (
                job.get("path", ""), voice, self.ffmpeg.text().strip(),self.controls["aspect"].currentData(),
                self.controls["fit"].currentData(),self.controls["background_blur"].value()):
            return
        if result.get("auto_crop_black_bars", self.controls["auto_crop_black_bars"].isChecked()) != self.controls["auto_crop_black_bars"].isChecked():
            return
        self._framing_status = "已去除素材自带黑边；铺满导出画布。" if result.get("source_crop") else ""
        if result.get("error"):
            self._framing_status = ""
            self._background_error = "背景预览失败：" + result["error"]
            self._update_preview_status()
            self._log(self._background_error)
        else:
            self._background_error = ""
            self.preview.background = QtGui.QImage.fromData(result["image"])
            self._media = result["media"]
            self._voice_duration = result.get("voice_duration")
            self.update_preview()
        self.preview.update()

    def _background_finished(self):
        if self.preview_worker.cancel.is_set():
            self._preview_path = None  # A cancelled request has no reusable poster.
        self.preview_worker.deleteLater()
        self.preview_worker = None
        if self._preview_pending:
            request, self._preview_pending = self._preview_pending, None
            force,self._force_preview_pending = self._force_preview_pending,False
            self.request_background(request[0],force=force)

    def selected_ids(self):
        return [self.jobs.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                for row in sorted({index.row() for index in self.jobs.selectedIndexes()})]

    def refresh_tables(self):
        current = self.current_id
        self._loading = True
        job_rows = []
        for job in self.state["jobs"]:
            try:
                voice = self.active_voice(job)
                label = Path(voice).name if voice else "无"
            except (ValueError, OSError) as error:
                voice, label = str(error), "来源需调整"
            display = job.get('label') or job.get('name') or Path(job['path']).stem
            from .render_timing import clock_text
            job_rows.append((job["id"], [display, job["status"], '', clock_text(job['render_timing'].get('elapsed_seconds')) if job.get('render_timing') else '—'],
                "\n".join(part for part in ('成品：'+job.get('name',''), '背景：'+(job['path'] or '未分配'),
                    '人声：'+(voice or label), job.get("error"), job.get("task_dir")) if part), ""))
        self.jobs.populate(job_rows)
        profile_id = self.state['active_profile']
        progress = self._render_progress.get(profile_id, {})
        active = profile_id in self._host_plans
        from .row_progress import set_row_progress
        for row, job in enumerate(self.state['jobs']):
            from .render_selection import checked
            item=self.jobs.item(row,0)
            item.setFlags(item.flags()|QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if checked(job,self.state.get('render_plans',()),profile_id)
                               else QtCore.Qt.CheckState.Unchecked)
            value = progress.get(job['id'], 100 if job['status'] == '已完成' else 0)
            in_batch = active and job['id'] in progress
            set_row_progress(self.jobs, row, value if in_batch or job['status'] == '已完成' else None)
            if in_batch:
                self.jobs.item(row, 1).setText('已结束' if value == 100 else
                    self._render_stages.get(profile_id, {}).get(job['id'], '渲染中' if value else '排队中'))
        self.jobs.set_read_only(self.jobs.read_only)
        self.sync_render_selection()
        self.render_queue_panel.refresh()
        music_rows = []
        for row, item in enumerate(self.state["music"]):
            prefix = "→ " if item["id"] == self.state["next_music_id"] or not self.state["next_music_id"] and row == 0 else ""
            music_rows.append((item["id"], [prefix+Path(item["path"]).name,
                f"{item.get('cursor',0):.1f}秒",str(item.get("last_used") or "未使用")],item["path"],""))
        self.music.populate(music_rows)
        if current and not self.jobs.read_only:
            row = next((i for i, job in enumerate(self.state["jobs"]) if job["id"] == current), -1)
            if row >= 0:
                self._show_job(self.state["jobs"][row])
        self.refresh_pools()
        self._sync_preview_layers()
        self._loading = False
        self.refresh_navigation()

    def add_files(self, paths, *, background=False):
        if self.is_batch_busy():
            return
        narration = [path for path in paths if Path(path).is_dir() or Path(path).suffix.casefold() in AUDIO_SUFFIXES and Path(path).stem.casefold() == "task_audio"]
        images = [] if background else [path for path in paths if Path(path).suffix.casefold() in IMAGE_SUFFIXES]
        regular = [path for path in paths if path not in narration]
        self._commit_edit()
        videos = [path for path in regular if Path(path).suffix.casefold() in (VIDEO_SUFFIXES | IMAGE_SUFFIXES if background else VIDEO_SUFFIXES)]
        music = [path for path in regular if Path(path).suffix.casefold() in AUDIO_SUFFIXES]
        n_video = add_videos(self.state, videos)
        n_music = add_paths(self.state["music"], music, music=True)
        if videos and not self.output.text().strip():
            self.output.setText(str(Path(videos[0]).parent / "静态文字版"))
        self.persist()
        self.refresh_tables()
        if videos:
            self.tabs.setCurrentWidget(self.resources)
            self.resources.setCurrentWidget(self.video_pool)
            if self.video_pool.rowCount():
                self.video_pool.setCurrentCell(0, 0)
        self.status.setText(f"已添加{n_video}个背景素材、{n_music}首音乐；重复路径自动忽略。")
        if narration:
            self._pending_layer_paths.extend(images)
            self.start_import(narration)
        elif images:
            self.import_pool_images(images)

    def add_voices(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "批量导入人声", "", "音频 (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.wma)")
        if paths:
            self.start_import(paths)

    def add_task_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择任务根目录，递归查找task_audio")
        if path:
            self.start_import([path])

    def start_import(self, paths=(), targets=()):
        if self.is_batch_busy():
            self.status.setText("已有导入或生成操作，请稍后再试。")
            return False
        self.edit_timer.stop()
        if not self.persist():
            return False
        self._import_result = None
        self.import_worker = ImportWorker(paths, targets, self)
        self.import_worker.ready.connect(self._import_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        self.import_worker.message.connect(self._log, QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.import_worker, self, 'import_worker', self._import_finished)
        self._set_work_controls(True)
        self.import_worker.start()
        return True

    def _import_ready(self, result):
        self._import_result = result

    def _import_finished(self):
        self.import_worker.deleteLater()
        self.import_worker = None
        self._set_work_controls(False)
        result = self._import_result or {"entries": [], "issues": ["没有返回导入结果。"]}
        if result.get("cancelled"):
            self.status.setText("已取消导入，原队列保留。")
            return
        try:
            self._commit_edit()
            count, _updated = merge_copies(self.state, result["entries"])
            self.controls["use_voice"].setChecked(True)
            self.persist()
            self.refresh_tables()
            self.tabs.setCurrentWidget(self.resources)
            self.resources.setCurrentWidget(self.copy_pool)
            if self.copy_pool.rowCount():
                self.copy_pool.setCurrentCell(0, 0)
                self.select_copy(0)
            self.status.setText(f"文案池导入 {count} 个人声任务；请从“批量→搭配资源”加入生成队列。")
            for issue in result.get("issues", []):
                self._log(issue)
        except Exception:
            self._log(traceback.format_exc())
        if self._pending_layer_paths:
            paths, self._pending_layer_paths = self._pending_layer_paths, []
            self.import_pool_images(paths)

    def replace_voice(self):
        key, identifier = ("copy_pool", self.current_copy_id) if self.current_copy_id else ("jobs", self.current_id)
        job = next((item for item in self.state[key] if item["id"] == identifier), None)
        if not job:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "更换当前任务的人声", "", "音频 (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.wma)")
        if path:
            job.update(voice_path=path, voice_override=True, match_error="", error="", status="待生成")
            self.controls["use_voice"].setChecked(True)
            self.persist()
            self.refresh_tables()

    def add_videos(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加背景视频／图片", "", "背景素材 (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mts *.png *.jpg *.jpeg *.jfif *.webp *.bmp *.tif *.tiff)")
        self.add_files(paths, background=True)

    def add_music(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加音乐", "", "音频 (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.wma)")
        if paths and not self.is_batch_busy():
            count = add_paths(self.state["music"], paths, music=True)
            self.persist()
            self.refresh_tables()
            self.status.setText(f"已添加{count}首音乐；重复路径自动忽略。")

    def remove_jobs(self):
        self.remove_resource("jobs")

    def remove_music(self):
        self.remove_resource("music")

    def move_music(self, direction):
        row = self.music.currentRow()
        target = row + direction
        if 0 <= row < len(self.state["music"]) and 0 <= target < len(self.state["music"]):
            self.state["music"][row], self.state["music"][target] = self.state["music"][target], self.state["music"][row]
            self.persist()
            self.refresh_tables()
            self.music.setCurrentCell(target, 0)

    def reset_music(self):
        if QtWidgets.QMessageBox.question(self, "重置音乐进度", "把当前选用音乐池的轮换和播放位置重置到开头？") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        from .custom_pools import music_source
        self._commit_edit()
        items, _next_id, pool = music_source(self.state)
        if pool:
            pool["next_id"] = ""
        else:
            self.state["next_music_id"] = ""
        for item in items:
            item["cursor"] = 0.0
        self.persist()
        self.refresh_tables()

    def copy_text(self):
        self._commit_edit()
        ids = set(self.selected_ids())
        for job in self.state["jobs"]:
            if job["id"] in ids:
                job.update(title=self.title.toPlainText(), body=self.body.toPlainText())
        self.persist()
        self.status.setText(f"文案已应用到{len(ids)}个所选视频；输出名称不变。")

    def pick_color(self, key):
        color = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.controls[key].text()), self)
        if color.isValid():
            self.controls[key].setText(color.name())
            self.schedule_edit()

    def choose_output(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if folder:
            self.output.setText(folder)

    def open_result(self, row, _column):
        job = self.state["jobs"][row]
        path = Path(job.get("output") or job["path"])
        if path.is_file():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def open_output_directory(self):
        try:
            entry = self.active_entry()
            job = entry
            if not self.current_id or self.current_copy_id:
                if entry.get("path"):
                    pair_key = [self.current_copy_id, file_identity(entry["path"])]
                    job = next((item for item in self.state["jobs"]
                                if item.get("pair_key") == pair_key), entry)
            output = str(job.get("output") or "")
            if output and Path(output).is_file():
                folder = Path(output).parent
            else:
                value = entry.get("task_dir") or self.output.text().strip()
                folder = Path(value) if value else Path(entry["path"]).parent if entry.get("path") else None
            if folder is None:
                self.status.setText("请先选择视频或设置输出目录。")
                return False
            folder = folder.resolve()
            if not folder.is_dir():
                self.status.setText("成品目录尚不存在，请先导出：" + str(folder))
                return False
            if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder))):
                raise RuntimeError("无法打开成品目录：" + str(folder))
            return True
        except Exception as error:
            self._log("打开成品目录失败：" + str(error) + "\n" + traceback.format_exc())
            return False

    def start_batch(self, selected):
        if self.is_batch_busy():
            return
        if self.playback_worker:
            self._batch_after_preview = bool(selected)
            self.stop_playback()
            self.status.setText("正在结束预览准备，随后自动开始生成；不会同时争用编码器。")
            return
        self.stop_playback()
        self.edit_timer.stop()
        if not self.persist():
            return
        if selected and self.right_tabs.currentWidget() != self.render_queue_panel:
            self.open_global_queue()
            self.status.setText("请在生成队列中多选任务后生成；资源池本身不是生成队列。")
            return
        from .render_selection import identifiers
        checked=identifiers(self.state)
        ids = [identifier for identifier in self.selected_ids() if identifier in checked] if selected else checked
        if not ids:
            self.status.setText("没有待生成任务；导入资源后请先用“批量→搭配资源”加入队列。")
            return
        try:
            from .background import validate_batch_outputs
            validate_batch_outputs(self.state, ids)
            self.store.sync_render_selection(self.state['active_profile'], ids)
            self.state = self.store.load()
            plan=next(p for p in self.state['render_plans'] if p['profile_id']==self.state['active_profile'] and p['status']=='staged')
        except Exception as error:
            self.status.setText('加入渲染队列失败：' + str(error))
            return
        self.status.setText(f'已加入“{plan["name"]}”的 {len(ids)} 个视频；可切换配置继续添加，最后在全局队列点击开始。')
        self.open_global_queue()

    def open_global_queue(self):
        self.render_queue_panel.refresh()
        self.right_tabs.setCurrentWidget(self.render_queue_panel)

    def sync_render_selection(self):
        if self.is_batch_busy(): return True
        from .render_selection import identifiers
        profile=next(p for p in self.state['profiles'] if p['id']==self.state['active_profile'])
        try:
            changed=self.store.sync_render_selection(profile['id'],identifiers(self.state),
                expected_profile_revision=profile.get('revision',0))
            if changed: self.state=self.store.load()
            return True
        except (ValueError,OSError) as error:
            self.status.setText('渲染列表未更新：'+str(error))
            return False

    def _render_check_changed(self,item):
        if getattr(self,'_loading',True) or item.column()!=0: return
        if self.is_batch_busy(): return
        identifier=item.data(QtCore.Qt.ItemDataRole.UserRole)
        job=next((j for j in self.state['jobs'] if j['id']==identifier),None)
        if job is None: return
        value=item.checkState()==QtCore.Qt.CheckState.Checked
        from .render_selection import checked
        if value==checked(job,self.state.get('render_plans',()),self.state['active_profile']): return
        previous=copy.deepcopy(job)
        job['render_checked']=value
        if not self.persist():
            job.clear();job.update(previous)
            with QtCore.QSignalBlocker(self.jobs):
                item.setCheckState(QtCore.Qt.CheckState.Checked if not value else QtCore.Qt.CheckState.Unchecked)
            return
        self.sync_render_selection()
        self.render_queue_panel.refresh()

    def start_staged_plans(self):
        if self.worker is not None or self.import_worker is not None or self.layer_panel.worker is not None or self.image_import_worker is not None:
            self.status.setText('本地导入或渲染准备尚未结束，请稍后开始全局队列。')
            return
        if not self.save_render_settings():
            return
        if not self.is_batch_busy() and not self.persist():
            return
        if not self.sync_render_selection(): return
        submitted = 0
        for plan in list(self.store.load().get('render_plans', [])):
            if plan['status'] != 'staged':
                continue
            if self._launch_batch(plan['ids'], plan=plan):
                submitted += 1
        if submitted:
            self.status.setText(f'已提交 {submitted} 个项目；不同项目可并行，所有编码共用全局并发上限。')
        self.render_queue_panel.refresh()

    def quick_export_current(self):
        if getattr(self, 'pool_preview', None) is not None and self.pool_preview.choices:
            self.status.setText('正在试看资源池素材，请先点击“恢复生成顺序”再快速导出。')
            return
        if self.is_batch_busy():
            self.status.setText("正在处理其他操作，请稍后快速导出。")
            return
        self.edit_timer.stop()
        try:
            self._commit_edit()
            entry = copy.deepcopy(self.active_entry())
            entry.update(title=self.title.toPlainText(), body=self.body.toPlainText(),
                         name=self.name.text().strip() or entry.get("name", ""))
            state, job_id = prepare_current_export(self.state, entry,
                self.current_copy_id, self.current_id)
            payload = {"state": state, "job_id": job_id}
        except Exception as error:
            self._log("快速导出失败：" + str(error) + "\n" + traceback.format_exc())
            return
        self.stop_playback()
        if self.playback_worker:
            # Freeze this exact composition; do not resolve it again after waiting.
            self._batch_after_preview = payload
            self._set_work_controls(True)
            self.status.setText("正在结束预览准备，随后自动导出当前成品。")
            return
        self._start_quick_export(payload)

    def _start_quick_export(self, payload):
        try:
            self.state = self.store.save(payload["state"], payload["state"]["revision"])
        except Exception:
            self._set_work_controls(False)
            self._log("快速导出记录保存失败，未开始生成：\n" + traceback.format_exc())
            return
        self._quick_export_id = payload["job_id"]
        self._quick_export_ids[self.state['active_profile']] = payload['job_id']
        self.progress_bar.setValue(0)
        self._launch_batch([self._quick_export_id])

    def _launch_batch(self, ids, *, plan=None):
        profile_id = plan['profile_id'] if plan else self.state['active_profile']
        if profile_id in self._host_plans:
            self.status.setText('这个配置已有渲染任务；请等待它完成或在后台任务中取消。')
            return False
        try:
            from .store import ProfileStore
            from .background import validate_batch_outputs
            profile_store = ProfileStore(self.store.directory, profile_id)
            render_state = profile_store.load()
            self.status.setText(f'正在核对{len(ids)}项成品路径…')
            checked_at = time.monotonic()
            self._log(f'提交前校验：{len(ids)}项成品，共用一次源文件路径检查。')
            validate_batch_outputs(render_state, ids)
            self._log(f'成品路径校验完成：{len(ids)}项，用时{time.monotonic()-checked_at:.2f}秒。')
            from .cloud_alignment import prepare_binding
            elevenlabs_binding = prepare_binding(render_state['settings'], self.context)
            from .node_effects import prepare_bundle
            bundle, registry, node_resources = prepare_bundle(render_state, ids, self.context)
        except Exception as error:
            if plan:
                try:
                    self.store.update_render_plan(plan['id'], 'staged', error=str(error))
                except Exception:
                    pass
            else:
                self._quick_export_ids.pop(profile_id, None)
                if profile_id == self.state['active_profile']:
                    self._set_work_controls(self.is_batch_busy())
            self._log('渲染计划准备失败：'+str(error))
            self.status.setText('渲染计划准备失败：'+str(error))
            return False
        self.reset_editor_history()
        self.progress_bar.setValue(0)
        self._batch_percentages = dict.fromkeys(ids, 0)
        if callable(getattr(self.context, "submit_background_task", None)):
            from functools import partial
            runner = partial(run_batch_video_job, node_registry=registry) if bundle else run_batch_video_job
            payload = {"store_dir": str(self.store.directory.resolve()), "profile_id": profile_id,
                       "state": render_state, "ids": list(ids)}
            if elevenlabs_binding: payload['elevenlabs_binding'] = elevenlabs_binding
            if bundle: payload['node_effects'] = bundle
            try:
                profile_name = next(p['name'] for p in render_state['profiles'] if p['id'] == profile_id)
                handle = self.context.submit_background_task(
                    title=f"文案视频 · {profile_name} · {len(ids)} 个", runner=runner, payload=payload,
                    kind="media", resources=batch_resources(self.store, render_state, ids)+list(node_resources))
                if plan:
                    self.store.update_render_plan(plan['id'], 'submitted', handle.task_id)
                self._host_plans[profile_id] = handle
                self.render_queue_panel.timing.begin(profile_id, ids)
                self._render_progress[profile_id] = dict.fromkeys(ids, 0)
                self._render_stages[profile_id] = dict.fromkeys(ids, '排队中')
                handle.itemProgress.connect(lambda job_id, value, p=profile_id: self._host_item_progress(p, job_id, value))
                handle.itemStage.connect(lambda job_id, stage, p=profile_id: self._host_item_stage(p, job_id, stage))
                handle.progressChanged.connect(lambda message, value, p=profile_id: self._host_batch_progress(p, message, value))
                handle.completed.connect(lambda result, h=handle, p=profile_id: self._host_batch_result(h, p, result))
                handle.failed.connect(lambda message, p=profile_id: self._host_batch_failed(p, message))
                handle.finished.connect(lambda p=profile_id, h=handle, q=plan: self._host_render_finished(p, h, q))
            except Exception as error:
                if 'handle' in locals():
                    handle.requestInterruption()
                if plan:
                    try:
                        self.store.update_render_plan(plan['id'], 'staged', error=str(error))
                    except Exception:
                        pass
                self._host_plans.pop(profile_id, None)
                self.render_queue_panel.timing.finish(profile_id)
                self._render_progress.pop(profile_id, None)
                self._render_stages.pop(profile_id, None)
                self._quick_export_ids.pop(profile_id, None)
                self._set_work_controls(self.is_batch_busy())
                self._log('提交后台失败：' + traceback.format_exc())
                self.status.setText('提交后台失败：' + str(error) + '；计划仍在队列，可修复后重试。')
                return False
            if profile_id == self.state['active_profile']:
                self._set_work_controls(True)
                self.refresh_tables()
            self.status.setText(f'已提交“{profile_name}”后台渲染，可切换配置继续准备。')
            return True
        if plan:
            self.status.setText('主程序没有后台任务管理器，无法启动全局队列。')
            return False
        self.worker = BatchWorker(self.store, self.state, ids, self, node_bundle=bundle, node_registry=registry,
            elevenlabs_binding=elevenlabs_binding)
        self.render_queue_panel.timing.begin(profile_id, ids, queued=False)
        self.worker.message.connect(self._log, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.progress.connect(self._progress, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.jobDone.connect(self._job_done, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.result.connect(self._batch_result, QtCore.Qt.ConnectionType.QueuedConnection)
        connect_thread_finished(self.worker, self, 'worker', self._batch_finished)
        self._set_work_controls(True)
        self.worker.start()
        return True

    @QtCore.pyqtSlot(str)
    def _log(self, message):
        self.status.setText(message.splitlines()[0][:300])
        if self.context:
            self.context.log(message, level=logging.ERROR if "Traceback" in message else logging.INFO)

    def _progress(self, _id, value):
        self.render_queue_panel.timing.progress(self.state['active_profile'], _id, value)
        values = getattr(self, '_batch_percentages', {})
        if _id in values:
            values[_id] = max(0, min(100, int(value)))
            self.progress_bar.setValue(int(sum(values.values())/max(1, len(values))))
        else:
            self.progress_bar.setValue(value)
        from .row_progress import set_row_progress
        for row in range(self.jobs.rowCount()):
            if self.jobs.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) == _id:
                set_row_progress(self.jobs, row, value)
                break

    def _job_done(self, job_id, status):
        self.render_queue_panel.timing.stage(self.state['active_profile'], job_id, status)
        self._progress(job_id, 100)
        for row in range(self.jobs.rowCount()):
            if self.jobs.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) == job_id:
                self.jobs.item(row, 1).setText(status)

    def _batch_result(self, result):
        self.render_queue_panel.timing.finish(self.state['active_profile'], result)
        self.status.setText(result.get("error") or f"已完成{result['completed']}个，需处理{result['failed']}个。" +
                            ("已停止，剩余队列与音乐位置已保留。" if result.get("cancelled") else "音乐轮换进度已保存。"))
        if result.get('failed') or result.get('error'):
            from .export_results_ui import show_export_warning
            show_export_warning(self,result)

    def _host_item_progress(self, profile_id, job_id, value):
        self.render_queue_panel.timing.progress(profile_id, job_id, value)
        self._render_progress.setdefault(profile_id, {})[job_id] = value
        self.render_queue_panel.update_progress(profile_id)
        if profile_id == self.state['active_profile']:
            from .row_progress import set_row_progress
            for row, job in enumerate(self.state['jobs']):
                if job['id'] == job_id:
                    set_row_progress(self.jobs, row, value)
                    self.jobs.item(row, 1).setText('已结束' if value == 100 else
                        self._render_stages.get(profile_id, {}).get(job_id, '渲染中' if value else '准备中'))
                    break

    def _host_item_stage(self, profile_id, job_id, stage):
        self.render_queue_panel.timing.stage(profile_id, job_id, stage)
        self._render_stages.setdefault(profile_id, {})[job_id] = stage
        self.render_queue_panel.update_progress(profile_id)
        if profile_id == self.state['active_profile']:
            for row, job in enumerate(self.state['jobs']):
                if job['id'] == job_id:
                    self.jobs.item(row, 1).setText(stage)
                    self.jobs.item(row, 1).setToolTip(stage)
                    break

    def _host_batch_progress(self, profile_id, message, value):
        if profile_id == self.state['active_profile']:
            self.status.setText(message)
            if value is not None:
                self.progress_bar.setValue(value)

    def _host_batch_result(self, handle, profile_id, result):
        self.render_queue_panel.timing.finish(profile_id, result)
        if profile_id == self.state['active_profile']:
            self._batch_result(result)
        if profile_id == self.state['active_profile'] and not result.get("failed") and not result.get("error") and not result.get("cancelled"):
            self.progress_bar.setValue(100)
        handle.set_result_handler(lambda: QtWidgets.QMessageBox.information(self, "文案视频导出结果",
            f"完成 {result['completed']} 个，需处理 {result['failed']} 个。\n" +
            "\n".join(result.get("failures", []) + result.get("paths", []))),
            pending=bool(result.get("failed") or result.get("error")))

    def _host_batch_failed(self, profile_id, message):
        if profile_id == self.state['active_profile']:
            self.status.setText(message)

    def _host_render_finished(self, profile_id, handle, plan):
        self.render_queue_panel.timing.finish(profile_id)
        status = handle.manager.get(handle.task_id)['status']
        if plan:
            try:
                self.store.update_render_plan(plan['id'], status, timing=self.render_queue_panel.timing.metrics(profile_id))
            except Exception as error:
                self._log('渲染计划状态保存失败：' + str(error))
        self._host_plans.pop(profile_id, None)
        self._render_progress.pop(profile_id, None)
        self._render_stages.pop(profile_id, None)
        self.render_queue_panel.finish_pending_removal(plan, handle.task_id)
        try:
            self.render_queue_panel.refresh()
        except Exception as error:
            self._log('刷新渲染队列失败：' + str(error))
        quick_id = self._quick_export_ids.pop(profile_id, '')
        if profile_id != self.state['active_profile']:
            return
        self._set_work_controls(False)
        try:
            self.state = self.store.load()
            self.layer_panel.set_layers(self.state['settings']['layers'])
            self.refresh_tables()
            self.reset_editor_history()
        except Exception:
            self._log('读取生成记录失败，原记录未清空：\n' + traceback.format_exc())
            return
        if quick_id:
            job = next((item for item in self.state['jobs'] if item['id'] == quick_id), None)
            if job and job['status'] == '已完成':
                self.status.setText('快速导出完成：' + job['output'])

    def _batch_finished(self):
        if not isinstance(self.worker, BackgroundTaskHandle):
            self.worker.deleteLater()
        self.worker = None
        self._set_work_controls(False)
        try:
            self.state = self.store.load()
            self.layer_panel.set_layers(self.state["settings"]["layers"])
        except Exception:
            self._quick_export_id = ""
            self._log("读取生成记录失败，未清空原记录；请检查插件数据后重新打开。\n" + traceback.format_exc())
            return
        self.refresh_tables()
        self.reset_editor_history()
        if self._quick_export_id:
            job = next((item for item in self.state["jobs"] if item["id"] == self._quick_export_id), None)
            self._quick_export_id = ""
            if job and job["status"] == "已完成":
                self.status.setText("快速导出完成：" + job["output"])

    def is_batch_busy(self):
        return (self.state['active_profile'] in self._host_plans or self.worker is not None or self.import_worker is not None or self.layer_panel.worker is not None
                or self.image_import_worker is not None or isinstance(self._batch_after_preview, dict))

    def is_busy(self):
        return (self.is_batch_busy() or self.preview_worker is not None or self.overlay_worker is not None
                or self.playback_worker is not None
                or self.text_import_dialog is not None and self.text_import_dialog.worker is not None)

    def has_local_work(self):
        # A manager-owned render handle must not mask dialog-owned QThreads.
        # Wait for their GUI completion callbacks too, not only isRunning().
        return (getattr(self, 'voice_fx_worker', None) is not None
                or getattr(self, 'matting_dialog', None) is not None and self.matting_dialog.worker is not None
                or self.worker is not None and not isinstance(self.worker, BackgroundTaskHandle)
                or any(worker is not None for worker in (
                    self.import_worker, self.layer_panel.worker, self.image_import_worker,
                    self.preview_worker, self.overlay_worker, self.playback_worker))
                or isinstance(self._batch_after_preview, dict)
                or self.text_import_dialog is not None and self.text_import_dialog.worker is not None)

    def _set_work_controls(self, busy):
        was_read_only = self.jobs.read_only
        self.animation_timeline.setEnabled(not busy)
        if getattr(self, 'pool_preview', None) is not None:
            self.pool_preview.setEnabled(not busy)
        for widget in (self.left_column, self.preview, self.layer_panel, self.project_sections):
            widget.setEnabled(not busy)
        # Users must be able to scroll past the first completed rows while the
        # project remains immutable. Browsing never changes the active editor.
        self.jobs.setEnabled(True)
        self.jobs.set_read_only(busy)
        self.task_text_panel.setEnabled(not busy)
        # Project switching stays available while a host-owned render runs.
        self.menu_bar.setEnabled(not busy or self.state['active_profile'] in self._host_plans)
        for widget in (self.scheme,self.output):
            widget.setEnabled(not busy)
        self.global_queue_button.setEnabled(True)
        self.stop_button.setEnabled(busy)
        self.update_navigation_busy(busy)
        if was_read_only and not busy and not self._loading and not self.is_batch_busy():
            self._selection_changed(self.jobs.currentRow())

    def cancel_work(self, *, cancel_host=True):
        self.stop_voice_effect_preview()
        if getattr(self, 'matting_dialog', None) is not None: self.matting_dialog.cancel_work()
        self.animation_preview_timer.stop()
        self.edit_timer.stop()
        pending_quick = isinstance(self._batch_after_preview, dict)
        self._batch_after_preview = None
        self.stop_playback()
        self.preview.cancel_drag()
        self.geometry_preview_timer.stop()
        self._preview_pending = None
        self._overlay_pending = None
        self._overlay_request = None
        self._force_preview_pending = False
        if self.overlay_worker:
            self.overlay_worker.cancel.set()
        if self.preview_worker:
            self.preview_worker.cancel.set()
        self._pending_layer_paths = []
        self.layer_panel.cancel_work()
        if self.image_import_worker:
            self.image_import_worker.cancel.set()
        if self.import_worker:
            self.import_worker.cancel.set()
        if self.worker and (cancel_host or not isinstance(self.worker, BackgroundTaskHandle)):
            if isinstance(self.worker, BackgroundTaskHandle):
                self.worker.requestInterruption()
            else:
                self.worker.cancel.set()
            self.stop_button.setEnabled(False)
            self.status.setText("正在停止当前视频；已完成的视频和音乐位置不受影响。")
        elif cancel_host and self.state['active_profile'] in self._host_plans:
            self._host_plans[self.state['active_profile']].requestInterruption()
            self.stop_button.setEnabled(False)
            self.status.setText('正在安全取消当前配置的渲染计划。')
        elif pending_quick:
            self._set_work_controls(False)
            self.status.setText("已取消快速导出，轮换进度未改变。")

    def closeEvent(self, event):
        if not self.save_render_settings():
            self._close_save_failed = True
            event.ignore()
            return
        self.animation_preview_timer.stop()
        if self.overlay_worker:
            self.overlay_worker.cancel.set()
        self._close_save_failed = False
        self._overlay_request = None
        self._overlay_pending = None
        pending_quick = isinstance(self._batch_after_preview, dict)
        self._batch_after_preview = None
        if pending_quick:
            self._set_work_controls(False)
        self.stop_playback()
        self.preview.cancel_drag()
        self.geometry_preview_timer.stop()
        if not self.is_batch_busy():
            self.edit_timer.stop()
            if not self.persist():
                # Do not hide the only in-memory copy or report a successful
                # shutdown when the encrypted volume is full/unavailable.
                self._close_save_failed = True
                self.status.setText(self.status.text() + '；窗口未关闭，修改仍保留。请处理上述原因后重试保存。')
                self.show()
                event.ignore()
                return
        self.hide()
        event.ignore()

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, '_overlay_request', None) is None:
            self.update_preview()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self.is_batch_busy():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not self.is_batch_busy():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            key = self.active_resource() or ""
            if key.startswith("pool:"):
                self.add_to_custom_pool(key[5:], paths)
            else:
                self.add_files(paths)
            event.acceptProposedAction()
