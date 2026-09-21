import html

from PyQt5 import QtCore, QtGui, QtWidgets

from .engine import (
    _combined_detection_error,
    _issue_summary,
    _media_shape,
    _protect_edges_adjacent_to_missing_script,
    apply_manual_breath_overrides,
    breath_cut_plan,
    combine_breath_gap_ranges,
    find_pause_removals,
    kept_ranges,
    refine_trim_boundaries,
    select_duplicate_group_clip,
    smart_video_task_review_decision,
    subtract_time_ranges,
)
from .player import FfplayReviewSurface
from .timeline_model import (
    build_issue_markers,
    build_subtitle_tracks,
    build_task_review_timeline,
    source_location_for_output,
)
from .timeline_widget import SmartTimelineWidget, format_time
from .waveform import (
    SilenceBatchLoader,
    VoiceActivityBatchLoader,
    WaveformLoader,
)


class SmartVideoTimelineReview(QtWidgets.QWidget):
    """Player-first review page; the existing table remains the precision view."""

    clipSelected = QtCore.pyqtSignal(int, int)
    markerSelected = QtCore.pyqtSignal(object)
    acknowledgeRequested = QtCore.pyqtSignal(int, int)
    taskDecisionRequested = QtCore.pyqtSignal(int, str)
    openAdvancedRequested = QtCore.pyqtSignal()
    orderChanged = QtCore.pyqtSignal(int)

    def __init__(
        self,
        bundle,
        parent=None,
        save_subtitle_defaults=None,
    ):
        super().__init__(parent)
        self.bundle = bundle
        self._save_subtitle_defaults_callback = save_subtitle_defaults
        self.breath_only = str(bundle.get("workflow") or "") == "breath_cut"
        self.task_index = 0
        self.segments = []
        self.markers = []
        self.recognized_subtitles = []
        self.aligned_subtitles = []
        self.duration = 0.0
        self.output_position = 0.0
        self.current_segment = None
        self._loading_slider = False
        self._resume_after_seek = False
        self._preview_removed_once = False
        self._breath_reanalysis_running = False
        self._breath_db_results = None
        self._breath_voice_results = None
        self._manual_delete_anchor = None
        self._waveform_generation = 0
        self._media_generation = 0
        self._media_aspect_cache = {}

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("当前任务：", self))
        self.task_combo = QtWidgets.QComboBox(self)
        self.task_combo.currentIndexChanged.connect(self._load_task)
        toolbar.addWidget(self.task_combo, 1)
        self.move_clip_left_button = QtWidgets.QPushButton("片段前移", self)
        self.move_clip_right_button = QtWidgets.QPushButton("片段后移", self)
        self.move_clip_left_button.setToolTip("调整当前视频片段的最终导出顺序")
        self.move_clip_right_button.setToolTip("调整当前视频片段的最终导出顺序")
        toolbar.addWidget(self.move_clip_left_button)
        toolbar.addWidget(self.move_clip_right_button)
        order_hint = QtWidgets.QLabel("也可拖动视频块排序", self)
        order_hint.setStyleSheet("color:#616B7E;")
        order_hint.setToolTip("按住时间线中的彩色视频块，拖到另一个视频块上松开")
        toolbar.addWidget(order_hint)
        toolbar.addWidget(QtWidgets.QLabel("预览：", self))
        self.preview_aspect_combo = QtWidgets.QComboBox(self)
        self.preview_aspect_combo.addItem("自动", "auto")
        self.preview_aspect_combo.addItem("竖屏", "portrait")
        self.preview_aspect_combo.addItem("横屏", "landscape")
        self.preview_aspect_combo.setToolTip(
            "自动读取当前任务首个视频的分辨率；也可以手动切换预览区域比例。"
        )
        toolbar.addWidget(self.preview_aspect_combo)
        refresh_button = QtWidgets.QPushButton("刷新时间线", self)
        refresh_button.setToolTip("应用表格精调中的顺序、裁切和是否生成设置")
        refresh_button.clicked.connect(lambda: self.refresh(preserve_time=True))
        toolbar.addWidget(refresh_button)
        self.zoom_out_button = QtWidgets.QPushButton("缩小", self)
        self.zoom_fit_button = QtWidgets.QPushButton("适应全长", self)
        self.zoom_in_button = QtWidgets.QPushButton("放大", self)
        toolbar.addWidget(self.zoom_out_button)
        toolbar.addWidget(self.zoom_fit_button)
        toolbar.addWidget(self.zoom_in_button)
        root.addLayout(toolbar)

        self.task_filter_widget = QtWidgets.QWidget(self)
        task_filter_bar = QtWidgets.QHBoxLayout(self.task_filter_widget)
        task_filter_bar.setContentsMargins(0, 0, 0, 0)
        task_filter_bar.addWidget(QtWidgets.QLabel("任务筛选：", self))
        self.task_filter_combo = QtWidgets.QComboBox(self)
        self.task_filter_combo.addItem("全部任务", "all")
        self.task_filter_combo.addItem("仅未处理缺段", "unresolved")
        self.task_filter_combo.addItem("全部缺段任务", "missing")
        self.task_filter_combo.setToolTip(
            "只在任务下拉框中保留符合条件的任务，不会改变导出决定"
        )
        task_filter_bar.addWidget(self.task_filter_combo)
        self.missing_task_count_label = QtWidgets.QLabel("", self)
        self.missing_task_count_label.setStyleSheet(
            "color:#B3261E;font-weight:600;"
        )
        task_filter_bar.addWidget(self.missing_task_count_label)
        self.previous_missing_task_button = QtWidgets.QPushButton(
            "◀ 上个缺段", self
        )
        self.next_missing_task_button = QtWidgets.QPushButton(
            "下个缺段 ▶", self
        )
        task_filter_bar.addWidget(self.previous_missing_task_button)
        task_filter_bar.addWidget(self.next_missing_task_button)
        task_filter_bar.addStretch(1)
        root.addWidget(self.task_filter_widget)

        breath_panel = QtWidgets.QGroupBox(
            "气口检测（应用到当前视频）"
            if self.breath_only else "气口检测（应用到当前任务）",
            self,
        )
        self.breath_panel = breath_panel
        breath_layout = QtWidgets.QGridLayout(breath_panel)
        breath_layout.setContentsMargins(8, 5, 8, 5)
        self.breath_detection_mode_combo = QtWidgets.QComboBox(breath_panel)
        self.breath_detection_mode_combo.addItem("仅分贝（旧版）", "db_only")
        self.breath_detection_mode_combo.addItem("仅人声", "voice_only")
        self.breath_detection_mode_combo.addItem("并集（删除更多）", "union")
        self.breath_detection_mode_combo.addItem("交集（删除更少）", "intersection")
        self.breath_detection_mode_combo.addItem(
            "人声主导＋分贝修边", "voice_refined"
        )
        self.breath_detection_mode_combo.setToolTip(
            "并集：任一检测认为是气口就删除；交集：两种检测都确认才删除。"
        )
        self.silence_detection_checkbox = QtWidgets.QCheckBox(
            "启用分贝检测", breath_panel
        )
        self.silence_db_spinbox = QtWidgets.QSpinBox(breath_panel)
        self.silence_db_spinbox.setRange(-80, -5)
        self.silence_db_spinbox.setSuffix(" dB")
        self.min_silence_spinbox = QtWidgets.QSpinBox(breath_panel)
        self.min_silence_spinbox.setRange(80, 5000)
        self.min_silence_spinbox.setSingleStep(50)
        self.min_silence_spinbox.setPrefix("静音≥")
        self.min_silence_spinbox.setSuffix(" ms")
        self.voice_detection_checkbox = QtWidgets.QCheckBox(
            "启用人声检测", breath_panel
        )
        self.vad_threshold_spinbox = QtWidgets.QDoubleSpinBox(breath_panel)
        self.vad_threshold_spinbox.setRange(0.10, 0.95)
        self.vad_threshold_spinbox.setDecimals(2)
        self.vad_threshold_spinbox.setSingleStep(0.05)
        self.vad_threshold_spinbox.setToolTip(
            "越高越不容易把环境音当成人声，裁切越积极；太高可能误伤轻声。"
        )
        self.vad_neg_threshold_spinbox = QtWidgets.QDoubleSpinBox(breath_panel)
        self.vad_neg_threshold_spinbox.setRange(0.01, 0.94)
        self.vad_neg_threshold_spinbox.setDecimals(2)
        self.vad_neg_threshold_spinbox.setSingleStep(0.05)
        self.vad_neg_threshold_spinbox.setToolTip(
            "人声段的结束阈值，必须低于开始阈值。"
        )
        self.vad_min_speech_spinbox = QtWidgets.QSpinBox(breath_panel)
        self.vad_min_speech_spinbox.setRange(0, 2000)
        self.vad_min_speech_spinbox.setSingleStep(20)
        self.vad_min_speech_spinbox.setSuffix(" ms")
        self.vad_min_speech_spinbox.setToolTip(
            "比它更短的疑似人声按杂音处理；过大会漏掉很短的词。"
        )
        self.vad_speech_pad_spinbox = QtWidgets.QSpinBox(breath_panel)
        self.vad_speech_pad_spinbox.setRange(0, 1500)
        self.vad_speech_pad_spinbox.setSingleStep(20)
        self.vad_speech_pad_spinbox.setSuffix(" ms")
        self.vad_speech_pad_spinbox.setToolTip(
            "人声两侧保护量；过大会把环境音也一起保留。"
        )
        self.compress_pauses_checkbox = QtWidgets.QCheckBox(
            "压缩句内长停顿", breath_panel
        )
        self.compress_pauses_checkbox.setToolTip(
            "句内气口存在误删说话的风险；时间线会把计划删除部分显示为灰色。"
        )
        self.pause_threshold_spinbox = QtWidgets.QSpinBox(breath_panel)
        self.pause_threshold_spinbox.setRange(300, 10000)
        self.pause_threshold_spinbox.setSingleStep(100)
        self.pause_threshold_spinbox.setSuffix(" ms")
        self.pause_keep_before_spinbox = QtWidgets.QSpinBox(breath_panel)
        self.pause_keep_before_spinbox.setRange(0, 2500)
        self.pause_keep_before_spinbox.setSingleStep(50)
        self.pause_keep_before_spinbox.setSuffix(" ms")
        self.pause_keep_after_spinbox = QtWidgets.QSpinBox(breath_panel)
        self.pause_keep_after_spinbox.setRange(0, 2500)
        self.pause_keep_after_spinbox.setSingleStep(50)
        self.pause_keep_after_spinbox.setSuffix(" ms")
        self.apply_breath_button = QtWidgets.QPushButton(
            "检测并应用", breath_panel
        )
        self.apply_breath_button.setToolTip(
            "按当前参数重新检测，并立即更新灰色删除区。"
        )
        self.apply_breath_button.setMaximumWidth(110)
        self.mark_delete_start_button = QtWidgets.QPushButton(
            "记删除起点", breath_panel
        )
        self.mark_delete_end_button = QtWidgets.QPushButton(
            "记终点并添加", breath_panel
        )
        self.restore_removed_button = QtWidgets.QPushButton(
            "恢复当前灰色区", breath_panel
        )
        self.clear_manual_button = QtWidgets.QPushButton(
            "清除当前片段手调", breath_panel
        )
        self.mark_delete_start_button.setToolTip(
            "在当前播放头记下起点，再移动播放头并点击“记终点并添加”。"
        )
        self.restore_removed_button.setToolTip(
            "把播放头所在的灰色删除区恢复为保留区。"
        )
        self.breath_status_label = QtWidgets.QLabel("", breath_panel)
        self.breath_status_label.setStyleSheet("color:#616B7E;")
        breath_layout.addWidget(QtWidgets.QLabel("判断方式", breath_panel), 0, 0)
        breath_layout.addWidget(self.breath_detection_mode_combo, 0, 1, 1, 2)
        breath_layout.addWidget(self.silence_detection_checkbox, 0, 3)
        breath_layout.addWidget(self.silence_db_spinbox, 0, 4)
        breath_layout.addWidget(self.min_silence_spinbox, 0, 5)
        breath_layout.addWidget(self.voice_detection_checkbox, 1, 0)
        breath_layout.addWidget(QtWidgets.QLabel("开始阈值", breath_panel), 1, 1)
        breath_layout.addWidget(self.vad_threshold_spinbox, 1, 2)
        breath_layout.addWidget(QtWidgets.QLabel("保护", breath_panel), 1, 3)
        breath_layout.addWidget(self.vad_speech_pad_spinbox, 1, 4)
        breath_layout.addWidget(QtWidgets.QLabel("结束阈值", breath_panel), 2, 0)
        breath_layout.addWidget(self.vad_neg_threshold_spinbox, 2, 1)
        breath_layout.addWidget(QtWidgets.QLabel("最短人声", breath_panel), 2, 2)
        breath_layout.addWidget(self.vad_min_speech_spinbox, 2, 3)
        breath_layout.addWidget(self.compress_pauses_checkbox, 3, 0)
        breath_layout.addWidget(self.pause_threshold_spinbox, 3, 1)
        breath_layout.addWidget(QtWidgets.QLabel("前留", breath_panel), 3, 2)
        breath_layout.addWidget(self.pause_keep_before_spinbox, 3, 3)
        breath_layout.addWidget(QtWidgets.QLabel("后留", breath_panel), 3, 4)
        breath_layout.addWidget(self.pause_keep_after_spinbox, 3, 5)
        breath_layout.addWidget(
            self.apply_breath_button, 0, 6, 1, 1, QtCore.Qt.AlignRight
        )
        breath_layout.addWidget(self.mark_delete_start_button, 4, 0)
        breath_layout.addWidget(self.mark_delete_end_button, 4, 1)
        breath_layout.addWidget(self.restore_removed_button, 4, 2, 1, 2)
        breath_layout.addWidget(self.clear_manual_button, 4, 4, 1, 2)
        breath_layout.addWidget(self.breath_status_label, 5, 0, 1, 8)
        # Only the empty tail column may absorb spare window width.  Giving
        # stretch to a parameter column makes its spin box look like an
        # oversized text field and merely moves the original layout problem.
        breath_layout.setColumnStretch(7, 1)
        root.addWidget(breath_panel)
        self._load_breath_controls()

        body = QtWidgets.QSplitter(QtCore.Qt.Horizontal, self)
        self.body_splitter = body
        preview_column = QtWidgets.QWidget(body)
        self.preview_column = preview_column
        preview_column.setMinimumWidth(230)
        preview_layout = QtWidgets.QVBoxLayout(preview_column)
        preview_layout.setContentsMargins(0, 0, 5, 0)
        largest_task = max(
            (
                len(task.get("clips", []) or [])
                for task in self.bundle.get("tasks", []) or []
            ),
            default=0,
        )
        self.player = FfplayReviewSurface(
            self.bundle.get("settings", {}).get("ffmpeg_path", ""),
            preview_column,
            # A standby native decoder makes short timelines switch smoothly,
            # but with many independent source files it noticeably raises GPU
            # decoder pressure and can crash inside Windows multimedia code.
            preload_enabled=largest_task <= 8,
        )
        self.player.positionChanged.connect(self._player_position_changed)
        self.player.playingChanged.connect(self._player_state_changed)
        self.player.rangeFinished.connect(self._play_next_range)
        self.player.errorOccurred.connect(self._show_player_error)
        self.player.setMinimumSize(210, 360)
        preview_layout.addWidget(self.player, 1)

        playback_buttons = QtWidgets.QHBoxLayout()
        self.play_button = QtWidgets.QPushButton("▶ 播放", preview_column)
        self.previous_issue_button = QtWidgets.QPushButton(
            "◀ 上一问题", preview_column
        )
        self.next_issue_button = QtWidgets.QPushButton(
            "下一问题 ▶", preview_column
        )
        self.seek_slider = QtWidgets.QSlider(
            QtCore.Qt.Horizontal, preview_column
        )
        self.seek_slider.setRange(0, 1)
        self.time_label = QtWidgets.QLabel(
            "00:00.00 / 00:00.00", preview_column
        )
        self.time_label.setAlignment(QtCore.Qt.AlignCenter)
        self.remaining_duration_label = QtWidgets.QLabel(
            "裁剪后：00:00.00", preview_column
        )
        self.remaining_duration_label.setAlignment(QtCore.Qt.AlignCenter)
        self.remaining_duration_label.setStyleSheet(
            "color:#3D526A;font-weight:600;"
        )
        playback_buttons.addWidget(self.play_button)
        playback_buttons.addWidget(self.previous_issue_button)
        playback_buttons.addWidget(self.next_issue_button)
        preview_layout.addLayout(playback_buttons)
        preview_layout.addWidget(self.seek_slider)
        preview_layout.addWidget(self.time_label)
        preview_layout.addWidget(self.remaining_duration_label)

        timeline_column = QtWidgets.QWidget(body)
        timeline_column.setMinimumWidth(480)
        timeline_layout = QtWidgets.QVBoxLayout(timeline_column)
        timeline_layout.setContentsMargins(5, 0, 5, 0)

        legend = QtWidgets.QLabel(
            "绿色＝通过　橙色＝需核对　粉色＝严重异常　"
            "灰色＝导出时删除　红色缺口＝原文没有对应视频。"
            "点击灰色区可试听，双击可取消该段删除；点击字幕块可查看差异。",
            timeline_column,
        )
        legend.setStyleSheet("color:#616B7E;")
        legend.setWordWrap(True)
        timeline_layout.addWidget(legend)

        self.subtitle_settings_panel = QtWidgets.QGroupBox(
            "字幕参数（即时预览 / 最终 SRT）", timeline_column
        )
        subtitle_layout = QtWidgets.QGridLayout(self.subtitle_settings_panel)
        subtitle_layout.setContentsMargins(8, 5, 8, 5)
        self.subtitle_model_combo = QtWidgets.QComboBox(
            self.subtitle_settings_panel
        )
        self.subtitle_model_combo.addItem("base", "base")
        self.subtitle_model_combo.addItem("small", "small")
        self.subtitle_model_combo.addItem("medium", "medium")
        self.subtitle_model_combo.addItem("large-v3", "large-v3")
        self.subtitle_model_combo.setToolTip(
            "无需重启。模型会在下一次重新分析或最终字幕对齐时于后台切换；"
            "已经生成的识别轨不会凭空改变。"
        )
        self.subtitle_max_words_spinbox = QtWidgets.QSpinBox(
            self.subtitle_settings_panel
        )
        self.subtitle_max_words_spinbox.setRange(0, 50)
        self.subtitle_max_words_spinbox.setSpecialValueText("不限")
        self.subtitle_max_chars_spinbox = QtWidgets.QSpinBox(
            self.subtitle_settings_panel
        )
        self.subtitle_max_chars_spinbox.setRange(0, 500)
        self.subtitle_max_chars_spinbox.setSpecialValueText("不限")
        self.subtitle_gap_spinbox = QtWidgets.QSpinBox(
            self.subtitle_settings_panel
        )
        self.subtitle_gap_spinbox.setRange(-1, 5000)
        self.subtitle_gap_spinbox.setSpecialValueText("保留")
        self.subtitle_gap_spinbox.setSuffix(" ms")
        self.subtitle_line_break_checkbox = QtWidgets.QCheckBox(
            "字幕块内允许换行", self.subtitle_settings_panel
        )
        self.save_subtitle_defaults_button = QtWidgets.QPushButton(
            "保存为全局默认", self.subtitle_settings_panel
        )
        self.save_subtitle_defaults_button.setToolTip(
            "同步到主界面字幕参数和智能剪辑默认设置，以后新任务继续使用。"
        )
        self.subtitle_settings_status = QtWidgets.QLabel(
            "修改后立即重排时间线字幕块；不保存则只影响本次。",
            self.subtitle_settings_panel,
        )
        self.subtitle_settings_status.setStyleSheet("color:#616B7E;")
        subtitle_layout.addWidget(
            QtWidgets.QLabel("模型", self.subtitle_settings_panel), 0, 0
        )
        subtitle_layout.addWidget(self.subtitle_model_combo, 0, 1)
        subtitle_layout.addWidget(
            QtWidgets.QLabel("每块单词", self.subtitle_settings_panel), 0, 2
        )
        subtitle_layout.addWidget(self.subtitle_max_words_spinbox, 0, 3)
        subtitle_layout.addWidget(
            QtWidgets.QLabel("每块字符", self.subtitle_settings_panel), 0, 4
        )
        subtitle_layout.addWidget(self.subtitle_max_chars_spinbox, 0, 5)
        subtitle_layout.addWidget(
            QtWidgets.QLabel("块间隔", self.subtitle_settings_panel), 0, 6
        )
        subtitle_layout.addWidget(self.subtitle_gap_spinbox, 0, 7)
        subtitle_layout.addWidget(
            self.subtitle_line_break_checkbox, 1, 0, 1, 2
        )
        subtitle_layout.addWidget(self.subtitle_settings_status, 1, 2, 1, 4)
        subtitle_layout.addWidget(
            self.save_subtitle_defaults_button,
            1,
            6,
            1,
            2,
            QtCore.Qt.AlignRight,
        )
        subtitle_layout.setColumnStretch(8, 1)
        timeline_layout.addWidget(self.subtitle_settings_panel)
        self._load_subtitle_controls()

        self.timeline_scroll = QtWidgets.QScrollArea(timeline_column)
        self.timeline_scroll.setWidgetResizable(False)
        self.timeline_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.timeline_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.timeline_scroll.setMinimumHeight(278)
        self.timeline_scroll.setMaximumHeight(290)
        self.timeline = SmartTimelineWidget()
        self.timeline.seekRequested.connect(self._timeline_seek)
        self.timeline.clipActivated.connect(self._timeline_clip_activated)
        self.timeline.clipReorderRequested.connect(self._move_clip_to)
        self.timeline.removedSegmentRestoreRequested.connect(
            self._restore_removed_segment
        )
        self.timeline.markerActivated.connect(self._marker_activated)
        self.timeline.subtitleActivated.connect(self._subtitle_activated)
        self.timeline_scroll.setWidget(self.timeline)
        self.timeline_scroll.viewport().installEventFilter(self)
        timeline_layout.addWidget(self.timeline_scroll)

        comparison = QtWidgets.QGroupBox(
            "当前片段文案核对", timeline_column
        )
        self.comparison_panel = comparison
        comparison_layout = QtWidgets.QGridLayout(comparison)
        comparison_layout.addWidget(QtWidgets.QLabel("正确原文", comparison), 0, 0)
        comparison_layout.addWidget(QtWidgets.QLabel("视频识别", comparison), 0, 1)
        self.expected_text = QtWidgets.QPlainTextEdit(comparison)
        self.expected_text.setReadOnly(True)
        self.recognized_text = QtWidgets.QPlainTextEdit(comparison)
        self.recognized_text.setReadOnly(True)
        self.expected_text.setMaximumHeight(58)
        self.recognized_text.setMaximumHeight(58)
        comparison_layout.addWidget(self.expected_text, 1, 0)
        comparison_layout.addWidget(self.recognized_text, 1, 1)
        comparison.setMaximumHeight(94)
        timeline_layout.addWidget(comparison)

        right = QtWidgets.QWidget(body)
        self.issue_panel = right
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(5, 0, 0, 0)
        issue_title_bar = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("问题与字幕修改建议", right)
        title.setStyleSheet("font-weight:600;")
        issue_title_bar.addWidget(title)
        issue_title_bar.addStretch(1)
        issue_title_bar.addWidget(QtWidgets.QLabel("显示：", right))
        self.issue_filter_combo = QtWidgets.QComboBox(right)
        self.issue_filter_combo.addItem("全部问题", "all")
        self.issue_filter_combo.addItem("只看确认缺段", "missing")
        self.issue_filter_combo.addItem("只看严重异常", "pink")
        self.issue_filter_combo.addItem("只看需核对", "orange")
        issue_title_bar.addWidget(self.issue_filter_combo)
        right_layout.addLayout(issue_title_bar)
        self.issue_tree = QtWidgets.QTreeWidget(right)
        self.issue_tree.setColumnCount(3)
        self.issue_tree.setHeaderLabels(["时间", "级别", "问题"])
        self.issue_tree.setRootIsDecorated(False)
        self.issue_tree.setAlternatingRowColors(True)
        self.issue_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.issue_tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.issue_tree.header().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.issue_tree.currentItemChanged.connect(self._issue_selection_changed)
        self.issue_tree.itemDoubleClicked.connect(
            lambda _item, _column: self.play_selected_issue()
        )
        right_layout.addWidget(self.issue_tree, 1)
        self.issue_detail = QtWidgets.QTextBrowser(right)
        self.issue_detail.setMinimumHeight(145)
        self.issue_detail.setMaximumHeight(210)
        right_layout.addWidget(self.issue_detail)
        issue_actions = QtWidgets.QHBoxLayout()
        self.play_issue_button = QtWidgets.QPushButton("播放问题附近", right)
        self.acknowledge_button = QtWidgets.QPushButton("✓ 当前片段没问题", right)
        issue_actions.addWidget(self.play_issue_button)
        issue_actions.addWidget(self.acknowledge_button)
        right_layout.addLayout(issue_actions)
        self.duplicate_group_panel = QtWidgets.QGroupBox(
            "重复片段（单选保留）", right
        )
        self.duplicate_group_panel.setToolTip(
            "同组只能保留一个版本；切换后会占用原片段的正确位置，不会追加到片尾。"
        )
        self.duplicate_group_layout = QtWidgets.QVBoxLayout(
            self.duplicate_group_panel
        )
        self.duplicate_group_layout.setContentsMargins(8, 5, 8, 5)
        self.duplicate_group_hint = QtWidgets.QLabel(
            "请选择最终使用的一个版本：", self.duplicate_group_panel
        )
        self.duplicate_group_hint.setStyleSheet("color:#616B7E;")
        self.duplicate_group_layout.addWidget(self.duplicate_group_hint)
        self.duplicate_choices_widget = QtWidgets.QWidget(
            self.duplicate_group_panel
        )
        self.duplicate_choices_layout = QtWidgets.QVBoxLayout(
            self.duplicate_choices_widget
        )
        self.duplicate_choices_layout.setContentsMargins(0, 0, 0, 0)
        self.duplicate_choices_layout.setSpacing(2)
        self.duplicate_group_layout.addWidget(self.duplicate_choices_widget)
        self.duplicate_button_group = QtWidgets.QButtonGroup(self)
        self.duplicate_button_group.setExclusive(True)
        self.duplicate_group_panel.setMaximumHeight(180)
        self.duplicate_group_panel.hide()
        right_layout.addWidget(self.duplicate_group_panel)
        task_decision = QtWidgets.QGroupBox("当前任务缺段决定", right)
        self.task_decision_panel = task_decision
        task_decision_layout = QtWidgets.QVBoxLayout(task_decision)
        self.task_decision_status = QtWidgets.QLabel("", task_decision)
        self.task_decision_status.setWordWrap(True)
        task_decision_layout.addWidget(self.task_decision_status)
        decision_actions = QtWidgets.QHBoxLayout()
        self.approve_task_button = QtWidgets.QPushButton(
            "确认完整，继续生成", task_decision
        )
        self.skip_task_button = QtWidgets.QPushButton(
            "跳过当前任务", task_decision
        )
        self.clear_task_decision_button = QtWidgets.QPushButton(
            "清除决定", task_decision
        )
        self.approve_task_button.setToolTip(
            "试听后确认是识别误报，允许这个任务继续生成"
        )
        self.skip_task_button.setToolTip(
            "本次不生成这个任务，保留到待处理智能剪辑中"
        )
        decision_actions.addWidget(self.approve_task_button)
        decision_actions.addWidget(self.skip_task_button)
        decision_actions.addWidget(self.clear_task_decision_button)
        task_decision_layout.addLayout(decision_actions)
        right_layout.addWidget(task_decision)
        advanced_button = QtWidgets.QPushButton("进入表格精调 / 处理缺段决定", right)
        self.advanced_button = advanced_button
        advanced_button.setToolTip(
            "修改顺序、起止时间、是否生成，或对确认缺段选择人工通过/暂缓"
        )
        advanced_button.clicked.connect(self.openAdvancedRequested)
        right_layout.addWidget(advanced_button)

        body.addWidget(preview_column)
        body.addWidget(timeline_column)
        body.addWidget(right)
        body.setSizes([310, 740, 390])
        root.addWidget(body, 1)
        self._detected_preview_aspect = "portrait"
        self.preview_aspect_combo.currentIndexChanged.connect(
            self._apply_preview_aspect
        )

        self.play_button.clicked.connect(self.toggle_playback)
        self.previous_issue_button.clicked.connect(lambda: self.jump_issue(-1))
        self.next_issue_button.clicked.connect(lambda: self.jump_issue(1))
        self.task_filter_combo.currentIndexChanged.connect(
            self._task_filter_changed
        )
        self.previous_missing_task_button.clicked.connect(
            lambda: self._jump_missing_task(-1)
        )
        self.next_missing_task_button.clicked.connect(
            lambda: self._jump_missing_task(1)
        )
        self.issue_filter_combo.currentIndexChanged.connect(
            self._fill_issue_tree
        )
        self.play_issue_button.clicked.connect(self.play_selected_issue)
        self.acknowledge_button.clicked.connect(self._acknowledge_current_clip)
        self.approve_task_button.clicked.connect(
            lambda: self._request_task_decision("approved")
        )
        self.skip_task_button.clicked.connect(
            lambda: self._request_task_decision("skipped")
        )
        self.clear_task_decision_button.clicked.connect(
            lambda: self._request_task_decision("")
        )
        self.seek_slider.sliderPressed.connect(self._slider_pressed)
        self.seek_slider.sliderMoved.connect(self._slider_moved)
        self.seek_slider.sliderReleased.connect(self._slider_released)
        self.zoom_out_button.clicked.connect(
            lambda: self.timeline.set_zoom(self.timeline.zoom / 1.5)
        )
        self.zoom_fit_button.clicked.connect(lambda: self.timeline.set_zoom(1.0))
        self.zoom_in_button.clicked.connect(
            lambda: self.timeline.set_zoom(self.timeline.zoom * 1.5)
        )
        self.move_clip_left_button.clicked.connect(lambda: self._move_current_clip(-1))
        self.move_clip_right_button.clicked.connect(lambda: self._move_current_clip(1))
        self.apply_breath_button.clicked.connect(self._apply_breath_settings)
        self.mark_delete_start_button.clicked.connect(
            self._mark_manual_delete_start
        )
        self.mark_delete_end_button.clicked.connect(
            self._finish_manual_delete_range
        )
        self.restore_removed_button.clicked.connect(
            self._restore_current_removed_range
        )
        self.clear_manual_button.clicked.connect(
            self._clear_current_manual_edits
        )
        self._subtitle_refresh_timer = QtCore.QTimer(self)
        self._subtitle_refresh_timer.setSingleShot(True)
        self._subtitle_refresh_timer.setInterval(100)
        self._subtitle_refresh_timer.timeout.connect(
            self._apply_subtitle_controls
        )
        self.subtitle_model_combo.currentIndexChanged.connect(
            self._subtitle_controls_changed
        )
        self.subtitle_max_words_spinbox.valueChanged.connect(
            self._subtitle_controls_changed
        )
        self.subtitle_max_chars_spinbox.valueChanged.connect(
            self._subtitle_controls_changed
        )
        self.subtitle_gap_spinbox.valueChanged.connect(
            self._subtitle_controls_changed
        )
        self.subtitle_line_break_checkbox.toggled.connect(
            self._subtitle_controls_changed
        )
        self.save_subtitle_defaults_button.clicked.connect(
            self._save_subtitle_defaults
        )
        self.breath_detection_mode_combo.currentIndexChanged.connect(
            self._update_breath_control_state
        )
        self.silence_detection_checkbox.toggled.connect(
            self._update_breath_control_state
        )
        self.voice_detection_checkbox.toggled.connect(
            self._update_breath_control_state
        )
        self.compress_pauses_checkbox.toggled.connect(
            self._update_breath_control_state
        )
        self._space_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self)
        self._space_shortcut.activated.connect(self.toggle_playback)
        ffmpeg_path = self.bundle.get("settings", {}).get("ffmpeg_path", "")
        self.waveform_loader = WaveformLoader(ffmpeg_path, self)
        self.waveform_loader.waveformReady.connect(self.timeline.set_waveform)
        self.waveform_loader.loadingChanged.connect(self._waveform_loading_changed)
        self.waveform_loader.errorOccurred.connect(self._waveform_error)
        self.silence_loader = SilenceBatchLoader(ffmpeg_path, self)
        self.silence_loader.progressChanged.connect(self._breath_progress)
        self.silence_loader.completed.connect(self._breath_db_complete)
        self.voice_loader = VoiceActivityBatchLoader(self)
        self.voice_loader.progressChanged.connect(self._breath_progress)
        self.voice_loader.completed.connect(self._breath_voice_complete)
        if self.breath_only:
            self.task_filter_widget.hide()
            self.move_clip_left_button.hide()
            self.move_clip_right_button.hide()
            order_hint.hide()
            self.previous_issue_button.hide()
            self.next_issue_button.hide()
            self.comparison_panel.hide()
            self.issue_panel.hide()
            self.subtitle_settings_panel.hide()
            legend.setText(
                "灰色＝导出时删除，彩色＝保留。点击灰色区可试听原气口；"
                "双击灰色区可单独取消删除；正常播放会自动跳过灰色区。"
            )
            body.setSizes([1, 0])
        self._update_breath_control_state()
        self.refresh()

    def eventFilter(self, watched, event):
        if watched is self.timeline_scroll.viewport() and event.type() == QtCore.QEvent.Resize:
            self.timeline.set_viewport_width(event.size().width())
        return super().eventFilter(watched, event)

    def _load_subtitle_controls(self):
        settings = self.bundle.get("settings", {})
        model_index = self.subtitle_model_combo.findData(
            settings.get("whisper_model_size", "base")
        )
        self.subtitle_model_combo.setCurrentIndex(
            model_index if model_index >= 0 else 0
        )
        self.subtitle_max_words_spinbox.setValue(
            int(settings.get("srt_max_words_per_block", 0) or 0)
        )
        self.subtitle_max_chars_spinbox.setValue(
            int(settings.get("srt_max_chars_per_block", 0) or 0)
        )
        self.subtitle_gap_spinbox.setValue(
            int(settings.get("srt_block_gap_ms", -1))
        )
        self.subtitle_line_break_checkbox.setChecked(
            bool(settings.get("srt_include_line_breaks", False))
        )

    def _subtitle_settings_from_controls(self):
        return {
            "whisper_model_size": str(
                self.subtitle_model_combo.currentData() or "base"
            ),
            "srt_max_words_per_block": (
                self.subtitle_max_words_spinbox.value()
            ),
            "srt_max_chars_per_block": (
                self.subtitle_max_chars_spinbox.value()
            ),
            "srt_block_gap_ms": self.subtitle_gap_spinbox.value(),
            "srt_include_line_breaks": (
                self.subtitle_line_break_checkbox.isChecked()
            ),
        }

    def _subtitle_controls_changed(self, *_args):
        self.subtitle_settings_status.setStyleSheet("color:#616B7E;")
        self._subtitle_refresh_timer.start()

    def apply_pending_subtitle_settings(self):
        """Commit visible controls before the parent collects the bundle."""
        if self._subtitle_refresh_timer.isActive():
            self._subtitle_refresh_timer.stop()
        self._apply_subtitle_controls()

    def _apply_subtitle_controls(self):
        previous_model = str(
            self.bundle.get("settings", {}).get("whisper_model_size", "base")
        )
        values = self._subtitle_settings_from_controls()
        self.bundle.setdefault("settings", {}).update(values)
        if not self.segments or not self.task_combo.count():
            return
        task = self.bundle["tasks"][self.task_index]
        self.recognized_subtitles, self.aligned_subtitles = (
            build_subtitle_tracks(
                task,
                self.segments,
                settings=self.bundle.get("settings", {}),
            )
        )
        self.timeline.set_data(
            self.segments,
            self.markers,
            self.duration,
            self.recognized_subtitles,
            self.aligned_subtitles,
        )
        self.timeline.set_playhead(self.output_position)
        if values["whisper_model_size"] != previous_model:
            self.subtitle_settings_status.setText(
                f"已选择 {values['whisper_model_size']}；最终对齐会在后台切换模型。"
            )
        else:
            self.subtitle_settings_status.setText(
                "当前任务的字幕分块预览已更新；尚未修改全局默认。"
            )

    def _save_subtitle_defaults(self):
        # Flush a pending 100 ms preview update before saving the controls.
        self.apply_pending_subtitle_settings()
        callback = self._save_subtitle_defaults_callback
        if callback is None:
            self.subtitle_settings_status.setText(
                "当前入口不提供全局保存；本次参数仍会用于导出。"
            )
            return
        try:
            saved, message = callback(
                self._subtitle_settings_from_controls()
            )
        except Exception as error:
            saved, message = False, f"保存失败：{error}"
        self.subtitle_settings_status.setText(str(message or ""))
        self.subtitle_settings_status.setStyleSheet(
            "color:#176B35;font-weight:600;"
            if saved else "color:#B3261E;font-weight:600;"
        )

    def _load_breath_controls(self):
        settings = self.bundle.get("settings", {})
        mode_index = self.breath_detection_mode_combo.findData(
            settings.get("breath_detection_mode", "voice_refined")
        )
        self.breath_detection_mode_combo.setCurrentIndex(
            mode_index if mode_index >= 0 else 0
        )
        self.silence_detection_checkbox.setChecked(
            bool(settings.get("silence_detection_enabled", True))
        )
        self.silence_db_spinbox.setValue(
            int(settings.get("silence_threshold_db", -35))
        )
        self.voice_detection_checkbox.setChecked(
            bool(settings.get("voice_detection_enabled", True))
        )
        self.vad_threshold_spinbox.setValue(
            float(settings.get("vad_threshold", 0.65))
        )
        self.vad_neg_threshold_spinbox.setValue(
            float(settings.get("vad_neg_threshold", 0.50))
        )
        self.vad_min_speech_spinbox.setValue(
            int(settings.get("vad_min_speech_ms", 120))
        )
        self.vad_speech_pad_spinbox.setValue(
            int(settings.get("vad_speech_pad_ms", 120))
        )
        self.min_silence_spinbox.setValue(
            int(settings.get("min_silence_ms", 350))
        )
        self.compress_pauses_checkbox.setChecked(
            bool(settings.get("compress_internal_pauses", False))
        )
        self.pause_threshold_spinbox.setValue(
            int(settings.get("pause_threshold_ms", 1000))
        )
        retained = int(settings.get("retained_pause_ms", 320))
        self.pause_keep_before_spinbox.setValue(
            int(settings.get("pause_keep_before_ms", retained // 2))
        )
        self.pause_keep_after_spinbox.setValue(
            int(settings.get("pause_keep_after_ms", retained - retained // 2))
        )

    def _update_breath_control_state(self):
        mode = str(self.breath_detection_mode_combo.currentData() or "voice_refined")
        needs_db = mode in {"db_only", "union", "intersection", "voice_refined"}
        needs_voice = mode in {
            "voice_only", "union", "intersection", "voice_refined"
        }
        db_enabled = needs_db and self.silence_detection_checkbox.isChecked()
        voice_enabled = needs_voice and self.voice_detection_checkbox.isChecked()
        detector_available = db_enabled or voice_enabled
        internal = detector_available and self.compress_pauses_checkbox.isChecked()
        self.silence_db_spinbox.setEnabled(db_enabled)
        self.voice_detection_checkbox.setEnabled(needs_voice)
        self.vad_threshold_spinbox.setEnabled(voice_enabled)
        self.vad_neg_threshold_spinbox.setEnabled(voice_enabled)
        self.vad_min_speech_spinbox.setEnabled(voice_enabled)
        self.vad_speech_pad_spinbox.setEnabled(voice_enabled)
        self.min_silence_spinbox.setEnabled(db_enabled or voice_enabled)
        self.pause_threshold_spinbox.setEnabled(internal)
        self.pause_keep_before_spinbox.setEnabled(internal)
        self.pause_keep_after_spinbox.setEnabled(internal)
        self.apply_breath_button.setEnabled(not self._breath_reanalysis_running)
        self.task_combo.setEnabled(not self._breath_reanalysis_running)
        self.task_filter_combo.setEnabled(not self._breath_reanalysis_running)
        self.previous_missing_task_button.setEnabled(
            not self._breath_reanalysis_running
            and bool(self._missing_task_indexes())
        )
        self.next_missing_task_button.setEnabled(
            not self._breath_reanalysis_running
            and bool(self._missing_task_indexes())
        )
        has_segment = self.current_segment is not None
        self.mark_delete_start_button.setEnabled(
            has_segment and not self._breath_reanalysis_running
        )
        self.mark_delete_end_button.setEnabled(
            self._manual_delete_anchor is not None
            and not self._breath_reanalysis_running
        )
        self.restore_removed_button.setEnabled(
            has_segment
            and bool(self.current_segment.get("is_removed"))
            and not self._breath_reanalysis_running
        )
        has_manual = False
        if has_segment:
            clip = self.bundle["tasks"][self.task_index]["clips"][
                int(self.current_segment["clip_index"])
            ]
            has_manual = any((
                clip.get("manual_delete_ranges"),
                clip.get("manual_keep_ranges"),
                clip.get("manual_keep_head"),
                clip.get("manual_keep_tail"),
            ))
        self.clear_manual_button.setEnabled(
            has_manual and not self._breath_reanalysis_running
        )

    def _waveform_loading_changed(self, loading):
        if loading and not self._breath_reanalysis_running:
            self.breath_status_label.setText("正在后台加载波形…")
        elif not self._breath_reanalysis_running:
            self.breath_status_label.setText("")

    def _waveform_error(self, message):
        if not self._breath_reanalysis_running:
            self.breath_status_label.setText(str(message))

    def _breath_progress(self, message):
        if self._breath_reanalysis_running:
            self.breath_status_label.setText(str(message))

    def _apply_breath_settings(self):
        if (
            self.voice_detection_checkbox.isChecked()
            and self.vad_neg_threshold_spinbox.value()
            >= self.vad_threshold_spinbox.value()
        ):
            self.breath_status_label.setText(
                "人声结束阈值必须低于开始阈值。"
            )
            self.vad_neg_threshold_spinbox.setFocus()
            return
        keep_before = self.pause_keep_before_spinbox.value()
        keep_after = self.pause_keep_after_spinbox.value()
        threshold = self.pause_threshold_spinbox.value()
        if keep_before + keep_after >= threshold:
            keep_after = max(0, threshold - 100 - keep_before)
            if keep_before + keep_after >= threshold:
                keep_before = max(0, threshold - 100)
                keep_after = 0
                self.pause_keep_before_spinbox.setValue(keep_before)
            self.pause_keep_after_spinbox.setValue(keep_after)
        settings = self.bundle.setdefault("settings", {})
        previous_settings = dict(settings)
        settings.update({
            "breath_detection_mode": (
                self.breath_detection_mode_combo.currentData()
            ),
            "silence_detection_enabled": self.silence_detection_checkbox.isChecked(),
            "silence_threshold_db": self.silence_db_spinbox.value(),
            "voice_detection_enabled": self.voice_detection_checkbox.isChecked(),
            "vad_threshold": self.vad_threshold_spinbox.value(),
            "vad_neg_threshold": self.vad_neg_threshold_spinbox.value(),
            "vad_min_speech_ms": self.vad_min_speech_spinbox.value(),
            "vad_speech_pad_ms": self.vad_speech_pad_spinbox.value(),
            "min_silence_ms": self.min_silence_spinbox.value(),
            "compress_internal_pauses": self.compress_pauses_checkbox.isChecked(),
            "internal_pause_mode": (
                "experimental"
                if self.compress_pauses_checkbox.isChecked() else "off"
            ),
            "pause_threshold_ms": threshold,
            "pause_keep_before_ms": keep_before,
            "pause_keep_after_ms": keep_after,
            "retained_pause_ms": keep_before + keep_after,
        })
        self._breath_reanalysis_running = True
        self._update_breath_control_state()
        mode = str(settings.get("breath_detection_mode") or "voice_refined")
        requires_db = mode in {
            "db_only", "union", "intersection", "voice_refined"
        }
        requires_voice = mode in {
            "voice_only", "union", "intersection", "voice_refined"
        }
        task = self.bundle["tasks"][self.task_index]
        clips = task.get("clips", []) or []
        db_inputs_changed = any(
            previous_settings.get(key) != settings.get(key)
            for key in ("silence_threshold_db", "min_silence_ms")
        )
        voice_inputs_changed = any(
            previous_settings.get(key) != settings.get(key)
            for key in (
                "vad_threshold",
                "vad_neg_threshold",
                "vad_min_speech_ms",
                "vad_speech_pad_ms",
                "min_silence_ms",
            )
        )
        # A mode-only change can recombine the raw cached dB/VAD ranges. If a
        # detector parameter changed (or an old report lacks its raw ranges),
        # rerun that detector in the background before recomputing boundaries.
        needs_db_analysis = bool(
            self.breath_only
            or db_inputs_changed
            or any("silence_ranges" not in clip for clip in clips)
        )
        needs_voice_analysis = bool(
            self.breath_only
            or voice_inputs_changed
            or any("voice_absence_ranges" not in clip for clip in clips)
        )
        self._breath_db_results = None
        self._breath_voice_results = None
        if (
            requires_db
            and settings["silence_detection_enabled"]
            and needs_db_analysis
        ):
            self.silence_loader.start(
                task,
                settings["silence_threshold_db"],
                settings["min_silence_ms"],
            )
        else:
            self._breath_db_results = [{
                "clip_index": index,
                "ranges": list(clip.get("silence_ranges", []) or []),
                "error": str(
                    clip.get("db_silence_detection_error") or ""
                ),
            } for index, clip in enumerate(clips)]
        if (
            requires_voice
            and settings["voice_detection_enabled"]
            and needs_voice_analysis
        ):
            self.voice_loader.start(task, settings)
        else:
            self._breath_voice_results = [{
                "clip_index": index,
                "ranges": list(
                    clip.get("voice_absence_ranges", []) or []
                ),
                "error": str(clip.get("voice_detection_error") or ""),
            } for index, clip in enumerate(clips)]
        self._finish_breath_reanalysis_if_ready()

    def _breath_db_complete(self, results):
        if not self._breath_reanalysis_running:
            return
        self._breath_db_results = list(results or [])
        self._finish_breath_reanalysis_if_ready()

    def _breath_voice_complete(self, results):
        if not self._breath_reanalysis_running:
            return
        self._breath_voice_results = list(results or [])
        self._finish_breath_reanalysis_if_ready()

    def _finish_breath_reanalysis_if_ready(self):
        if (
            not self._breath_reanalysis_running
            or self._breath_db_results is None
            or self._breath_voice_results is None
        ):
            return
        db_by_clip = {
            int(item.get("clip_index", -1)): item
            for item in self._breath_db_results
        }
        voice_by_clip = {
            int(item.get("clip_index", -1)): item
            for item in self._breath_voice_results
        }
        indexes = sorted(set(db_by_clip) | set(voice_by_clip))
        combined = []
        for clip_index in indexes:
            db_result = db_by_clip.get(clip_index, {})
            voice_result = voice_by_clip.get(clip_index, {})
            combined.append({
                "clip_index": clip_index,
                "ranges": list(db_result.get("ranges", []) or []),
                "error": str(db_result.get("error") or ""),
                "voice_ranges": list(
                    voice_result.get("ranges", []) or []
                ),
                "voice_error": str(voice_result.get("error") or ""),
            })
        self._breath_db_results = None
        self._breath_voice_results = None
        self._breath_reanalysis_complete(combined)

    def _breath_reanalysis_complete(self, results):
        if not self._breath_reanalysis_running:
            return
        task = self.bundle["tasks"][self.task_index]
        settings = self.bundle.get("settings", {})
        changed = 0
        errors = []
        for result in results or []:
            clip_index = int(result.get("clip_index", -1))
            clips = task.get("clips", [])
            if clip_index < 0 or clip_index >= len(clips):
                continue
            clip = clips[clip_index]
            previous_boundary_kinds = {
                str(issue.get("kind") or "")
                for issue in (
                    list(clip.get("boundary_warnings", []) or [])
                    + list(clip.get("boundary_decisions", []) or [])
                )
            }
            previous_boundary_kinds.add("missing_script_boundary_guard")
            had_missing_guard = any(
                issue.get("kind") == "missing_script_boundary_guard"
                for issue in clip.get("issues", []) or []
            )
            old_plan = (
                float(clip.get("trim_start") or 0.0),
                float(clip.get("trim_end") or 0.0),
                list(clip.get("pause_removals", []) or []),
            )
            silence_ranges = list(result.get("ranges", []) or [])
            db_error = str(result.get("error") or "")
            voice_absence_ranges = (
                list(result.get("voice_ranges", []) or [])
                if settings.get("voice_detection_enabled", True)
                else []
            )
            voice_error = str(result.get("voice_error") or "")
            breath_gap_ranges = combine_breath_gap_ranges(
                float(clip.get("original_duration") or 0.0),
                silence_ranges,
                voice_absence_ranges,
                mode=settings.get("breath_detection_mode", "voice_refined"),
                db_detection_available=(
                    settings.get("silence_detection_enabled", True)
                    and not db_error
                ),
                voice_detection_available=(
                    settings.get("voice_detection_enabled", True)
                    and not voice_error
                ),
                boundary_extension_s=min(
                    0.55,
                    settings.get("vad_speech_pad_ms", 120) / 1000.0 + 0.1,
                ),
            )
            detection_error = _combined_detection_error(
                settings, db_error, voice_error
            )
            clip["silence_ranges"] = silence_ranges
            clip["voice_absence_ranges"] = voice_absence_ranges
            clip["breath_gap_ranges"] = breath_gap_ranges
            clip["silence_detection_error"] = detection_error
            clip["db_silence_detection_error"] = db_error
            clip["voice_detection_error"] = voice_error
            if self.breath_only:
                trim_start, trim_end, removals, planned_kept = breath_cut_plan(
                    float(clip.get("original_duration") or 0.0),
                    breath_gap_ranges,
                    settings,
                )
            else:
                duration = float(clip.get("original_duration") or 0.0)
                timeline = list(clip.get("word_timeline", []) or [])
                boundary_decisions = []
                boundary_warnings = []
                if timeline:
                    trim_start, trim_end, boundary_decisions, boundary_warnings = (
                        refine_trim_boundaries(
                            min(float(item.get("start") or 0.0) for item in timeline),
                            max(float(item.get("end") or 0.0) for item in timeline),
                            duration,
                            breath_gap_ranges,
                            settings,
                            detection_error,
                        )
                    )
                    if not timeline[0].get("anchor", False):
                        trim_start = 0.0
                    if not timeline[-1].get("anchor", False):
                        trim_end = duration
                else:
                    trim_start = float(
                        clip.get("automatic_trim_start", clip.get("trim_start"))
                        or 0.0
                    )
                    trim_end = float(
                        clip.get("automatic_trim_end", clip.get("trim_end"))
                        or duration
                    )
                clip["automatic_trim_start"] = round(trim_start, 3)
                clip["automatic_trim_end"] = round(trim_end, 3)
                clip["boundary_decisions"] = boundary_decisions
                clip["boundary_warnings"] = boundary_warnings
                removals = find_pause_removals(
                    clip.get("words", []),
                    trim_start,
                    trim_end,
                    settings,
                    breath_gap_ranges,
                )
                planned_kept = kept_ranges(trim_start, trim_end, removals)
            apply_manual_breath_overrides(
                clip, trim_start, trim_end, removals
            )
            clip["silence_threshold_db"] = settings.get("silence_threshold_db")
            clip["min_silence_ms"] = settings.get("min_silence_ms")
            clip["issues"] = [
                issue for issue in clip.get("issues", []) or []
                if issue.get("kind") != "internal_silence_cut"
                and str(issue.get("kind") or "") not in previous_boundary_kinds
            ]
            if not self.breath_only:
                clip["issues"].extend(clip.get("boundary_warnings", []) or [])
                clip["issues"].extend(clip.get("boundary_decisions", []) or [])
            for left, right in clip.get("pause_removals", []) or []:
                recognized = " ".join(
                    str(word.get("text") or "").strip()
                    for word in clip.get("words", []) or []
                    if float(word.get("end") or 0.0) > left
                    and float(word.get("start") or 0.0) < right
                    and str(word.get("text") or "").strip()
                )
                clip["issues"].append({
                    "severity": "info",
                    "kind": "internal_silence_cut",
                    "start": left,
                    "end": right,
                    "title": "句内气口压缩",
                    "detail": (
                        "检测到没有人声或低音量的长停顿，"
                        f"前保留 {settings.get('pause_keep_before_ms')} ms、"
                        f"后保留 {settings.get('pause_keep_after_ms')} ms。"
                    ),
                    "expected": "",
                    "recognized": recognized,
                })
            final_removals = list(clip.get("pause_removals", []) or [])
            internal_removed = sum(right - left for left, right in final_removals)
            duration = float(clip.get("original_duration") or 0.0)
            boundary_removed = float(clip.get("trim_start") or 0.0) + max(
                0.0, duration - float(clip.get("trim_end") or duration)
            )
            clip["internal_removed_seconds"] = round(internal_removed, 3)
            clip["boundary_removed_seconds"] = round(boundary_removed, 3)
            clip["removed_seconds"] = round(
                internal_removed + boundary_removed, 3
            )
            new_plan = (
                float(clip.get("trim_start") or 0.0),
                float(clip.get("trim_end") or 0.0),
                final_removals,
            )
            if old_plan != new_plan:
                clip.pop("review_acknowledged", None)
                changed += 1
            if had_missing_guard:
                severities = {
                    str(issue.get("severity") or "")
                    for issue in clip.get("issues", []) or []
                }
                clip["status"] = (
                    "pink" if "pink" in severities
                    else "orange" if "orange" in severities
                    else "green"
                )
                clip["issue_reason"] = "；".join(
                    dict.fromkeys(
                        _issue_summary(issue)
                        for issue in clip.get("issues", []) or []
                        if issue.get("severity") in {"pink", "orange"}
                    )
                )
            if detection_error:
                errors.append(f"{clip.get('file_name', '')}：{detection_error}")
        if not self.breath_only:
            uncovered = (
                list(task.get("missing_blocks", []) or [])
                + list(task.get("unverified_blocks", []) or [])
            )
            _protect_edges_adjacent_to_missing_script(
                [
                    clip for clip in task.get("clips", []) or []
                    if clip.get("included", True)
                ],
                uncovered,
            )
        self._breath_reanalysis_running = False
        self._update_breath_control_state()
        self.breath_status_label.setText(
            f"已更新 {changed} 个片段的灰色删除区"
            + (f"；{len(errors)} 个读取失败" if errors else "")
        )
        self.orderChanged.emit(self.task_index)
        self.refresh(preserve_time=True)

    def _current_clip_and_source_time(self):
        location = source_location_for_output(
            self.segments, self.output_position
        )
        if location is None:
            return None
        segment, source_time = location
        clips = self.bundle["tasks"][self.task_index].get("clips", [])
        clip_index = int(segment.get("clip_index", -1))
        if clip_index < 0 or clip_index >= len(clips):
            return None
        return segment, clips[clip_index], clip_index, float(source_time)

    def _mark_manual_delete_start(self):
        current = self._current_clip_and_source_time()
        if current is None:
            return
        self.player.pause()
        _segment, _clip, clip_index, source_time = current
        self._manual_delete_anchor = (
            self.task_index, clip_index, source_time
        )
        self.breath_status_label.setText(
            f"已记删除起点 {format_time(source_time)}；移动播放头后记终点。"
        )
        self._update_breath_control_state()

    def _finish_manual_delete_range(self):
        current = self._current_clip_and_source_time()
        anchor = self._manual_delete_anchor
        if current is None or anchor is None:
            return
        segment, clip, clip_index, source_time = current
        if anchor[0] != self.task_index or anchor[1] != clip_index:
            self.breath_status_label.setText("起点和终点必须位于同一个视频片段。")
            return
        left, right = sorted((float(anchor[2]), source_time))
        if right - left < 0.08:
            self.breath_status_label.setText("删除区间至少需要 0.08 秒。")
            return
        duration = float(clip.get("original_duration") or 0.0)
        manual_deletes = list(clip.get("manual_delete_ranges", []) or [])
        manual_deletes.append([left, right])
        clip["manual_delete_ranges"] = manual_deletes
        clip["manual_keep_ranges"] = subtract_time_ranges(
            clip.get("manual_keep_ranges", []) or [],
            [[left, right]],
            duration,
        )
        if left <= 0.001:
            clip["manual_keep_head"] = False
        if right >= duration - 0.001:
            clip["manual_keep_tail"] = False
        apply_manual_breath_overrides(clip)
        clip.pop("review_acknowledged", None)
        self._manual_delete_anchor = None
        self.orderChanged.emit(self.task_index)
        self.refresh(preserve_time=True)
        self.breath_status_label.setText(
            f"已手工添加删除区间 {format_time(left)} - {format_time(right)}。"
        )

    def _restore_current_removed_range(self):
        current = self._current_clip_and_source_time()
        if current is None:
            return
        segment, _clip, _clip_index, _source_time = current
        if not segment.get("is_removed"):
            self.breath_status_label.setText("当前播放头不在灰色删除区。")
            return
        self._restore_removed_segment(segment)

    def _restore_removed_segment(self, segment):
        if not isinstance(segment, dict) or not segment.get("is_removed"):
            return
        clips = self.bundle["tasks"][self.task_index].get("clips", [])
        clip_index = int(segment.get("clip_index", -1))
        if clip_index < 0 or clip_index >= len(clips):
            return
        clip = clips[clip_index]
        left = float(segment.get("source_start") or 0.0)
        right = float(segment.get("source_end") or left)
        duration = float(clip.get("original_duration") or 0.0)
        keep_ranges = list(clip.get("manual_keep_ranges", []) or [])
        keep_ranges.append([left, right])
        clip["manual_keep_ranges"] = keep_ranges
        clip["manual_delete_ranges"] = subtract_time_ranges(
            clip.get("manual_delete_ranges", []) or [],
            [[left, right]],
            duration,
        )
        reason = str(segment.get("remove_reason") or "")
        if reason == "片头气口":
            clip["manual_keep_head"] = True
        elif reason == "片尾气口":
            clip["manual_keep_tail"] = True
        apply_manual_breath_overrides(clip)
        clip.pop("review_acknowledged", None)
        self.orderChanged.emit(self.task_index)
        self.refresh(preserve_time=True)
        self.breath_status_label.setText(
            f"已恢复灰色区 {format_time(left)} - {format_time(right)}。"
        )

    def _clear_current_manual_edits(self):
        current = self._current_clip_and_source_time()
        if current is None:
            return
        _segment, clip, _clip_index, _source_time = current
        for key in (
            "manual_delete_ranges",
            "manual_keep_ranges",
            "manual_keep_head",
            "manual_keep_tail",
        ):
            clip.pop(key, None)
        apply_manual_breath_overrides(clip)
        clip.pop("review_acknowledged", None)
        self._manual_delete_anchor = None
        self.orderChanged.emit(self.task_index)
        self.refresh(preserve_time=True)
        self.breath_status_label.setText("已清除当前片段的手工气口调整。")

    def _missing_task_indexes(self, unresolved_only=False):
        result = []
        for task_index, task in enumerate(self.bundle.get("tasks", []) or []):
            if not task.get("missing_blocks"):
                continue
            if unresolved_only and smart_video_task_review_decision(task):
                continue
            result.append(task_index)
        return result

    def _task_filter_changed(self, _index=None):
        self.refresh(preserve_time=False)

    def _task_matches_filter(self, task):
        mode = str(self.task_filter_combo.currentData() or "all")
        has_missing = bool(task.get("missing_blocks"))
        if mode == "missing":
            return has_missing
        if mode == "unresolved":
            return has_missing and not smart_video_task_review_decision(task)
        return True

    @staticmethod
    def _task_display_text(task):
        label = str(task.get("label") or task.get("task_id") or "任务")
        missing_count = len(task.get("missing_blocks") or [])
        if not missing_count:
            return label
        decision = smart_video_task_review_decision(task)
        if decision == "approved":
            state = "✓ 已确认完整"
        elif decision == "skipped":
            state = "⏸ 本次跳过"
        else:
            state = "⛔ 未处理"
        return f"{state} · 缺段 {missing_count} · {label}"

    def _jump_missing_task(self, direction):
        unresolved = self._missing_task_indexes(unresolved_only=True)
        targets = unresolved or self._missing_task_indexes()
        if not targets:
            return
        if not unresolved and self.task_filter_combo.currentData() == "unresolved":
            self.task_filter_combo.setCurrentIndex(
                self.task_filter_combo.findData("missing")
            )
        try:
            position = targets.index(self.task_index)
        except ValueError:
            position = -1 if int(direction) > 0 else 0
        target_task = targets[(position + int(direction)) % len(targets)]
        combo_index = self.task_combo.findData(target_task)
        if combo_index < 0:
            self.task_filter_combo.setCurrentIndex(
                self.task_filter_combo.findData("missing")
            )
            combo_index = self.task_combo.findData(target_task)
        if combo_index >= 0:
            self.task_combo.setCurrentIndex(combo_index)
            self.issue_filter_combo.setCurrentIndex(
                self.issue_filter_combo.findData("missing")
            )
            if self.issue_tree.topLevelItemCount():
                self.issue_tree.setCurrentItem(self.issue_tree.topLevelItem(0))

    def refresh(self, bundle=None, preserve_time=False):
        if bundle is not None:
            self.bundle = bundle
        previous_task = self.task_index
        previous_time = self.output_position if preserve_time else 0.0
        missing_count = len(self._missing_task_indexes())
        unresolved_count = len(self._missing_task_indexes(unresolved_only=True))
        self.missing_task_count_label.setText(
            f"缺段任务 {missing_count}（未处理 {unresolved_count}）"
        )
        can_jump = bool(missing_count) and not self._breath_reanalysis_running
        self.previous_missing_task_button.setEnabled(can_jump)
        self.next_missing_task_button.setEnabled(can_jump)
        self.task_combo.blockSignals(True)
        self.task_combo.clear()
        for task_index, task in enumerate(self.bundle.get("tasks", []) or []):
            if self._task_matches_filter(task):
                self.task_combo.addItem(
                    self._task_display_text(task), task_index
                )
        if self.task_combo.count():
            selected = self.task_combo.findData(previous_task)
            self.task_combo.setCurrentIndex(selected if selected >= 0 else 0)
        self.task_combo.blockSignals(False)
        self._load_task(self.task_combo.currentIndex(), target_time=previous_time)

    def _detect_source_preview_aspect(self, source):
        source = str(source or "")
        if not source:
            return "portrait"
        if source in self._media_aspect_cache:
            return self._media_aspect_cache[source]
        try:
            width, height, _fps = _media_shape(source)
            aspect = "landscape" if width > height else "portrait"
        except (OSError, ValueError, TypeError):
            aspect = "portrait"
        self._media_aspect_cache[source] = aspect
        return aspect

    def _detect_task_preview_aspect(self, task):
        source = next(
            (
                str(clip.get("source") or "")
                for clip in task.get("clips", []) or []
                if clip.get("included", True) and str(clip.get("source") or "")
            ),
            "",
        )
        return self._detect_source_preview_aspect(source)

    def _apply_preview_aspect(self, _index=None):
        mode = str(self.preview_aspect_combo.currentData() or "auto")
        if mode == "auto":
            mode = self._detected_preview_aspect
        if mode == "landscape":
            self.player.setMinimumSize(360, 210)
            self.preview_column.setMinimumWidth(390)
            self.body_splitter.setSizes([480, 650, 360])
        else:
            self.player.setMinimumSize(210, 360)
            self.preview_column.setMinimumWidth(230)
            self.body_splitter.setSizes([310, 740, 390])

    def _update_duration_summary(self, task):
        kept_seconds = sum(
            float(segment.get("timeline_end") or 0.0)
            - float(segment.get("timeline_start") or 0.0)
            for segment in self.segments
            if segment.get("included", True) and not segment.get("is_removed")
        )
        original_seconds = sum(
            float(clip.get("original_duration") or 0.0)
            for clip in task.get("clips", []) or []
            if clip.get("included", True)
        )
        removed_seconds = max(0.0, original_seconds - kept_seconds)
        self.remaining_duration_label.setText(
            f"裁剪后：{format_time(kept_seconds)}　"
            f"删除：{format_time(removed_seconds)}"
        )
        self.remaining_duration_label.setToolTip(
            f"参与导出的原始片段合计 {format_time(original_seconds)}；"
            f"裁剪后预计成片 {format_time(kept_seconds)}。"
        )

    def _load_task(self, index, target_time=0.0):
        self._manual_delete_anchor = None
        task_index = self.task_combo.itemData(index) if index >= 0 else None
        try:
            task_index = int(task_index)
        except (TypeError, ValueError):
            task_index = -1
        if task_index < 0 or task_index >= len(self.bundle.get("tasks", [])):
            self.player.pause()
            self.current_segment = None
            if hasattr(self, "waveform_loader"):
                self.waveform_loader.cancel()
            self.segments = []
            self.markers = []
            self.duration = 0.0
            self.remaining_duration_label.setText("裁剪后：00:00.00")
            self.timeline.set_data([], [], 0.01)
            self.issue_tree.clear()
            self.issue_detail.clear()
            self.duplicate_group_panel.hide()
            self.task_decision_status.setText("没有符合当前筛选条件的任务。")
            self.task_decision_status.setStyleSheet("color:#616B7E;")
            self.approve_task_button.setEnabled(False)
            self.skip_task_button.setEnabled(False)
            self.clear_task_decision_button.setEnabled(False)
            self._update_breath_control_state()
            return
        self.player.pause()
        self.current_segment = None
        self._preview_removed_once = False
        self.task_index = task_index
        task = self.bundle["tasks"][task_index]
        self.segments, self.duration = build_task_review_timeline(task)
        self._detected_preview_aspect = self._detect_task_preview_aspect(task)
        self._apply_preview_aspect()
        self._update_duration_summary(task)
        self.markers = build_issue_markers(task, self.segments)
        self.recognized_subtitles, self.aligned_subtitles = build_subtitle_tracks(
            task,
            self.segments,
            settings=self.bundle.get("settings", {}),
        )
        self.timeline.set_viewport_width(self.timeline_scroll.viewport().width())
        self.timeline.clear_waveforms()
        self.timeline.set_data(
            self.segments,
            self.markers,
            self.duration,
            self.recognized_subtitles,
            self.aligned_subtitles,
        )
        self._waveform_generation += 1
        waveform_generation = self._waveform_generation
        QtCore.QTimer.singleShot(
            180,
            lambda task=task, generation=waveform_generation: (
                self.waveform_loader.load_task(task)
                if generation == self._waveform_generation
                else None
            ),
        )
        self.seek_slider.setRange(0, max(1, int(round(self.duration * 1000))))
        self.duplicate_group_panel.hide()
        self._fill_issue_tree()
        self._update_task_decision_controls(task)
        initial = min(max(0.0, target_time), self.duration)
        first_item = self.issue_tree.topLevelItem(0)
        first_marker = (
            first_item.data(0, QtCore.Qt.UserRole)
            if first_item is not None else None
        )
        if not target_time and isinstance(first_marker, dict):
            initial = max(0.0, first_marker["time"] - 1.5)
            self.issue_tree.setCurrentItem(first_item)
        # Build the visible timeline synchronously, but do not ask the native
        # decoder for a frame until Qt has had a chance to display the dialog.
        self.output_position = initial
        self._update_position_ui()
        self._media_generation += 1
        media_generation = self._media_generation
        QtCore.QTimer.singleShot(
            0,
            lambda generation=media_generation, position=initial: (
                self._seek_output(position, autoplay=False)
                if generation == self._media_generation
                else None
            ),
        )

    def _fill_issue_tree(self, _index=None):
        self.issue_tree.clear()
        severity_text = {"pink": "严重", "orange": "核对", "green": "轻微"}
        colors = {"pink": "#F4CCCC", "orange": "#FCE5CD", "green": "#D9EAD3"}
        mode = str(self.issue_filter_combo.currentData() or "all")
        visible_markers = [
            marker for marker in self.markers
            if mode == "all"
            or (mode == "missing" and marker.get("kind") == "missing")
            or marker.get("severity") == mode
        ]
        for marker in visible_markers:
            item = QtWidgets.QTreeWidgetItem([
                format_time(marker["time"]),
                severity_text.get(marker.get("severity"), marker.get("severity", "")),
                marker.get("title", "问题"),
            ])
            item.setData(0, QtCore.Qt.UserRole, marker)
            item.setToolTip(2, marker.get("detail", ""))
            brush = QtGui.QBrush(QtGui.QColor(colors.get(marker.get("severity"), "#FFFFFF")))
            for column in range(3):
                item.setBackground(column, brush)
            self.issue_tree.addTopLevelItem(item)
        if not visible_markers:
            message = (
                "当前任务没有待核对问题"
                if mode == "all" else "当前筛选条件下没有问题"
            )
            item = QtWidgets.QTreeWidgetItem(["—", "无", message])
            item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
            self.issue_tree.addTopLevelItem(item)

    def _timeline_seek(self, seconds):
        self._seek_output(seconds, autoplay=self.player.is_playing())
        self._preview_removed_once = bool(
            self.current_segment and self.current_segment.get("is_removed")
        )

    def _timeline_clip_activated(self, clip_index):
        self._select_clip(clip_index)

    def _marker_activated(self, marker):
        desired_filter = "missing" if marker.get("kind") == "missing" else "all"
        if self.issue_filter_combo.currentData() != desired_filter:
            self.issue_filter_combo.setCurrentIndex(
                self.issue_filter_combo.findData(desired_filter)
            )
        self._select_marker_item(marker)

    def _subtitle_activated(self, block):
        self._select_clip(int(block.get("clip_index", 0)))
        if block.get("is_missing"):
            title = "⛔ 缺失的任务原文"
        else:
            title = (
                "模型识别字幕" if block.get("kind") == "recognized"
                else "任务文本对齐字幕"
            )
        severity = {
            "green": "基本一致",
            "orange": "需要核对",
            "pink": "严重不一致",
        }.get(block.get("severity"), "需要核对")
        self.issue_detail.setHtml(
            f"<b>{html.escape(title)} · {html.escape(severity)}</b><br>"
            f"{format_time(block.get('timeline_start', 0.0))} - "
            f"{format_time(block.get('timeline_end', 0.0))}<hr>"
            f"<b>本轨内容：</b>{html.escape(str(block.get('text') or '（空）'))}<br>"
            f"<b>另一轨对应：</b>{html.escape(str(block.get('comparison_text') or '（未找到）'))}<br>"
            f"<b>对齐相似度：</b>{float(block.get('similarity', 0.0)) * 100:.1f}%<br><br>"
            f"<b>建议：</b>{html.escape(str(block.get('suggestion') or '请试听核对。'))}"
        )

    def _select_marker_item(self, marker):
        for index in range(self.issue_tree.topLevelItemCount()):
            item = self.issue_tree.topLevelItem(index)
            if item.data(0, QtCore.Qt.UserRole) == marker:
                self.issue_tree.setCurrentItem(item)
                break

    def _seek_output(self, seconds, autoplay=False):
        self.output_position = max(0.0, min(self.duration, float(seconds or 0.0)))
        location = source_location_for_output(self.segments, self.output_position)
        if location is None:
            self._update_position_ui()
            return
        segment, source_time = location
        local_position = source_time - segment["source_start"]
        same_segment = self.current_segment is segment
        self.current_segment = segment
        self.player.prepare(
            segment["source"],
            segment["source_start"],
            segment["source_end"],
            local_position,
        )
        self._select_clip(segment["clip_index"], emit=not same_segment)
        self._update_position_ui()
        if autoplay:
            self.player.play()
            self._queue_following_range(segment)

    def _select_clip(self, clip_index, emit=True):
        task = self.bundle.get("tasks", [])[self.task_index]
        clips = task.get("clips", [])
        if clip_index < 0 or clip_index >= len(clips):
            return
        clip = clips[clip_index]
        if self.preview_aspect_combo.currentData() == "auto":
            detected = self._detect_source_preview_aspect(clip.get("source"))
            if detected != self._detected_preview_aspect:
                self._detected_preview_aspect = detected
                self._apply_preview_aspect()
        self.expected_text.setPlainText(str(clip.get("expected_text") or ""))
        self.recognized_text.setPlainText(str(clip.get("recognized_text") or ""))
        self.acknowledge_button.setEnabled(not bool(clip.get("review_acknowledged")))
        self.acknowledge_button.setText(
            "✓ 已人工核对" if clip.get("review_acknowledged") else "✓ 当前片段没问题"
        )
        self._update_duplicate_group_controls(clip_index)
        ordered = sorted(
            range(len(clips)),
            key=lambda index: (
                int(clips[index].get("export_order", index + 1)),
                int(clips[index].get("source_index", index)),
            ),
        )
        position = ordered.index(clip_index) if clip_index in ordered else -1
        self.move_clip_left_button.setEnabled(position > 0)
        self.move_clip_right_button.setEnabled(0 <= position < len(ordered) - 1)
        self._update_breath_control_state()
        if emit:
            self.clipSelected.emit(self.task_index, int(clip_index))

    def _clear_duplicate_choices(self):
        for button in self.duplicate_button_group.buttons():
            self.duplicate_button_group.removeButton(button)
            button.deleteLater()
        while self.duplicate_choices_layout.count():
            item = self.duplicate_choices_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _update_duplicate_group_controls(self, clip_index):
        self._clear_duplicate_choices()
        tasks = self.bundle.get("tasks", []) or []
        if self.breath_only or self.task_index >= len(tasks):
            self.duplicate_group_panel.hide()
            return
        clips = tasks[self.task_index].get("clips", []) or []
        if clip_index < 0 or clip_index >= len(clips):
            self.duplicate_group_panel.hide()
            return
        group_id = str(clips[clip_index].get("duplicate_group_id") or "")
        candidates = [
            (index, candidate)
            for index, candidate in enumerate(clips)
            if group_id
            and str(candidate.get("duplicate_group_id") or "") == group_id
        ]
        if len(candidates) < 2:
            self.duplicate_group_panel.hide()
            return
        self.duplicate_group_panel.show()
        selected_name = next(
            (
                str(candidate.get("file_name") or "")
                for _index, candidate in candidates
                if candidate.get("duplicate_selected")
            ),
            "",
        )
        self.duplicate_group_hint.setText(
            f"检测到 {len(candidates)} 个重复版本；当前保留："
            f"{selected_name or '未确定'}"
        )
        for index, candidate in candidates:
            duration = float(candidate.get("original_duration") or 0.0)
            score = float(candidate.get("similarity") or 0.0) * 100.0
            label = (
                f"{candidate.get('file_name') or f'片段 {index + 1}'}　"
                f"{duration:.1f} 秒 / 匹配 {score:.0f}%"
            )
            button = QtWidgets.QRadioButton(
                label, self.duplicate_choices_widget
            )
            button.setToolTip(
                str(candidate.get("recognized_text") or "没有识别文本")
            )
            self.duplicate_button_group.addButton(button, index)
            button.setChecked(bool(candidate.get("duplicate_selected")))
            button.toggled.connect(
                lambda checked, candidate_index=index: (
                    self._choose_duplicate_clip(candidate_index)
                    if checked else None
                )
            )
            self.duplicate_choices_layout.addWidget(button)

    def _choose_duplicate_clip(self, clip_index):
        task = self.bundle.get("tasks", [])[self.task_index]
        current = next(
            (
                index for index, clip in enumerate(task.get("clips", []) or [])
                if clip.get("duplicate_group_id")
                == task["clips"][clip_index].get("duplicate_group_id")
                and clip.get("duplicate_selected")
            ),
            -1,
        )
        if int(current) == int(clip_index):
            return
        try:
            select_duplicate_group_clip(
                task,
                clip_index,
                self.bundle.get("settings", {}),
            )
        except (IndexError, TypeError, ValueError) as error:
            QtWidgets.QMessageBox.warning(self, "切换重复片段", str(error))
            self._update_duplicate_group_controls(current)
            return
        self.orderChanged.emit(self.task_index)
        self.refresh(preserve_time=False)
        self.select_clip(self.task_index, clip_index)

    def _player_position_changed(self, local_position):
        if self.current_segment is None:
            return
        self.output_position = min(
            self.duration,
            self.current_segment["timeline_start"] + float(local_position or 0.0),
        )
        self._update_position_ui()

    def _player_state_changed(self, playing):
        self.play_button.setText("❚❚ 暂停" if playing else "▶ 播放")

    def _update_position_ui(self):
        self._loading_slider = True
        self.seek_slider.setValue(int(round(self.output_position * 1000)))
        self._loading_slider = False
        self.timeline.set_playhead(self.output_position)
        self.time_label.setText(
            f"{format_time(self.output_position)} / {format_time(self.duration)}"
        )
        playhead_x = self.timeline._x_for_time(self.output_position)
        bar = self.timeline_scroll.horizontalScrollBar()
        viewport = self.timeline_scroll.viewport().width()
        if playhead_x < bar.value() + 40 or playhead_x > bar.value() + viewport - 40:
            bar.setValue(max(0, int(playhead_x - viewport * 0.45)))

    def toggle_playback(self):
        if self.player.is_playing():
            self.player.pause()
            return
        if not self.segments:
            return
        location = source_location_for_output(self.segments, self.output_position)
        if location is None:
            return
        segment, source_time = location
        if segment.get("is_removed") and not self._preview_removed_once:
            try:
                start_index = self.segments.index(segment)
            except ValueError:
                start_index = -1
            playable = self._next_playable_index(start_index + 1)
            if playable is None:
                return
            segment = self.segments[playable]
            source_time = segment["source_start"]
            self.output_position = segment["timeline_start"]
        self._preview_removed_once = False
        self.current_segment = segment
        self.player.prepare(
            segment["source"],
            segment["source_start"],
            segment["source_end"],
            source_time - segment["source_start"],
        )
        self.player.play()
        self._queue_following_range(segment)

    def _play_next_range(self):
        if self.current_segment is None:
            return
        try:
            index = self.segments.index(self.current_segment)
        except ValueError:
            return
        next_index = self._next_playable_index(index + 1)
        if next_index is None:
            self.output_position = self.duration
            self._update_position_ui()
            return
        next_segment = self.segments[next_index]
        self.output_position = next_segment["timeline_start"]
        self.current_segment = next_segment
        self.player.prepare(
            next_segment["source"],
            next_segment["source_start"],
            next_segment["source_end"],
            0.0,
        )
        self._select_clip(next_segment["clip_index"])
        self._update_position_ui()
        self.player.play()
        self._queue_following_range(next_segment)

    def _queue_following_range(self, segment):
        """Warm the next decoder so a clip boundary does not restart cold."""
        try:
            current_index = self.segments.index(segment)
        except ValueError:
            self.player.clear_queue()
            return
        next_index = self._next_playable_index(current_index + 1)
        if next_index is None:
            self.player.clear_queue()
            return
        following = self.segments[next_index]
        self.player.queue(
            following["source"],
            following["source_start"],
            following["source_end"],
        )

    def _next_playable_index(self, start):
        for index in range(max(0, int(start)), len(self.segments)):
            segment = self.segments[index]
            if segment.get("included", True) and not segment.get("is_removed"):
                return index
        return None

    def _move_current_clip(self, direction):
        if self.current_segment is None:
            return
        task = self.bundle.get("tasks", [])[self.task_index]
        clips = task.get("clips", [])
        current = int(self.current_segment["clip_index"])
        ordered = sorted(
            range(len(clips)),
            key=lambda index: (
                int(clips[index].get("export_order", index + 1)),
                int(clips[index].get("source_index", index)),
            ),
        )
        try:
            position = ordered.index(current)
        except ValueError:
            return
        target = position + int(direction)
        if target < 0 or target >= len(ordered):
            return
        ordered[position], ordered[target] = ordered[target], ordered[position]
        self._apply_clip_order(ordered, current)

    def _move_clip_to(self, current, target):
        task = self.bundle.get("tasks", [])[self.task_index]
        clips = task.get("clips", [])
        ordered = sorted(
            range(len(clips)),
            key=lambda index: (
                int(clips[index].get("export_order", index + 1)),
                int(clips[index].get("source_index", index)),
            ),
        )
        try:
            source_position = ordered.index(int(current))
            target_position = ordered.index(int(target))
        except ValueError:
            return
        if source_position == target_position:
            return
        clip_index = ordered.pop(source_position)
        ordered.insert(target_position, clip_index)
        self._apply_clip_order(ordered, int(current))

    def _apply_clip_order(self, ordered, current):
        task = self.bundle.get("tasks", [])[self.task_index]
        clips = task.get("clips", [])
        for export_order, clip_index in enumerate(ordered, 1):
            clips[clip_index]["export_order"] = export_order
        self.orderChanged.emit(self.task_index)
        self.refresh(preserve_time=False)
        self.select_clip(self.task_index, current)

    def _request_task_decision(self, decision):
        self.taskDecisionRequested.emit(self.task_index, str(decision or ""))

    def _update_task_decision_controls(self, task):
        has_missing = bool(task.get("missing_blocks"))
        decision = smart_video_task_review_decision(task)
        if decision == "approved":
            text = "✓ 已人工确认内容完整；这个任务会继续生成。"
            color = "#1B5E20"
        elif decision == "skipped":
            text = "⏸ 这个任务本次将跳过，其余任务可继续生成。"
            color = "#8A5A00"
        elif has_missing:
            text = "⛔ 检测到缺段：请试听后选择继续生成或跳过当前任务。"
            color = "#B3261E"
        else:
            text = "当前任务没有确认缺段，无需作任务级决定。"
            color = "#616B7E"
        self.task_decision_status.setText(text)
        self.task_decision_status.setStyleSheet(f"color:{color};font-weight:600;")
        self.approve_task_button.setEnabled(has_missing and decision != "approved")
        self.skip_task_button.setEnabled(has_missing and decision != "skipped")
        self.clear_task_decision_button.setEnabled(bool(decision))

    def _slider_pressed(self):
        self._resume_after_seek = self.player.is_playing()
        self.player.pause()

    def _slider_moved(self, value):
        self._seek_output(float(value) / 1000.0, autoplay=False)

    def _slider_released(self):
        if self._resume_after_seek:
            self.toggle_playback()
        self._resume_after_seek = False

    def _issue_selection_changed(self, current, _previous):
        marker = current.data(0, QtCore.Qt.UserRole) if current else None
        if not isinstance(marker, dict):
            self.issue_detail.clear()
            return
        self.markerSelected.emit(marker)
        expected = marker.get("expected") or "（无）"
        recognized = marker.get("recognized") or "（无）"
        self.issue_detail.setHtml(
            f"<b>{html.escape(str(marker.get('title', '问题')))}</b><br>"
            f"{html.escape(str(marker.get('detail', '')))}<hr>"
            f"<b>正确：</b>{html.escape(str(expected))}<br>"
            f"<b>识别：</b>{html.escape(str(recognized))}"
        )
        if marker.get("clip_index") is not None:
            self._select_clip(int(marker["clip_index"]))

    def play_selected_issue(self):
        item = self.issue_tree.currentItem()
        marker = item.data(0, QtCore.Qt.UserRole) if item else None
        if not isinstance(marker, dict):
            return
        self._seek_output(max(0.0, marker["time"] - 1.8), autoplay=False)
        self.toggle_playback()

    def jump_issue(self, direction):
        visible_items = []
        for index in range(self.issue_tree.topLevelItemCount()):
            item = self.issue_tree.topLevelItem(index)
            if isinstance(item.data(0, QtCore.Qt.UserRole), dict):
                visible_items.append(item)
        if not visible_items:
            return
        current_item = self.issue_tree.currentItem()
        try:
            current = visible_items.index(current_item)
        except ValueError:
            current = -1 if int(direction) > 0 else 0
        item = visible_items[(current + int(direction)) % len(visible_items)]
        self.issue_tree.setCurrentItem(item)
        marker = item.data(0, QtCore.Qt.UserRole)
        self._seek_output(max(0.0, marker["time"] - 1.0), autoplay=False)

    def _acknowledge_current_clip(self):
        if self.current_segment is None:
            return
        self.acknowledgeRequested.emit(
            self.task_index, int(self.current_segment["clip_index"])
        )

    def _show_player_error(self, message):
        QtWidgets.QMessageBox.warning(self, "时间线播放器", str(message))

    def select_clip(self, task_index, clip_index):
        if task_index != self.task_index:
            combo_index = self.task_combo.findData(int(task_index))
            if combo_index < 0:
                self.task_filter_combo.setCurrentIndex(
                    self.task_filter_combo.findData("all")
                )
                combo_index = self.task_combo.findData(int(task_index))
            if combo_index >= 0:
                self.task_combo.setCurrentIndex(combo_index)
        for segment in self.segments:
            if int(segment["clip_index"]) == int(clip_index):
                self._seek_output(segment["timeline_start"], autoplay=False)
                return

    def close_player(self):
        self.player.release()
        self.waveform_loader.cancel()
        self.silence_loader.cancel()
        self.voice_loader.cancel()
