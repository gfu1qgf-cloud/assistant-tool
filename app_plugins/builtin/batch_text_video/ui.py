import copy
import logging
import os
from pathlib import Path
import subprocess
import threading
import traceback

from qt_compat import QtCore, QtGui, QtWidgets
from .engine import AUDIO_SUFFIXES, VIDEO_SUFFIXES, Cancelled, RENDERERS, output_size, probe, resolve_tools
from .layout import ensure_fonts, render_preview_overlay
from .store import SCHEMES, Store, add_paths, file_identity
from .matching import scan_inputs
from .adaptation import adaptation_plan, source_fps
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


class OverlayWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)

    def __init__(self, request, cache, parent=None):
        super().__init__(parent)
        self.request, self.cache = request, cache

    def run(self):
        title, body, width, height, settings = self.request
        try:
            image, info = render_preview_overlay(title, body, width, height, settings, self.cache)
            self.ready.emit({"request": self.request, "image": image, "info": info})
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

    def __init__(self, store, state, ids, parent=None):
        super().__init__(parent)
        self.store, self.state, self.ids = store, copy.deepcopy(state), list(ids)
        self.cancel = threading.Event()

    def run(self):
        completed = failed = 0
        try:
            renderer = RENDERERS[self.state["settings"]["scheme"]]
            for index, job_id in enumerate(self.ids):
                if self.cancel.is_set():
                    break
                job = next(item for item in self.state["jobs"] if item["id"] == job_id)
                self.progress.emit(job_id, 0)
                self.message.emit(f"[{index+1}/{len(self.ids)}] {Path(job['path']).name}")
                try:
                    if job.get("requires_title") and not str(job.get("title","")).strip():
                        raise ValueError("表格文案没有标题，请补上第一行标题后再生成。")
                    _, sequence_plan = resolve_sources(self.state["settings"]["layers"],
                                                        job.get("title", ""),job.get("body", ""))
                    output, plan = renderer(job, self.state, self.store.directory, self.cancel,
                                            lambda value: self.progress.emit(job_id, value), self.message.emit)
                except Cancelled:
                    break
                except Exception as error:
                    if Path(job.get("output") or "__missing__").is_file():
                        self.message.emit("请检查既有输出，未推进音乐位置。")
                    job["status"], job["error"] = "需处理", str(error)
                    self.state = self.store.save(self.state, self.state["revision"])
                    failed += 1
                    self.jobDone.emit(job_id, "需处理")
                    self.message.emit(traceback.format_exc())
                    continue
                # A failed ledger write must stop the batch, not silently reuse its music.
                self.state = self.store.completed(self.state, job_id, output, plan, sequence_plan=sequence_plan)
                # 100% means the verified file AND its completion record are saved.
                self.progress.emit(job_id, 100)
                completed += 1
                self.jobDone.emit(job_id, "已完成")
                self.message.emit("已完成：" + output.name)
            self.result.emit({"completed": completed, "failed": failed, "cancelled": self.cancel.is_set()})
        except Exception:
            self.message.emit(traceback.format_exc())
            self.result.emit({"completed": completed, "failed": failed, "error": "记录保存或批处理异常，已停止，请查看程序日志。"})


class PreviewWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)

    def __init__(self, path, configured, parent=None, voice_path="", blur=0, frame_settings=None):
        super().__init__(parent)
        self.path, self.configured = path, configured
        self.voice_path = voice_path
        self.blur = blur
        self.frame_settings = frame_settings

    def run(self):
        identity = {"path": self.path, "voice_path": self.voice_path, "configured": self.configured, "blur": self.blur}
        if self.frame_settings:
            identity.update(self.frame_settings)
        try:
            ffmpeg, ffprobe = resolve_tools(self.configured)
            voice_duration = probe(self.voice_path, ffprobe, audio_only=True)["duration"] if self.voice_path else None
            if not self.path:
                self.ready.emit({**identity, "image": b"", "media": None, "voice_duration": voice_duration})
                return
            media = probe(self.path, ffprobe)
            if self.frame_settings:
                width,height = output_size(self.frame_settings,media)
                scale = min(1,720/width,1280/height)
                width,height = max(2,int(width*scale)//2*2),max(2,int(height*scale)//2*2)
                if self.frame_settings["fit"] == "cover":
                    frame_filter = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
                else:
                    frame_filter = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
            else:
                width,frame_filter = 720,"scale=720:-2"
            effect = blur_filter({"background_blur":self.blur},width)
            frame_filter += ","+effect if effect else ""
            result = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", "0.1",
                                     "-i", self.path, "-vf", frame_filter, "-frames:v", "1",
                                     "-f", "image2pipe", "-c:v", "png", "pipe:1"],
                                    capture_output=True, timeout=20,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode or not result.stdout:
                raise ValueError("无法读取预览帧。")
            self.ready.emit({**identity, "image": result.stdout, "media": media, "voice_duration": voice_duration})
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


class BatchTextVideoDialog(UndoController, PlaybackController, PoolController, QtWidgets.QDialog):
    def __init__(self, context=None, store=None, parent=None):
        super().__init__(parent)
        ensure_fonts()
        self.context, self.store = context, store or Store()
        self.state = self.store.load()
        self.worker = self.preview_worker = self.import_worker = None
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
        self._overlay_status = ""
        self._last_preview_warning = ""
        self._force_preview_pending = False
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
        self.jobs = ResourceTable(["任务", "背景", "人声", "状态"])
        self.jobs.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.jobs.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 65), (2, 70), (3, 60)):
            self.jobs.setColumnWidth(column, width)
        self.jobs.currentCellChanged.connect(self._selection_changed)
        self.jobs.cellDoubleClicked.connect(self.open_result)
        self.tabs.addTab(self.jobs, "生成队列")
        music_page = QtWidgets.QWidget()
        music_layout = QtWidgets.QVBoxLayout(music_page)
        note = QtWidgets.QLabel("按列表轮换，每首音乐记住播放位置。\n箭头表示下一首；只有导出成功才推进。")
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
        self.tabs.setMinimumWidth(260)
        self.splitter.addWidget(self.tabs)
        preview_page = QtWidgets.QWidget()
        preview_layout = QtWidgets.QVBoxLayout(preview_page)
        self.preview = Preview()
        self.init_playback(preview_layout)
        self.preview_status = QtWidgets.QLabel("预览使用导出时相同的排版，视频背景暂显示首帧。")
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_status)
        self.duration_status = QtWidgets.QLabel("未添加人声时，保持原视频长度。")
        self.duration_status.setWordWrap(True)
        preview_layout.addWidget(self.duration_status)
        self.refresh_preview_button = QtWidgets.QPushButton("刷新预览")
        self.refresh_preview_button.setToolTip("重新读取当前视频背景，并按当前文案、图层设置更新首帧预览。")
        self.refresh_preview_button.clicked.connect(self.refresh_preview)
        preview_tools = QtWidgets.QHBoxLayout()
        self.show_bounds_checkbox = QtWidgets.QCheckBox("显示框边界")
        self.show_bounds_checkbox.setChecked(self.state["settings"]["show_layer_bounds"])
        self.show_bounds_checkbox.setToolTip("默认显示框的范围和实际宽高；辅助线只用于编辑，不会出现在导出视频中。")
        preview_tools.addWidget(self.show_bounds_checkbox)
        preview_tools.addWidget(self.refresh_preview_button,1)
        preview_layout.addLayout(preview_tools)
        self.splitter.addWidget(preview_page)
        editor_page = QtWidgets.QWidget()
        editor_layout = QtWidgets.QVBoxLayout(editor_page)
        self.name = QtWidgets.QLineEdit()
        self.title = QtWidgets.QPlainTextEdit()
        self.title.setAcceptDrops(False)
        self.title.setMaximumHeight(85)
        self.body = QtWidgets.QPlainTextEdit()
        self.body.setAcceptDrops(False)
        self.body.setMinimumHeight(130)
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
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        settings_widget = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(settings_widget)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.controls = {}
        form.addRow("方案",self.scheme)
        self._combo(form, "aspect", "画布", [("竖屏 1080×1920", "portrait"), ("横屏 1920×1080", "landscape"), ("原视频规格", "source")])
        self._combo(form, "fit", "画面", [("完整显示（不裁切）", "contain"), ("铺满画面（会裁边）", "cover")])
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
        check = QtWidgets.QCheckBox("允许覆盖同名成品")
        check.setChecked(self.state["settings"]["overwrite"])
        self.controls["overwrite"] = check
        form.addRow(check)
        note = QtWidgets.QLabel("文字的字体、字号、颜色和对齐方式，请选中左侧图层中的对应组件设置。")
        note.setWordWrap(True)
        form.addRow(note)
        component_button = QtWidgets.QPushButton("设置文字组件…")
        component_button.clicked.connect(self.edit_text_components)
        form.addRow(component_button)
        self.ffmpeg = QtWidgets.QLineEdit(self.state["settings"]["ffmpeg_path"])
        self.ffmpeg.setPlaceholderText("留空使用随程序提供的标准FFmpeg")
        form.addRow("FFmpeg", self.ffmpeg)
        scroll.setWidget(settings_widget)
        settings_tabs = QtWidgets.QTabWidget()
        settings_tabs.addTab(scroll, "基础")
        voice_widget = QtWidgets.QWidget()
        voice_form = QtWidgets.QFormLayout(voice_widget)
        voice_check = QtWidgets.QCheckBox("使用任务人声，成品长度跟随人声")
        voice_check.setChecked(self.state["settings"]["use_voice"])
        self.controls["use_voice"] = voice_check
        voice_form.addRow(voice_check)
        self._combo(voice_form, "duration_strategy", "背景适配", [("循环播放", "loop"), ("放慢填满", "slow"), ("减速＋循环", "hybrid")])
        self._combo(voice_form, "loop_transition", "循环交界", [("直接衔接", "none"), ("叠化", "fade")])
        voice_form.addRow("叠化时长 秒", self._spin("transition_seconds", 0.05, 3, floating=True))
        voice_form.addRow("混合最低速度", self._spin("min_speed", 0.1, 1, floating=True))
        self._combo(voice_form, "slow_interpolation", "减速补帧", [("帧采样（重复帧，快）", "repeat"), ("帧混合", "blend"), ("运动补偿（较慢）", "motion")])
        self.controls["slow_interpolation"].setToolTip("运动补偿使用FFmpeg运动估计插帧，并不等同于达芬奇光流算法。运动较大时可能产生扭曲，建议先生成一个样片。")
        note = QtWidgets.QLabel("不剪掉人声停顿，不改变人声语速。人声不足视频长度时截短背景；已有任务人声时忽略背景原声。")
        note.setWordWrap(True)
        voice_form.addRow(note)
        mix_widget = QtWidgets.QWidget()
        mix_form = QtWidgets.QFormLayout(mix_widget)
        for key,label,low,high in (("music_volume","无人声时音乐 %",0,200),
                                   ("silence_db","静音阈值 dB",-80,-20)):
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
        for key, label, low, high in (("voice_volume", "人声音量 %", 0, 200), ("voice_music_volume", "有人声时音乐 %", 0, 100),
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
        for widget, label in ((voice_widget, "人声时长"), (mix_widget, "混音")):
            page = QtWidgets.QScrollArea()
            page.setWidgetResizable(True)
            page.setWidget(widget)
            settings_tabs.addTab(page, label)
        self.layer_panel = LayerPanel(self.state["settings"]["layers"], self.store.directory,settings=self.state["settings"])
        self.layer_scroll = QtWidgets.QScrollArea()
        self.layer_scroll.setWidgetResizable(True)
        self.layer_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.layer_scroll.setWidget(self.layer_panel)
        self.layer_tab_index = self.tabs.addTab(self.layer_scroll, "图层")
        self.settings_tabs = settings_tabs
        editor_layout.addWidget(settings_tabs, 1)
        editor_page.setMinimumWidth(315)
        self.splitter.addWidget(editor_page)
        self.splitter.setSizes([310, 410, 380])
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter, 1)
        output_row = QtWidgets.QHBoxLayout()
        self.output = QtWidgets.QLineEdit(self.state["settings"]["output_dir"])
        output_row.addWidget(QtWidgets.QLabel("普通视频输出目录"))
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
        self.generate = QtWidgets.QPushButton("生成待完成视频")
        self.selected = QtWidgets.QPushButton("生成所选视频")
        self.stop_button = QtWidgets.QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.generate.clicked.connect(lambda: self.start_batch(False))
        self.selected.clicked.connect(lambda: self.start_batch(True))
        self.stop_button.clicked.connect(self.cancel_work)
        for button in (self.generate, self.selected, self.stop_button):
            footer.addWidget(button)
        self.progress_bar = QtWidgets.QProgressBar()
        footer.addWidget(self.progress_bar, 1)
        layout.addLayout(footer)
        self.status = QtWidgets.QLabel("自动字号在设定范围内调整；放不下就提示，不裁掉文字。关闭窗口会保留列表与音乐进度。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.init_menus(layout)
        self.edit_timer = QtCore.QTimer(self)
        self.edit_timer.setSingleShot(True)
        self.edit_timer.setInterval(600)
        self.edit_timer.timeout.connect(self._edit_changed)
        self.geometry_preview_timer = QtCore.QTimer(self)
        self.geometry_preview_timer.setSingleShot(True)
        self.geometry_preview_timer.setInterval(80)
        self.geometry_preview_timer.timeout.connect(self.update_preview)
        self.title.textChanged.connect(self.schedule_edit)
        self.body.textChanged.connect(self.schedule_edit)
        self.name.textChanged.connect(self.schedule_edit)
        self.output.textChanged.connect(self.schedule_edit)
        self.ffmpeg.textChanged.connect(self.schedule_edit)
        self.scheme.currentIndexChanged.connect(self.schedule_edit)
        self.layer_panel.changed.connect(self.schedule_edit)
        self.layer_panel.changed.connect(self._sync_preview_layers)
        self.layer_panel.selectionChanged.connect(self._sync_preview_layers)
        self.preview.layerSelected.connect(self.layer_panel.select_layer_id)
        self.preview.geometryEdited.connect(self._preview_geometry_edited)
        self.preview.geometryCommitted.connect(self._finish_geometry_edit)
        self.show_bounds_checkbox.toggled.connect(self._sync_preview_layers)
        self.show_bounds_checkbox.toggled.connect(self.schedule_edit)
        self.layer_panel.busyChanged.connect(lambda _busy: self._set_work_controls(self.is_batch_busy()))
        self.layer_panel.message.connect(self._log)
        for key, control in self.controls.items():
            if isinstance(control, QtWidgets.QAbstractSpinBox):
                control.valueChanged.connect(self.schedule_edit)
            elif isinstance(control, QtWidgets.QCheckBox):
                control.toggled.connect(self.schedule_edit)
            elif isinstance(control, QtWidgets.QComboBox):
                control.currentIndexChanged.connect(self.schedule_edit)
        self._sync_preview_layers()
        self.refresh_tables()
        if self.jobs.rowCount():
            self.tabs.setCurrentWidget(self.jobs)
            self.jobs.setCurrentCell(0, 0)
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
        if not current or current["kind"] == "image":
            self.layer_panel.rebuild("title")
        self.layer_panel.property_tabs.setCurrentIndex(0)

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
            added,updated = merge_copies(self.state,dialog.entries,sheet=True)
            # Copy-only jobs do not require an ElevenLabs/task_audio round-trip.
            if all(not entry.get("voice_path") for entry in dialog.entries):
                self.controls["use_voice"].setChecked(False)
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
            self.status.setText(f"文案池新增{added}条、更新{updated}条；在“批量→搭配资源”中与视频配对，成品仍放入任务目录。")
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
                        layers=bind_pool_layers(self.layer_panel.values(),self.state),geometry_units=self.layer_panel.units.currentData(),
                        show_layer_bounds=self.show_bounds_checkbox.isChecked())
        return settings

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

    def _preview_geometry_edited(self,identifier,values):
        if not self._loading and not self.is_batch_busy() and self.layer_panel.set_geometry(identifier,values):
            if not self.geometry_preview_timer.isActive():
                self.geometry_preview_timer.start()

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

    def _edit_changed(self):
        self._commit_edit()
        self.persist()
        self.refresh_pools()
        self.update_preview()
        job = self.active_entry()
        if job:
            self.request_background(job["path"])

    def persist(self):
        if self.is_batch_busy():
            return False
        try:
            self._commit_edit()
            self.state = self.store.save(self.state, self.state["revision"])
            return True
        except Exception as error:
            self.status.setText("保存失败：" + str(error))
            self._log("保存失败：" + traceback.format_exc())
            return False

    def _selection_changed(self, row, *_args):
        if self._loading:
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
        self.voice_label.setText("当前人声：" + (Path(job["voice_path"]).name if job.get("voice_path") else "未指定"))
        self.voice_label.setToolTip(job.get("voice_path", ""))
        self._loading = False
        self.request_background(job["path"])
        self.update_preview()
        self.refresh_editor_baseline()

    def update_preview(self):
        if self.is_batch_busy():
            return
        try:
            settings = self.read_settings()
            job = self.active_entry()
            if self._preview_path and self._preview_path[-1] != blur_strength(settings) and job.get("path"):
                self.request_background(job["path"])
            width, height = output_size(settings, self._media)
            self.preview.canvas_size = QtCore.QSize(width,height)
            self.layer_panel.set_canvas_size(width,height)
            self._sync_preview_layers()
            self.preview.cover = settings["fit"] == "cover"
            request = (self.title.toPlainText(), self.body.toPlainText(), width, height, settings)
            self._overlay_request = request
            has_images = any(layer["kind"] in {"image", "text"} and layer["enabled"] and layer["opacity"] > 0 for layer in settings["layers"])
            if self.overlay_worker:
                self._overlay_pending = request
            elif has_images:
                self._start_overlay(request)
            else:
                image, info = render_preview_overlay(*request)
                self._overlay_ready({"request": request, "image": image, "info": info})
            if settings["use_voice"] and self._voice_duration:
                text = f"成品跟随人声：{self._voice_duration:.2f}秒"
                if self._media:
                    plan = adaptation_plan(self._media["duration"], self._voice_duration, settings, source_fps(self._media))
                    text += f"；背景{self._media['duration']:.2f}秒 / {plan['speed']:.2f}×"
                    if plan["repeating"]:
                        text += " / 循环" + ("叠化" if plan["fade"] else "直切")
                self.duration_status.setText(text)
            else:
                self.duration_status.setText("请导入人声。" if settings["use_voice"] else "未启用任务人声，保持原视频长度。")
        except Exception as error:
            self.preview.overlay = QtGui.QImage()
            self._overlay_status = str(error)
            self._update_preview_status()
        self.preview.update()

    def _start_overlay(self, request):
        self.overlay_worker = OverlayWorker(request, self._image_cache, self)
        self.overlay_worker.ready.connect(self._overlay_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        self.overlay_worker.finished.connect(self._overlay_finished)
        self.overlay_worker.start()

    def _overlay_ready(self, result):
        if result["request"] != self._overlay_request:
            return
        if result.get("error"):
            self.preview.overlay = QtGui.QImage()
            self._overlay_status = "图层预览失败：" + result["error"]
            self._log("图层预览失败：" + result["error"] + "\n" + result.get("traceback", ""))
        else:
            _, _, width, height, _ = result["request"]
            self.preview.overlay = result["image"]
            info = result["info"]
            sizes = info.get("component_sizes",{})
            text = ("文本框字号："+" / ".join(f"{size:.0f}" for size in list(sizes.values())[:3])
                    +(f"（共{len(sizes)}个）" if len(sizes)>3 else "")) if sizes else (
                    f"自适应字号：标题 {info['title_size']:.0f} / 正文 {info['body_size']:.0f}")
            warnings = result["info"].get("warnings",[])
            self._overlay_status = ("需调整（导出仍会阻止）："+"；".join(warnings)) if warnings else f"{text}；{width}×{height}（背景首帧）"
            warning = "；".join(warnings)
            if warning and warning != self._last_preview_warning:
                self._log("预览排版提示："+warning)
            self._last_preview_warning = warning
        self._update_preview_status()
        self.preview.update()

    def _update_preview_status(self):
        self.preview_status.setText("\n".join(part for part in (self._background_error,self._overlay_status) if part))

    def refresh_preview(self):
        if self.is_batch_busy():
            return
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

    def request_background(self, path, force=False):
        job = self.active_entry()
        voice = job.get("voice_path", "") if self.controls["use_voice"].isChecked() else ""
        request = (path, voice, self.ffmpeg.text().strip())
        frame_settings = {key:self.controls[key].currentData() for key in ("aspect","fit")}
        request += (frame_settings["aspect"],frame_settings["fit"],blur_strength({"background_blur":self.controls["background_blur"].value()}))
        if self.preview_worker is not None:
            self._preview_pending = request
            self._force_preview_pending = self._force_preview_pending or force
            return
        if self._preview_path == request and not force:
            return
        same_media = self._preview_path and self._preview_path[:3] == request[:3]
        if not same_media or force:
            self.invalidate_playback()
        self._preview_path = request
        if not same_media:
            self._media = self._voice_duration = None
            self.preview.background = QtGui.QImage()
        self._background_error = ""
        self.preview_worker = PreviewWorker(path, self.ffmpeg.text().strip(), self, voice_path=voice, blur=request[-1], frame_settings=frame_settings)
        self.preview_worker.ready.connect(self._background_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        self.preview_worker.finished.connect(self._background_finished)
        self.preview_worker.start()

    def _background_ready(self, result):
        job = self.active_entry()
        voice = job.get("voice_path", "") if self.controls["use_voice"].isChecked() else ""
        if (result["path"], result.get("voice_path", ""), result.get("configured", ""),
                result.get("aspect",self.controls["aspect"].currentData()),result.get("fit",self.controls["fit"].currentData()),result.get("blur",0)) != (
                job.get("path", ""), voice, self.ffmpeg.text().strip(),self.controls["aspect"].currentData(),
                self.controls["fit"].currentData(),self.controls["background_blur"].value()):
            return
        if result.get("error"):
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
        self.jobs.populate([(job["id"], [job.get("label") or Path(job["path"]).stem,
            Path(job["path"]).name if job["path"] else "未分配",
            Path(job["voice_path"]).name if job.get("voice_path") else "无", job["status"]],
            "\n".join(part for part in (job["path"],job.get("voice_path"),job.get("error"),job.get("task_dir")) if part), "")
            for job in self.state["jobs"]])
        music_rows = []
        for row, item in enumerate(self.state["music"]):
            prefix = "→ " if item["id"] == self.state["next_music_id"] or not self.state["next_music_id"] and row == 0 else ""
            music_rows.append((item["id"], [prefix+Path(item["path"]).name,
                f"{item.get('cursor',0):.1f}秒",str(item.get("last_used") or "未使用")],item["path"],""))
        self.music.populate(music_rows)
        if current:
            row = next((i for i, job in enumerate(self.state["jobs"]) if job["id"] == current), -1)
            if row >= 0:
                self._show_job(self.state["jobs"][row])
        self.refresh_pools()
        self._sync_preview_layers()
        self._loading = False

    def add_files(self, paths):
        if self.is_batch_busy():
            return
        narration = [path for path in paths if Path(path).is_dir() or Path(path).suffix.casefold() in AUDIO_SUFFIXES and Path(path).stem.casefold() == "task_audio"]
        images = [path for path in paths if Path(path).suffix.casefold() in IMAGE_SUFFIXES]
        regular = [path for path in paths if path not in narration]
        self._commit_edit()
        videos = [path for path in regular if Path(path).suffix.casefold() in VIDEO_SUFFIXES]
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
        self.status.setText(f"已添加{n_video}个视频、{n_music}首音乐；重复路径自动忽略。")
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
        self.import_worker.finished.connect(self._import_finished)
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
            job.update(voice_path=path, match_error="", error="", status="待生成")
            self.controls["use_voice"].setChecked(True)
            self.persist()
            self.refresh_tables()

    def add_videos(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加视频", "", "视频 (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mts)")
        self.add_files(paths)

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
        if QtWidgets.QMessageBox.question(self, "重置音乐进度", "把音乐轮换和每首音乐的播放位置重置到开头？") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.state["next_music_id"] = ""
        for item in self.state["music"]:
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
        if selected and self.tabs.currentWidget() != self.jobs:
            self.tabs.setCurrentWidget(self.jobs)
            self.status.setText("请在生成队列中多选任务后生成；资源池本身不是生成队列。")
            return
        ids = self.selected_ids() if selected else [job["id"] for job in self.state["jobs"] if job["status"] != "已完成"]
        if not ids:
            self.status.setText("没有待生成任务；导入资源后请先用“批量→搭配资源”加入队列。")
            return
        self._launch_batch(ids)

    def quick_export_current(self):
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
        self.progress_bar.setValue(0)
        self._launch_batch([self._quick_export_id])

    def _launch_batch(self, ids):
        self.reset_editor_history()
        self.progress_bar.setValue(0)
        self.worker = BatchWorker(self.store, self.state, ids, self)
        self.worker.message.connect(self._log, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.progress.connect(self._progress, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.jobDone.connect(self._job_done, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.result.connect(self._batch_result, QtCore.Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._batch_finished)
        self._set_work_controls(True)
        self.worker.start()

    @QtCore.pyqtSlot(str)
    def _log(self, message):
        self.status.setText(message.splitlines()[0][:300])
        if self.context:
            self.context.log(message, level=logging.ERROR if "Traceback" in message else logging.INFO)

    def _progress(self, _id, value):
        self.progress_bar.setValue(value)

    def _job_done(self, job_id, status):
        for row in range(self.jobs.rowCount()):
            if self.jobs.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) == job_id:
                self.jobs.item(row, 3).setText(status)

    def _batch_result(self, result):
        self.status.setText(result.get("error") or f"已完成{result['completed']}个，需处理{result['failed']}个。" +
                            ("已停止，剩余队列与音乐位置已保留。" if result.get("cancelled") else "音乐轮换进度已保存。"))

    def _batch_finished(self):
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
        return (self.worker is not None or self.import_worker is not None or self.layer_panel.worker is not None
                or self.image_import_worker is not None or isinstance(self._batch_after_preview, dict))

    def is_busy(self):
        return (self.is_batch_busy() or self.preview_worker is not None or self.overlay_worker is not None
                or self.playback_worker is not None
                or self.text_import_dialog is not None and self.text_import_dialog.worker is not None)

    def _set_work_controls(self, busy):
        self.splitter.setEnabled(not busy)
        self.menu_bar.setEnabled(not busy)
        for widget in (self.scheme,self.output,self.generate,self.selected):
            widget.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    def cancel_work(self):
        pending_quick = isinstance(self._batch_after_preview, dict)
        self._batch_after_preview = None
        self.stop_playback()
        self.preview.cancel_drag()
        self.geometry_preview_timer.stop()
        self._preview_pending = None
        self._overlay_pending = None
        self._force_preview_pending = False
        self._pending_layer_paths = []
        self.layer_panel.cancel_work()
        if self.image_import_worker:
            self.image_import_worker.cancel.set()
        if self.import_worker:
            self.import_worker.cancel.set()
        if self.worker:
            self.worker.cancel.set()
            self.stop_button.setEnabled(False)
            self.status.setText("正在停止当前视频；已完成的视频和音乐位置不受影响。")
        elif pending_quick:
            self._set_work_controls(False)
            self.status.setText("已取消快速导出，轮换进度未改变。")

    def closeEvent(self, event):
        pending_quick = isinstance(self._batch_after_preview, dict)
        self._batch_after_preview = None
        if pending_quick:
            self._set_work_controls(False)
        self.stop_playback()
        self.preview.cancel_drag()
        self.geometry_preview_timer.stop()
        if not self.is_batch_busy():
            self.edit_timer.stop()
            self.persist()
        self.hide()
        event.ignore()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self.is_batch_busy():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not self.is_batch_busy():
            self.add_files([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
            event.acceptProposedAction()
