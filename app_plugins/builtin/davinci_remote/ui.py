"""PyQt interface for Resolve batch export, subtitle review, and track fill."""

import json
import os
import sys
from pathlib import Path

from qt_compat import QtCore, QtWidgets
from app_paths import APP_ROOT
from davinci_remote_worker import RESULT_PREFIX


def _spin(value, minimum=0, maximum=9999, parent=None):
    widget = QtWidgets.QSpinBox(parent)
    widget.setRange(minimum, maximum)
    widget.setValue(int(value))
    return widget


def _check(label, value=False, parent=None):
    widget = QtWidgets.QCheckBox(label, parent)
    widget.setChecked(bool(value))
    return widget


def _combo(choices, value, parent=None):
    widget = QtWidgets.QComboBox(parent)
    for label, key in choices:
        widget.addItem(label, key)
    index = widget.findData(value)
    widget.setCurrentIndex(max(index, 0))
    return widget


class DaVinciRemotePanel(QtWidgets.QWidget):
    """Compact controls mounted by PluginHost in the main remote tab."""

    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("达芬奇工具", self))
        self.status = QtWidgets.QLabel("尚未检测达芬奇连接", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        refresh = QtWidgets.QPushButton("连接并刷新时间线", self)
        refresh.clicked.connect(lambda: plugin.open_dialog(probe=True))
        layout.addWidget(refresh)
        for index, label in enumerate(("批量导出", "字幕核对", "轨道填充 / 水印")):
            button = QtWidgets.QPushButton(label, self)
            button.clicked.connect(lambda _checked=False, page=index: plugin.open_dialog(page))
            layout.addWidget(button)
        layout.addStretch(1)


class SubtitleReviewDialog(QtWidgets.QDialog):
    def __init__(self, reviews, summary, ai_prompt, jump_callback, parent=None):
        super().__init__(parent)
        self.setWindowTitle("达芬奇字幕核对结果")
        self.resize(1000, 700)
        self.reviews = reviews
        self.ai_prompt = ai_prompt
        self.jump_callback = jump_callback
        root = QtWidgets.QVBoxLayout(self)
        self.summary = QtWidgets.QPlainTextEdit(self)
        self.summary.setReadOnly(True)
        self.summary.setPlainText(summary)
        self.summary.setMaximumHeight(180)
        root.addWidget(self.summary)
        splitter = QtWidgets.QSplitter(self)
        self.table = QtWidgets.QTableWidget(len(reviews), 4, splitter)
        self.table.setHorizontalHeaderLabels(("字幕", "判定", "起始帧", "建议预览"))
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        detail = QtWidgets.QWidget(splitter)
        detail_layout = QtWidgets.QVBoxLayout(detail)
        detail_layout.addWidget(QtWidgets.QLabel("时间线原字幕"))
        self.original = QtWidgets.QPlainTextEdit(detail)
        self.original.setReadOnly(True)
        detail_layout.addWidget(self.original)
        detail_layout.addWidget(QtWidgets.QLabel("建议文字 / 缺失内容"))
        self.suggested = QtWidgets.QPlainTextEdit(detail)
        self.suggested.setReadOnly(True)
        detail_layout.addWidget(self.suggested)
        splitter.addWidget(self.table)
        splitter.addWidget(detail)
        splitter.setSizes([430, 570])
        root.addWidget(splitter, 1)
        buttons = QtWidgets.QHBoxLayout()
        jump = QtWidgets.QPushButton("定位到时间线", self)
        jump.clicked.connect(self._jump)
        buttons.addWidget(jump)
        copy = QtWidgets.QPushButton("复制建议", self)
        copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(
            self.suggested.toPlainText()
        ))
        buttons.addWidget(copy)
        copy_ai = QtWidgets.QPushButton("复制 AI 核对提示词", self)
        copy_ai.setEnabled(bool(ai_prompt))
        copy_ai.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(ai_prompt))
        buttons.addWidget(copy_ai)
        buttons.addStretch(1)
        root.addLayout(buttons)
        for row, review in enumerate(reviews):
            values = (
                str(review.get("index", "")), str(review.get("color_name", "")),
                str(review.get("start", "")),
                str(review.get("review_text") or "").replace("\n", " ")[:100],
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        self.table.itemSelectionChanged.connect(self._select)
        if reviews:
            self.table.selectRow(0)

    def _select(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.reviews):
            review = self.reviews[row]
            self.original.setPlainText(str(review.get("original") or ""))
            self.suggested.setPlainText(str(review.get("review_text") or ""))

    def _jump(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.reviews):
            self.jump_callback(int(self.reviews[row]["start"]))


class DaVinciRemoteDialog(QtWidgets.QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle("达芬奇遥控器")
        self.resize(900, 760)
        self.setMinimumSize(680, 520)
        self.process = None
        self._buffer = ""
        self._action = ""
        self._review_dialog = None
        root = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.connection = QtWidgets.QLabel("尚未连接达芬奇", self)
        self.connection.setWordWrap(True)
        top.addWidget(self.connection, 1)
        refresh = QtWidgets.QPushButton("刷新当前项目 / 时间线", self)
        refresh.clicked.connect(self.probe)
        top.addWidget(refresh)
        root.addLayout(top)
        self.tabs = QtWidgets.QTabWidget(self)
        root.addWidget(self.tabs, 1)
        self._build_export_tab()
        self._build_subtitle_tab()
        self._build_track_tab()
        root.addWidget(QtWidgets.QLabel("执行日志（错误也会写入主程序日志）", self))
        self.log = QtWidgets.QPlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(500)
        self.log.setMaximumHeight(150)
        root.addWidget(self.log)

    def _page(self, title):
        page = QtWidgets.QWidget(self.tabs)
        scroll = QtWidgets.QScrollArea(self.tabs)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, title)
        layout = QtWidgets.QVBoxLayout(page)
        return page, layout

    def _stored(self, section, key, default):
        value = self.plugin.settings.get(section, {})
        return value.get(key, default) if isinstance(value, dict) else default

    def _build_export_tab(self):
        page, outer = self._page("批量导出")
        form = QtWidgets.QFormLayout()
        outer.addLayout(form)
        self.export_name = QtWidgets.QLineEdit("", page)
        self.export_name.setPlaceholderText("刷新时间线后自动填入日期或时间线名称")
        form.addRow("任务名", self.export_name)
        self.export_category = _combo((("reels", "reels"), ("口播", "口播")),
                                      self._stored("export", "category", "reels"), page)
        form.addRow("分类", self.export_category)
        self.export_root = QtWidgets.QLineEdit(str(self._stored(
            "export", "output_root", str(Path.home() / "Desktop" / "任务"))), page)
        root_row = QtWidgets.QHBoxLayout()
        root_row.addWidget(self.export_root)
        browse = QtWidgets.QPushButton("浏览…", page)
        browse.clicked.connect(self._browse_export_root)
        root_row.addWidget(browse)
        form.addRow("输出根目录", root_row)
        self.export_preset = QtWidgets.QLineEdit(str(self._stored(
            "export", "render_preset", "MyExportSetting")), page)
        form.addRow("渲染预设", self.export_preset)
        self.export_begin = _spin(self._stored("export", "begin_index", 1), 1, 99999, page)
        form.addRow("起始编号", self.export_begin)
        self.export_audio = _spin(self._stored("export", "audio_track", 2), 1, 64, page)
        form.addRow("参考音轨", self.export_audio)
        self.export_video = _spin(self._stored("export", "video_track", 1), 1, 64, page)
        form.addRow("参考视频轨", self.export_video)
        self.export_name_source = _combo((("时间线名", "timeline"), ("音频片段名", "clip")),
                                         self._stored("export", "name_source", "timeline"), page)
        form.addRow("成品命名来源", self.export_name_source)
        self.export_index_name = _check("文件名前加编号", self._stored(
            "export", "include_index_in_name", False), page)
        outer.addWidget(self.export_index_name)
        self.export_preflight = _check("导出前检查音视频逐帧对齐；问题片段标橙色", self._stored(
            "export", "preflight_alignment", True), page)
        outer.addWidget(self.export_preflight)
        self.export_autostart = _check("创建后自动开始渲染", self._stored(
            "export", "auto_start_render", False), page)
        outer.addWidget(self.export_autostart)
        run = QtWidgets.QPushButton("创建渲染任务", page)
        run.clicked.connect(self._run_export)
        outer.addWidget(run)
        outer.addStretch(1)

    def _browse_export_root(self):
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择输出根目录", self.export_root.text()
        )
        if chosen:
            self.export_root.setText(chosen)

    def _export_settings(self):
        return {
            "task_name": self.export_name.text().strip(),
            "category": self.export_category.currentData(),
            "output_root": self.export_root.text().strip(),
            "render_preset": self.export_preset.text().strip(),
            "begin_index": self.export_begin.value(),
            "audio_track": self.export_audio.value(),
            "video_track": self.export_video.value(),
            "name_source": self.export_name_source.currentData(),
            "include_index_in_name": self.export_index_name.isChecked(),
            "preflight_alignment": self.export_preflight.isChecked(),
            "auto_start_render": self.export_autostart.isChecked(),
        }

    def _run_export(self):
        settings = self._export_settings()
        if not settings["output_root"] or not settings["render_preset"]:
            QtWidgets.QMessageBox.warning(self, "参数不完整", "请填写输出目录和渲染预设。")
            return
        if settings["auto_start_render"] and QtWidgets.QMessageBox.question(
            self, "确认开始渲染", "将创建任务并立即启动达芬奇渲染，继续吗？"
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.plugin.save_settings("export", settings)
        self._start("export", {"settings": settings})

    def _build_subtitle_tab(self):
        page, outer = self._page("字幕核对")
        self.subtitle_info = QtWidgets.QLabel("刷新时间线后显示当前字幕。", page)
        outer.addWidget(self.subtitle_info)
        self.subtitle_list = QtWidgets.QTableWidget(0, 3, page)
        self.subtitle_list.setHorizontalHeaderLabels(("序号", "起始帧", "当前字幕"))
        self.subtitle_list.horizontalHeader().setStretchLastSection(True)
        self.subtitle_list.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.subtitle_list.setMinimumHeight(130)
        outer.addWidget(self.subtitle_list, 1)
        outer.addWidget(QtWidgets.QLabel("正确文稿（可粘贴整篇；空行可分隔字幕块）", page))
        self.correct_text = QtWidgets.QPlainTextEdit(page)
        self.correct_text.setPlaceholderText("在这里粘贴正确文稿…")
        self.correct_text.setMinimumHeight(150)
        outer.addWidget(self.correct_text, 1)
        options = QtWidgets.QHBoxLayout()
        self.pink_threshold = _spin(self._stored("subtitle", "pink_threshold", 35), 1, 100, page)
        options.addWidget(QtWidgets.QLabel("粉红错误比例 ≥", page))
        options.addWidget(self.pink_threshold)
        options.addWidget(QtWidgets.QLabel("%", page))
        options.addStretch(1)
        outer.addLayout(options)
        self.force_first = _check("文稿从第一个字幕块开始", self._stored(
            "subtitle", "force_first_block", True), page)
        outer.addWidget(self.force_first)
        self.clear_marks = _check("处理前清除旧的核对颜色", self._stored(
            "subtitle", "clear_previous_marks", True), page)
        outer.addWidget(self.clear_marks)
        run = QtWidgets.QPushButton("核对并标记字幕", page)
        run.clicked.connect(self._run_subtitle)
        outer.addWidget(run)

    def _run_subtitle(self):
        settings = {
            "correct_text": self.correct_text.toPlainText().strip(),
            "pink_threshold": self.pink_threshold.value(),
            "force_first_block": self.force_first.isChecked(),
            "clear_previous_marks": self.clear_marks.isChecked(),
        }
        if not settings["correct_text"]:
            QtWidgets.QMessageBox.warning(self, "缺少文稿", "请先粘贴正确文稿。")
            return
        # A full pasted script is task data, not a persistent preference.
        self.plugin.save_settings("subtitle", {
            key: value for key, value in settings.items() if key != "correct_text"
        })
        self._start("subtitle", {"settings": settings})

    def _build_track_tab(self):
        page, outer = self._page("轨道填充 / 水印")
        self.track_tabs = QtWidgets.QTabWidget(page)
        outer.addWidget(self.track_tabs, 1)
        fill = QtWidgets.QWidget(self.track_tabs)
        self.track_tabs.addTab(fill, "轨道铺设")
        fill_outer = QtWidgets.QVBoxLayout(fill)
        form = QtWidgets.QFormLayout()
        fill_outer.addLayout(form)
        modes = (
            ("原版铺设（推荐）", "original_helper"),
            ("精确连续循环（备用）", "precise_continuous"),
            ("快速连续循环", "fast_continuous"),
            ("每个区域从头开始", "restart_each_block"),
        )
        self.fill_mode = _combo(modes, self._stored("track", "mode", "original_helper"), fill)
        form.addRow("铺设模式", self.fill_mode)
        type_choices = (("音频轨", "audio"), ("视频轨", "video"))
        self.fill_source_type = _combo(type_choices, self._stored("track", "source_type", "audio"), fill)
        form.addRow("参照类型", self.fill_source_type)
        self.fill_source_track = _spin(self._stored("track", "source_track", 1), 1, 64, fill)
        form.addRow("参照轨号", self.fill_source_track)
        self.fill_target_type = _combo(type_choices, self._stored("track", "target_type", "audio"), fill)
        form.addRow("目标类型", self.fill_target_type)
        self.fill_target_track = _spin(self._stored("track", "target_track", 2), 1, 64, fill)
        form.addRow("目标轨号", self.fill_target_track)
        self.fill_tolerance = _spin(self._stored("track", "merge_tolerance", 5), 0, 999, fill)
        form.addRow("区间合并容差（帧）", self.fill_tolerance)
        self.fill_media_path = QtWidgets.QLineEdit(str(self._stored("track", "media_file_path", "")), fill)
        media_row = QtWidgets.QHBoxLayout()
        media_row.addWidget(self.fill_media_path)
        media_browse = QtWidgets.QPushButton("浏览…", fill)
        media_browse.clicked.connect(self._browse_fill_media)
        media_row.addWidget(media_browse)
        form.addRow("外部素材（可选）", media_row)
        self.fill_clear = _check("铺设前清空目标轨道", self._stored(
            "track", "clear_target_track", True), fill)
        fill_outer.addWidget(self.fill_clear)
        self.fill_mark = _check("模板从头循环时标记片段", self._stored(
            "track", "mark_loop_start", True), fill)
        fill_outer.addWidget(self.fill_mark)
        fill_run = QtWidgets.QPushButton("开始铺设轨道", fill)
        fill_run.clicked.connect(self._run_track_fill)
        fill_outer.addWidget(fill_run)
        fill_outer.addStretch(1)

        watermark = QtWidgets.QWidget(self.track_tabs)
        self.track_tabs.addTab(watermark, "批量水印")
        watermark_outer = QtWidgets.QVBoxLayout(watermark)
        watermark_form = QtWidgets.QFormLayout()
        watermark_outer.addLayout(watermark_form)
        self.water_source = _spin(self._stored("watermark", "source_track", 1), 1, 64, watermark)
        watermark_form.addRow("参照视频轨", self.water_source)
        self.water_start = _spin(self._stored("watermark", "start_track", 0), 0, 64, watermark)
        self.water_start.setSpecialValueText("自动")
        watermark_form.addRow("起始水印轨", self.water_start)
        self.water_tolerance = _spin(self._stored("watermark", "tolerance", 5), 0, 999, watermark)
        watermark_form.addRow("容差（帧）", self.water_tolerance)
        self.water_preset = {}
        preset_labels = (
            ("main", "版权水印", True),
            ("suno", "版权水印 suno", False),
            ("whatsapp", "MX306-XH", False),
            ("lzx_r", "LZX-R", False),
            ("xh_c", "XH-C", False),
        )
        preset_box = QtWidgets.QGroupBox("预设水印", watermark)
        preset_layout = QtWidgets.QGridLayout(preset_box)
        for index, (key, label, default) in enumerate(preset_labels):
            check = _check(label, self._stored("watermark", key, default), preset_box)
            self.water_preset[key] = check
            preset_layout.addWidget(check, index // 2, index % 2)
        watermark_outer.addWidget(preset_box)
        self.water_custom = QtWidgets.QLineEdit(str(self._stored("watermark", "custom_names", "")), watermark)
        self.water_custom.setPlaceholderText("多个名称用逗号分隔")
        watermark_form.addRow("其他素材名", self.water_custom)
        self.water_template = QtWidgets.QLineEdit(str(self._stored("watermark", "template_tracks", "")), watermark)
        self.water_template.setPlaceholderText("例如 2,3,4：取各轨首片段作为模板")
        watermark_form.addRow("模板轨道", self.water_template)
        self.water_clear = _check("添加前清空目标水印轨", self._stored(
            "watermark", "clear_tracks", True), watermark)
        watermark_outer.addWidget(self.water_clear)
        self.water_continuous = _check("跨区间连续读取模板素材", self._stored(
            "watermark", "continuous_source", False), watermark)
        watermark_outer.addWidget(self.water_continuous)
        water_run = QtWidgets.QPushButton("添加批量水印", watermark)
        water_run.clicked.connect(self._run_watermark)
        watermark_outer.addWidget(water_run)
        watermark_outer.addStretch(1)

    def _browse_fill_media(self):
        chosen, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "选择铺设素材")
        if chosen:
            self.fill_media_path.setText(chosen)

    def _track_settings(self):
        return {
            "operation": "track_fill",
            "mode": self.fill_mode.currentData(),
            "source_type": self.fill_source_type.currentData(),
            "source_track": self.fill_source_track.value(),
            "target_type": self.fill_target_type.currentData(),
            "target_track": self.fill_target_track.value(),
            "merge_tolerance": self.fill_tolerance.value(),
            "mark_loop_start": self.fill_mark.isChecked(),
            "clear_target_track": self.fill_clear.isChecked(),
            "media_file_path": self.fill_media_path.text().strip().strip('"'),
        }

    def _run_track_fill(self):
        settings = self._track_settings()
        if (settings["source_type"], settings["source_track"]) == (
            settings["target_type"], settings["target_track"]
        ):
            QtWidgets.QMessageBox.warning(self, "轨道冲突", "参照轨道和目标轨道不能相同。")
            return
        if settings["media_file_path"] and not Path(settings["media_file_path"]).is_file():
            QtWidgets.QMessageBox.warning(self, "素材不存在", settings["media_file_path"])
            return
        warning = "将按参照轨道铺设目标轨道"
        if settings["clear_target_track"]:
            warning += "，并先清空目标轨道现有片段"
        if QtWidgets.QMessageBox.question(self, "确认修改时间线", warning + "。继续吗？") != (
            QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return
        self.plugin.save_settings("track", settings)
        self._start("track", {"settings": settings})

    def _watermark_settings(self):
        return {
            "operation": "watermark_batch",
            "source_track": self.water_source.value(),
            "start_track": self.water_start.value(),
            "tolerance": self.water_tolerance.value(),
            "clear_tracks": self.water_clear.isChecked(),
            "continuous_source": self.water_continuous.isChecked(),
            "custom_names": self.water_custom.text().strip(),
            "template_tracks": self.water_template.text().strip(),
            **{key: check.isChecked() for key, check in self.water_preset.items()},
        }

    def _run_watermark(self):
        settings = self._watermark_settings()
        if not any(settings[key] for key in self.water_preset) and not (
            settings["custom_names"] or settings["template_tracks"]
        ):
            QtWidgets.QMessageBox.warning(self, "未选择水印", "请至少选择一项水印或模板轨道。")
            return
        warning = "将按参照视频轨批量添加水印"
        if settings["clear_tracks"]:
            warning += "，并清空目标水印轨道"
        if QtWidgets.QMessageBox.question(self, "确认修改时间线", warning + "。继续吗？") != (
            QtWidgets.QMessageBox.StandardButton.Yes
        ):
            return
        self.plugin.save_settings("watermark", settings)
        self._start("track", {"settings": settings})

    def probe(self):
        self._start("probe", {})

    def _jump_to_frame(self, frame):
        self._start("jump", {"frame": frame})

    def _start(self, action, payload):
        if self.process is not None and self.process.state() != QtCore.QProcess.ProcessState.NotRunning:
            QtWidgets.QMessageBox.information(self, "正在执行", "请等待当前达芬奇操作完成。")
            return
        self._action = action
        self._payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._raw_output = bytearray()
        self._pending_lines = ""
        self.log.appendPlainText("→ " + {
            "probe": "检查连接", "export": "批量导出", "subtitle": "字幕核对",
            "track": "轨道 / 水印铺设", "jump": "定位字幕",
        }.get(action, action))
        process = QtCore.QProcess(self)
        self.process = process
        process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        environment = QtCore.QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUTF8", "1")
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(APP_ROOT))
        process.setProgram(sys.executable)
        args = [] if getattr(sys, "frozen", False) else [str(APP_ROOT / "main.py")]
        process.setArguments(args + ["--davinci-worker", action])
        process.readyReadStandardOutput.connect(self._read_output)
        process.started.connect(self._send_payload)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._process_error)
        self.tabs.setEnabled(False)
        process.start()

    def _send_payload(self):
        if self.process is not None:
            self.process.write(self._payload_bytes)
            self.process.closeWriteChannel()

    def _read_output(self):
        if self.process is None:
            return
        chunk = bytes(self.process.readAllStandardOutput())
        self._raw_output.extend(chunk)
        self._pending_lines += chunk.decode("utf-8", errors="replace")
        while "\n" in self._pending_lines:
            line, self._pending_lines = self._pending_lines.split("\n", 1)
            if line and not line.startswith(RESULT_PREFIX):
                self.log.appendPlainText(line.rstrip("\r"))

    def _process_error(self, error):
        if self.process is None:
            return
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self.tabs.setEnabled(True)
            message = "达芬奇工作进程启动失败：" + self.process.errorString()
            self.log.appendPlainText(message)
            self.plugin.context.log(message)
            self.plugin.report_status("连接失败")
            self.process.deleteLater()
            self.process = None

    def _finished(self, exit_code, _exit_status):
        process = self.process
        if process is None:
            return
        self._read_output()
        if self._pending_lines.strip() and not self._pending_lines.startswith(RESULT_PREFIX):
            self.log.appendPlainText(self._pending_lines.strip())
        output = self._raw_output.decode("utf-8", errors="replace")
        self.tabs.setEnabled(True)
        process.deleteLater()
        self.process = None
        try:
            encoded = output.rsplit(RESULT_PREFIX, 1)[1].splitlines()[0]
            result = json.loads(encoded)
        except (IndexError, ValueError, TypeError) as exc:
            result = {"ok": False, "error": "未收到有效执行结果：{}".format(exc)}
        if not result.get("ok"):
            message = str(result.get("error") or "未知错误")
            self.connection.setText("达芬奇操作失败：" + message)
            self.plugin.report_status("操作失败：" + message)
            self.plugin.context.log("达芬奇操作失败：" + message + "\n" + output[-12000:])
            QtWidgets.QMessageBox.warning(self, "达芬奇操作失败", message + "\n\n详细原因已写入程序日志。")
            return
        data = result.get("data") or {}
        self._handle_result(self._action, data)

    def _handle_result(self, action, data):
        if action == "probe":
            status = "已连接：{} / {}（{} 条字幕）".format(
                data.get("project", ""), data.get("timeline", ""),
                data.get("subtitle_count", 0),
            )
            self.connection.setText(status)
            self.plugin.report_status(status)
            task_name = str(data.get("task_name") or "")
            previous_auto = getattr(self, "_last_auto_task_name", "")
            if not self.export_name.text().strip() or self.export_name.text() == previous_auto:
                self.export_name.setText(task_name)
            self._last_auto_task_name = task_name
            subtitles = data.get("subtitles") or []
            self.subtitle_info.setText("当前时间线共 {} 条字幕".format(len(subtitles)))
            self.subtitle_list.setRowCount(len(subtitles))
            for row, item in enumerate(subtitles):
                for column, value in enumerate((
                    item.get("index", ""), item.get("start", ""),
                    item.get("original", ""),
                )):
                    self.subtitle_list.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))
            return
        message = str(data.get("message") or "执行完成")
        self.log.appendPlainText(message)
        self.plugin.context.log(message)
        if action == "subtitle" and data.get("reviews"):
            self._review_dialog = SubtitleReviewDialog(
                data["reviews"], message, str(data.get("ai_review_prompt") or ""),
                self._jump_to_frame, self,
            )
            self._review_dialog.show()
            self._review_dialog.raise_()
        elif action != "jump":
            QtWidgets.QMessageBox.information(self, "达芬奇操作完成", message)
