from PyQt5 import QtWidgets

from .engine import (
    SMART_VIDEO_EDITOR_CONFIG_KEY,
    normalize_smart_video_editor_settings,
)


class SmartVideoEditorSettingsPage:
    """Program-settings page owned entirely by the smart-edit plugin."""

    def __init__(self, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        scroll = QtWidgets.QScrollArea(self.widget)
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget(scroll)
        form = QtWidgets.QFormLayout(content)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        self.lead_padding_spinbox = QtWidgets.QSpinBox(content)
        self.lead_padding_spinbox.setRange(0, 3000)
        self.lead_padding_spinbox.setSuffix(" ms")
        self.tail_padding_spinbox = QtWidgets.QSpinBox(content)
        self.tail_padding_spinbox.setRange(0, 3000)
        self.tail_padding_spinbox.setSuffix(" ms")
        form.addRow("片段开头留白：", self.lead_padding_spinbox)
        form.addRow("片段结尾留白：", self.tail_padding_spinbox)

        self.silence_detection_checkbox = QtWidgets.QCheckBox(
            "启用分贝静音检测", content
        )
        self.silence_detection_checkbox.setToolTip(
            "Whisper 只负责定位说到哪个单词；实际首尾切点必须位于 FFmpeg "
            "检测到的低音量区间。找不到静音时会保留原片并提示核对。"
        )
        self.silence_db_spinbox = QtWidgets.QSpinBox(content)
        self.silence_db_spinbox.setRange(-80, -5)
        self.silence_db_spinbox.setSuffix(" dB")
        self.silence_db_spinbox.setToolTip(
            "越接近 0 越容易把较小声音当作静音；如果误剪说话，请调低，"
            "例如从 -35 调到 -42 dB。"
        )
        self.min_silence_spinbox = QtWidgets.QSpinBox(content)
        self.min_silence_spinbox.setRange(80, 5000)
        self.min_silence_spinbox.setSingleStep(50)
        self.min_silence_spinbox.setSuffix(" ms")
        self.voice_detection_checkbox = QtWidgets.QCheckBox(
            "启用人声活动检测（推荐；有环境音时仍能找到气口）", content
        )
        self.voice_detection_checkbox.setToolTip(
            "使用 Faster-Whisper 自带的 Silero VAD 判断有没有人在说话。"
            "它与分贝检测合并使用，不会额外安装模型。"
        )
        self.vad_threshold_spinbox = QtWidgets.QDoubleSpinBox(content)
        self.vad_threshold_spinbox.setRange(0.10, 0.95)
        self.vad_threshold_spinbox.setDecimals(2)
        self.vad_threshold_spinbox.setSingleStep(0.05)
        self.vad_threshold_spinbox.setToolTip(
            "越高越不容易把环境音误认成人声、裁切更积极；太高可能误伤轻声，"
            "默认 0.65。"
        )
        self.vad_neg_threshold_spinbox = QtWidgets.QDoubleSpinBox(content)
        self.vad_neg_threshold_spinbox.setRange(0.01, 0.94)
        self.vad_neg_threshold_spinbox.setDecimals(2)
        self.vad_neg_threshold_spinbox.setSingleStep(0.05)
        self.vad_neg_threshold_spinbox.setToolTip(
            "已经进入人声后，概率低于此值才结束人声段；必须低于开始阈值。"
        )
        self.vad_min_speech_spinbox = QtWidgets.QSpinBox(content)
        self.vad_min_speech_spinbox.setRange(0, 2000)
        self.vad_min_speech_spinbox.setSingleStep(20)
        self.vad_min_speech_spinbox.setSuffix(" ms")
        self.vad_min_speech_spinbox.setToolTip(
            "短于此值的疑似人声会当作环境杂音；过大会漏掉很短的词。"
        )
        self.vad_speech_pad_spinbox = QtWidgets.QSpinBox(content)
        self.vad_speech_pad_spinbox.setRange(0, 1500)
        self.vad_speech_pad_spinbox.setSingleStep(50)
        self.vad_speech_pad_spinbox.setSuffix(" ms")
        self.vad_speech_pad_spinbox.setToolTip(
            "在人声两侧额外保护的时间；数值越大越安全，但会明显保留更多"
            "环境音。默认 120 ms。"
        )
        self.boundary_search_spinbox = QtWidgets.QSpinBox(content)
        self.boundary_search_spinbox.setRange(100, 10000)
        self.boundary_search_spinbox.setSingleStep(100)
        self.boundary_search_spinbox.setSuffix(" ms")
        self.boundary_search_spinbox.setToolTip(
            "只在首词之前、尾词之后的这个范围内寻找静音边界。"
        )
        self.breath_detection_mode_combo = QtWidgets.QComboBox(content)
        self.breath_detection_mode_combo.addItem(
            "仅分贝（旧版，积极）", "db_only"
        )
        self.breath_detection_mode_combo.addItem(
            "仅人声（适合有环境音）", "voice_only"
        )
        self.breath_detection_mode_combo.addItem(
            "并集：任一检测认为是气口（最积极）", "union"
        )
        self.breath_detection_mode_combo.addItem(
            "交集：两种检测都认为是气口（最保守）", "intersection"
        )
        self.breath_detection_mode_combo.addItem(
            "人声主导＋分贝修边（均衡）", "voice_refined"
        )
        self.breath_detection_mode_combo.setToolTip(
            "旧版分贝模式最接近此前可用的裁切效果；人声模式能忽略环境底噪；"
            "并集删除更多，交集删除更少。"
        )
        form.addRow("气口判断方式：", self.breath_detection_mode_combo)
        form.addRow("", self.silence_detection_checkbox)
        form.addRow("静音音量阈值：", self.silence_db_spinbox)
        form.addRow("", self.voice_detection_checkbox)
        form.addRow("人声检测阈值：", self.vad_threshold_spinbox)
        form.addRow("人声结束阈值：", self.vad_neg_threshold_spinbox)
        form.addRow("最短人声时长：", self.vad_min_speech_spinbox)
        form.addRow("人声保护边距：", self.vad_speech_pad_spinbox)
        form.addRow("最短静音时长：", self.min_silence_spinbox)
        form.addRow("切点搜索范围：", self.boundary_search_spinbox)

        self.compress_pauses_checkbox = QtWidgets.QCheckBox(
            "实验性：压缩句子内部的过长停顿（默认关闭）", content
        )
        self.compress_pauses_checkbox.setToolTip(
            "正常智能剪辑已经会删除每段开头和结尾的气口。句内停顿可能包含"
            "Whisper 漏掉的单词，开启后存在误剪风险。"
        )
        self.pause_threshold_spinbox = QtWidgets.QSpinBox(content)
        self.pause_threshold_spinbox.setRange(300, 10000)
        self.pause_threshold_spinbox.setSingleStep(100)
        self.pause_threshold_spinbox.setSuffix(" ms")
        self.pause_keep_before_spinbox = QtWidgets.QSpinBox(content)
        self.pause_keep_before_spinbox.setRange(0, 2500)
        self.pause_keep_before_spinbox.setSingleStep(50)
        self.pause_keep_before_spinbox.setSuffix(" ms")
        self.pause_keep_before_spinbox.setToolTip(
            "每个被删除气口的左侧保留量，用来保护前一句的尾音。"
        )
        self.pause_keep_after_spinbox = QtWidgets.QSpinBox(content)
        self.pause_keep_after_spinbox.setRange(0, 2500)
        self.pause_keep_after_spinbox.setSingleStep(50)
        self.pause_keep_after_spinbox.setSuffix(" ms")
        self.pause_keep_after_spinbox.setToolTip(
            "每个被删除气口的右侧保留量，用来保护后一句的起音。"
        )
        form.addRow("", self.compress_pauses_checkbox)
        form.addRow("超过此时长才压缩：", self.pause_threshold_spinbox)
        form.addRow("删除区间向前保留：", self.pause_keep_before_spinbox)
        form.addRow("删除区间向后保留：", self.pause_keep_after_spinbox)

        self.pass_similarity_spinbox = QtWidgets.QSpinBox(content)
        self.pass_similarity_spinbox.setRange(40, 100)
        self.pass_similarity_spinbox.setSuffix(" %")
        self.severe_similarity_spinbox = QtWidgets.QSpinBox(content)
        self.severe_similarity_spinbox.setRange(0, 95)
        self.severe_similarity_spinbox.setSuffix(" %")
        form.addRow("自动通过相似度：", self.pass_similarity_spinbox)
        form.addRow("严重异常低于：", self.severe_similarity_spinbox)

        self.whisper_model_combo = QtWidgets.QComboBox(content)
        self.whisper_model_combo.addItem("base（当前速度，精度较低）", "base")
        self.whisper_model_combo.addItem("small（均衡）", "small")
        self.whisper_model_combo.addItem("medium（更准确，CPU 较慢）", "medium")
        self.whisper_model_combo.addItem("large-v3（最高精度，最慢）", "large-v3")
        self.whisper_model_combo.setToolTip(
            "同时用于视频内容识别和最终字幕强制对齐。首次选择未缓存模型时"
            "需要下载；修改后会在下一次分析或导出时于后台动态切换。"
        )
        form.addRow("任务识别/对齐模型：", self.whisper_model_combo)

        model_note = QtWidgets.QLabel(
            "模型越大，外语短词和词尾时间通常越准确，但 CPU 用时和内存占用"
            "也会明显增加。程序会优先复用本机缓存；未缓存模型首次使用需要"
            "下载，不需要重启程序。",
            content,
        )
        model_note.setWordWrap(True)
        model_note.setStyleSheet("color:#555;")
        form.addRow("", model_note)

        self.use_main_subtitle_settings_checkbox = QtWidgets.QCheckBox(
            "沿用主界面的字幕生成参数", content
        )
        self.subtitle_max_words_spinbox = QtWidgets.QSpinBox(content)
        self.subtitle_max_words_spinbox.setRange(0, 50)
        self.subtitle_max_words_spinbox.setSpecialValueText("不限")
        self.subtitle_max_chars_spinbox = QtWidgets.QSpinBox(content)
        self.subtitle_max_chars_spinbox.setRange(0, 500)
        self.subtitle_max_chars_spinbox.setSpecialValueText("不限")
        self.subtitle_gap_spinbox = QtWidgets.QSpinBox(content)
        self.subtitle_gap_spinbox.setRange(-1, 5000)
        self.subtitle_gap_spinbox.setSpecialValueText("保留原间隔")
        self.subtitle_gap_spinbox.setSuffix(" ms")
        self.subtitle_line_break_checkbox = QtWidgets.QCheckBox(
            "允许字幕块内部换行", content
        )
        form.addRow("字幕参数：", self.use_main_subtitle_settings_checkbox)
        form.addRow("每块最多单词：", self.subtitle_max_words_spinbox)
        form.addRow("每块最多字符：", self.subtitle_max_chars_spinbox)
        form.addRow("相邻字幕块间隔：", self.subtitle_gap_spinbox)
        form.addRow("", self.subtitle_line_break_checkbox)

        subtitle_note = QtWidgets.QLabel(
            "间隔 -1 表示保留模型时间，0 ms 表示字幕块首尾相接。单词数和"
            "字符数同时启用时，任何一个达到上限都会切分。",
            content,
        )
        subtitle_note.setWordWrap(True)
        subtitle_note.setStyleSheet("color:#555;")
        form.addRow("", subtitle_note)

        self.output_folder_edit = QtWidgets.QLineEdit(content)
        self.ffmpeg_path_edit = QtWidgets.QLineEdit(content)
        self.ffmpeg_path_edit.setPlaceholderText(
            "留空时使用整理任务结果的编码器或系统 ffmpeg"
        )
        browse_row = QtWidgets.QWidget(content)
        browse_layout = QtWidgets.QHBoxLayout(browse_row)
        browse_layout.setContentsMargins(0, 0, 0, 0)
        browse_layout.addWidget(self.ffmpeg_path_edit, 1)
        browse_button = QtWidgets.QPushButton("浏览…", browse_row)
        browse_button.clicked.connect(self._browse_ffmpeg)
        browse_layout.addWidget(browse_button)
        self.existing_output_combo = QtWidgets.QComboBox(content)
        self.existing_output_combo.addItem("保留旧文件并生成新版本", "version")
        self.existing_output_combo.addItem("覆盖旧输出", "overwrite")
        self.existing_output_combo.addItem("已有输出时跳过", "skip")
        self.auto_export_checkbox = QtWidgets.QCheckBox(
            "没有发生裁切且全部通过时直接导出；有裁切或异常时打开核对界面",
            content,
        )
        form.addRow("输出目录名：", self.output_folder_edit)
        form.addRow("FFmpeg 编码器：", browse_row)
        form.addRow("已有输出：", self.existing_output_combo)
        form.addRow("", self.auto_export_checkbox)

        note = QtWidgets.QLabel(
            "该功能从“插件”菜单或任务列表右键菜单启动：以任务语音文案为"
            "正确内容，Whisper 只负责定位和核对。原视频永不覆盖，分析报告和"
            "识别缓存保存在输出目录中。气口可以按分贝、人声或两者的"
            "并集/交集判断；旧版分贝模式最积极，人声模式更适合有底噪的素材。"
            "句内停顿压缩属于实验功能，"
            "默认关闭。",
            content,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        form.addRow(note)

        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.compress_pauses_checkbox.toggled.connect(self._update_enabled_state)
        self.silence_detection_checkbox.toggled.connect(self._update_enabled_state)
        self.voice_detection_checkbox.toggled.connect(self._update_enabled_state)
        self.breath_detection_mode_combo.currentIndexChanged.connect(
            self._update_enabled_state
        )
        self.use_main_subtitle_settings_checkbox.toggled.connect(
            self._update_subtitle_state
        )
        self.load_config({})

    def _browse_ffmpeg(self):
        selected, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self.widget,
            "选择 FFmpeg 编码器",
            self.ffmpeg_path_edit.text().strip(),
            "可执行文件 (*.exe);;所有文件 (*)",
        )
        if selected:
            self.ffmpeg_path_edit.setText(selected)

    def _update_enabled_state(self):
        mode = str(self.breath_detection_mode_combo.currentData() or "voice_refined")
        needs_db = mode in {"db_only", "union", "intersection", "voice_refined"}
        needs_voice = mode in {
            "voice_only", "union", "intersection", "voice_refined"
        }
        silence_enabled = self.silence_detection_checkbox.isChecked()
        voice_enabled = self.voice_detection_checkbox.isChecked()
        any_detector = (
            (needs_db and silence_enabled) or (needs_voice and voice_enabled)
        )
        self.silence_db_spinbox.setEnabled(needs_db and silence_enabled)
        self.vad_threshold_spinbox.setEnabled(needs_voice and voice_enabled)
        self.vad_neg_threshold_spinbox.setEnabled(needs_voice and voice_enabled)
        self.vad_min_speech_spinbox.setEnabled(needs_voice and voice_enabled)
        self.vad_speech_pad_spinbox.setEnabled(needs_voice and voice_enabled)
        self.min_silence_spinbox.setEnabled(any_detector)
        self.boundary_search_spinbox.setEnabled(any_detector)
        pause_enabled = any_detector and self.compress_pauses_checkbox.isChecked()
        self.pause_threshold_spinbox.setEnabled(pause_enabled)
        self.pause_keep_before_spinbox.setEnabled(pause_enabled)
        self.pause_keep_after_spinbox.setEnabled(pause_enabled)

    def _update_subtitle_state(self):
        enabled = not self.use_main_subtitle_settings_checkbox.isChecked()
        self.subtitle_max_words_spinbox.setEnabled(enabled)
        self.subtitle_max_chars_spinbox.setEnabled(enabled)
        self.subtitle_gap_spinbox.setEnabled(enabled)
        self.subtitle_line_break_checkbox.setEnabled(enabled)

    def load_config(self, config):
        source = config.get(SMART_VIDEO_EDITOR_CONFIG_KEY) if isinstance(config, dict) else None
        settings = normalize_smart_video_editor_settings(source)
        self.lead_padding_spinbox.setValue(settings["lead_padding_ms"])
        self.tail_padding_spinbox.setValue(settings["tail_padding_ms"])
        self.silence_detection_checkbox.setChecked(settings["silence_detection_enabled"])
        self.silence_db_spinbox.setValue(settings["silence_threshold_db"])
        self.voice_detection_checkbox.setChecked(
            settings["voice_detection_enabled"]
        )
        self.vad_threshold_spinbox.setValue(settings["vad_threshold"])
        self.vad_neg_threshold_spinbox.setValue(
            settings["vad_neg_threshold"]
        )
        self.vad_min_speech_spinbox.setValue(settings["vad_min_speech_ms"])
        self.vad_speech_pad_spinbox.setValue(settings["vad_speech_pad_ms"])
        mode_index = self.breath_detection_mode_combo.findData(
            settings["breath_detection_mode"]
        )
        self.breath_detection_mode_combo.setCurrentIndex(
            mode_index if mode_index >= 0 else 0
        )
        self.min_silence_spinbox.setValue(settings["min_silence_ms"])
        self.boundary_search_spinbox.setValue(settings["boundary_search_ms"])
        self.compress_pauses_checkbox.setChecked(settings["compress_internal_pauses"])
        self.pause_threshold_spinbox.setValue(settings["pause_threshold_ms"])
        self.pause_keep_before_spinbox.setValue(
            settings["pause_keep_before_ms"]
        )
        self.pause_keep_after_spinbox.setValue(
            settings["pause_keep_after_ms"]
        )
        self.pass_similarity_spinbox.setValue(settings["pass_similarity_percent"])
        self.severe_similarity_spinbox.setValue(settings["severe_similarity_percent"])
        model_index = self.whisper_model_combo.findData(
            settings["whisper_model_size"]
        )
        self.whisper_model_combo.setCurrentIndex(
            model_index if model_index >= 0 else 0
        )
        self.use_main_subtitle_settings_checkbox.setChecked(
            settings["use_main_subtitle_settings"]
        )
        self.subtitle_max_words_spinbox.setValue(
            settings["srt_max_words_per_block"]
        )
        self.subtitle_max_chars_spinbox.setValue(
            settings["srt_max_chars_per_block"]
        )
        self.subtitle_gap_spinbox.setValue(settings["srt_block_gap_ms"])
        self.subtitle_line_break_checkbox.setChecked(
            settings["srt_include_line_breaks"]
        )
        self.output_folder_edit.setText(settings["output_folder_name"])
        self.ffmpeg_path_edit.setText(settings["ffmpeg_path"])
        index = self.existing_output_combo.findData(settings["existing_output"])
        self.existing_output_combo.setCurrentIndex(index if index >= 0 else 0)
        self.auto_export_checkbox.setChecked(settings["auto_export_clean"])
        self._update_enabled_state()
        self._update_subtitle_state()

    def settings(self):
        return normalize_smart_video_editor_settings({
            "lead_padding_ms": self.lead_padding_spinbox.value(),
            "tail_padding_ms": self.tail_padding_spinbox.value(),
            "silence_detection_enabled": self.silence_detection_checkbox.isChecked(),
            "silence_threshold_db": self.silence_db_spinbox.value(),
            "voice_detection_enabled": self.voice_detection_checkbox.isChecked(),
            "vad_threshold": self.vad_threshold_spinbox.value(),
            "vad_neg_threshold": self.vad_neg_threshold_spinbox.value(),
            "vad_min_speech_ms": self.vad_min_speech_spinbox.value(),
            "vad_speech_pad_ms": self.vad_speech_pad_spinbox.value(),
            "breath_detection_mode": (
                self.breath_detection_mode_combo.currentData()
            ),
            "min_silence_ms": self.min_silence_spinbox.value(),
            "boundary_search_ms": self.boundary_search_spinbox.value(),
            "compress_internal_pauses": self.compress_pauses_checkbox.isChecked(),
            "internal_pause_mode": "experimental" if self.compress_pauses_checkbox.isChecked() else "off",
            "pause_threshold_ms": self.pause_threshold_spinbox.value(),
            "pause_keep_before_ms": self.pause_keep_before_spinbox.value(),
            "pause_keep_after_ms": self.pause_keep_after_spinbox.value(),
            "retained_pause_ms": (
                self.pause_keep_before_spinbox.value()
                + self.pause_keep_after_spinbox.value()
            ),
            "pass_similarity_percent": self.pass_similarity_spinbox.value(),
            "severe_similarity_percent": self.severe_similarity_spinbox.value(),
            "whisper_model_size": self.whisper_model_combo.currentData(),
            "use_main_subtitle_settings": (
                self.use_main_subtitle_settings_checkbox.isChecked()
            ),
            "srt_max_words_per_block": self.subtitle_max_words_spinbox.value(),
            "srt_max_chars_per_block": self.subtitle_max_chars_spinbox.value(),
            "srt_block_gap_ms": self.subtitle_gap_spinbox.value(),
            "srt_include_line_breaks": (
                self.subtitle_line_break_checkbox.isChecked()
            ),
            "output_folder_name": self.output_folder_edit.text().strip(),
            "ffmpeg_path": self.ffmpeg_path_edit.text().strip(),
            "existing_output": self.existing_output_combo.currentData(),
            "auto_export_clean": self.auto_export_checkbox.isChecked(),
        })

    def validate(self):
        output_name = self.output_folder_edit.text().strip()
        if (
            not output_name
            or output_name in {".", ".."}
            or any(character in output_name for character in '<>:"/\\|?*')
        ):
            self.output_folder_edit.setFocus()
            raise ValueError("智能剪辑输出目录必须是普通文件夹名称，不能包含路径或特殊字符。")
        if self.severe_similarity_spinbox.value() >= self.pass_similarity_spinbox.value():
            self.severe_similarity_spinbox.setFocus()
            raise ValueError("智能剪辑的严重异常阈值必须低于自动通过阈值。")
        if (
            self.voice_detection_checkbox.isChecked()
            and self.vad_neg_threshold_spinbox.value()
            >= self.vad_threshold_spinbox.value()
        ):
            self.vad_neg_threshold_spinbox.setFocus()
            raise ValueError("人声结束阈值必须低于人声检测开始阈值。")
        if (
            self.compress_pauses_checkbox.isChecked()
            and (
                self.pause_keep_before_spinbox.value()
                + self.pause_keep_after_spinbox.value()
            ) >= self.pause_threshold_spinbox.value()
        ):
            self.pause_keep_after_spinbox.setFocus()
            raise ValueError(
                "智能剪辑向前与向后保留量之和必须短于触发压缩的时长。"
            )

    def update_config(self, config):
        config[SMART_VIDEO_EDITOR_CONFIG_KEY] = self.settings()
        return config
