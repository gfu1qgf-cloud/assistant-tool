"""Non-modal music search window; all decoding and inference stays off the GUI thread."""

import logging
import os
from pathlib import Path

from qt_compat import QtCore, QtGui, QtMultimedia, QtWidgets
from model.ClipboardHelper import set_internal_clipboard_text

from .audio import encode_reference, media_duration, resolve_tools
from .encoder import COVERAGE_LABELS, MODEL_SPECS, MusicEncoder, build_music_prompt
from .feedback import MusicFeedback, search_key
from .index import MusicIndex
from .settings import normalize_settings
from .tag_input import MusicTagInput


logger = logging.getLogger(__name__)


def format_time(seconds):
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


class _Worker(QtCore.QThread):
    progressChanged = QtCore.pyqtSignal(int, int, str)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task

    def run(self):
        try:
            result = self.task(self.progressChanged.emit, self.isInterruptionRequested)
            self.completed.emit(result)
        except Exception as exc:
            logger.exception("智能搜音乐后台任务失败")
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _MediaDropEdit(QtWidgets.QLineEdit):
    pathDropped = QtCore.pyqtSignal()

    def __init__(self, placeholder, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText(placeholder)

    def dragEnterEvent(self, event):
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile() and Path(url.toLocalFile()).is_file():
                self.setText(url.toLocalFile())
                self.pathDropped.emit()
                event.acceptProposedAction()
                return
        super().dropEvent(event)


class _DraggableMusicResults(QtWidgets.QTableWidget):
    """Expose original audio files to DaVinci/Explorer without moving the library."""

    def __init__(self, parent=None):
        super().__init__(0, 5, parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(False)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(QtCore.Qt.DropAction.CopyAction)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)

    def mimeData(self, items):
        mime = QtCore.QMimeData()
        paths = []
        seen = set()
        for item in items:
            path = str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "")
            if path and path not in seen and Path(path).is_file():
                paths.append(path)
                seen.add(path)
        mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in paths])
        return mime

    def startDrag(self, _supported_actions):
        mime = self.mimeData(self.selectedItems())
        if not mime.hasUrls():
            return
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        drag.exec(QtCore.Qt.DropAction.CopyAction, QtCore.Qt.DropAction.CopyAction)


class SmartMusicSearchDialog(QtWidgets.QDialog):
    settingsChanged = QtCore.pyqtSignal(object)

    def __init__(self, settings, parent=None, index=None, encoder=None):
        super().__init__(parent)
        self.setWindowTitle("智能搜音乐")
        self.resize(1010, 690)
        self.settings = normalize_settings(settings)
        self._managed_encoder = encoder is None
        self._managed_index = index is None
        self.encoder = encoder or MusicEncoder(self.settings["model_key"], self.settings["coverage"])
        self.index = index or MusicIndex.for_encoder(self.encoder)
        self.feedback = MusicFeedback(self.index.root)
        self._feedback_key = ""
        self._pending_settings = None
        self.worker = None
        self._task_kind = ""
        self._results = []
        self._shown_count = 0
        self._player = None
        self._audio_output = None
        self._pending_seek_ms = None
        self._preview_end_ms = None
        self._seek_dragging = False
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "可用文字搜索，或拖入参考音频 / 视频，按所选位置起约 10 秒的声音找相似配乐。"
            "首次建索引较慢，之后只处理新增或变化的歌曲；原文件只读。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        line = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel()
        line.addWidget(self.status, 1)
        self.index_button = QtWidgets.QPushButton("建立 / 更新索引")
        self.index_button.clicked.connect(self.update_index)
        line.addWidget(self.index_button)
        self.stop_button = QtWidgets.QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.cancel_work)
        line.addWidget(self.stop_button)
        layout.addLayout(line)
        model_line = QtWidgets.QHBoxLayout()
        model_line.addWidget(QtWidgets.QLabel("检索模型"))
        self.model_combo = QtWidgets.QComboBox()
        for key, spec in MODEL_SPECS.items():
            self.model_combo.addItem(spec["label"], key)
        self.model_combo.setCurrentIndex(self.model_combo.findData(self.settings["model_key"]))
        self.model_combo.setToolTip("可切回旧模型对比；新旧索引分别保存，切换不会加载模型或覆盖旧记录。")
        model_line.addWidget(self.model_combo)
        model_line.addWidget(QtWidgets.QLabel("音乐覆盖"))
        self.coverage_combo = QtWidgets.QComboBox()
        for key, label in COVERAGE_LABELS.items():
            self.coverage_combo.addItem(label, key)
        self.coverage_combo.setCurrentIndex(self.coverage_combo.findData(self.settings["coverage"]))
        self.coverage_combo.setToolTip("精细：每约10秒，最多256段；均衡：每约20秒，最多128段。旧版抽样仅3～8段。")
        model_line.addWidget(self.coverage_combo)
        self.use_feedback = QtWidgets.QCheckBox("参考我的喜好")
        self.use_feedback.setChecked(self.settings["use_feedback"])
        self.use_feedback.setToolTip("结果右键标记符合／不符合本次情绪；只影响相同搜索条件，不训练模型。")
        model_line.addWidget(self.use_feedback)
        model_line.addStretch(1)
        self.model_combo.currentIndexChanged.connect(self._search_options_changed)
        self.coverage_combo.currentIndexChanged.connect(self._search_options_changed)
        self.use_feedback.toggled.connect(self._search_options_changed)
        layout.addLayout(model_line)
        self.progress = QtWidgets.QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)

        query_line = QtWidgets.QHBoxLayout()
        self.tag_input = MusicTagInput(self)
        self.query = self.tag_input.query
        self.query.returnPressed.connect(self.search)
        query_line.addWidget(self.tag_input, 1)
        self.filename_match = QtWidgets.QCheckBox("匹配文件名")
        self.filename_match.setToolTip(
            "默认关闭。启用后，歌名直接命中的结果会单独置顶；不会改变音频语义相似度。"
        )
        query_line.addWidget(self.filename_match)
        self.search_button = QtWidgets.QPushButton("搜索")
        self.search_button.clicked.connect(self.search)
        query_line.addWidget(self.search_button)
        layout.addLayout(query_line)

        reference_line = QtWidgets.QHBoxLayout()
        reference_line.addWidget(QtWidgets.QLabel("参考音频 / 视频"))
        self.reference_path = _MediaDropEdit("拖入参考文件，按实际声音找相似配乐")
        reference_line.addWidget(self.reference_path, 1)
        reference_choose = QtWidgets.QPushButton("选择…")
        reference_choose.clicked.connect(self._choose_reference)
        reference_line.addWidget(reference_choose)
        layout.addLayout(reference_line)
        reference_options = QtWidgets.QHBoxLayout()
        reference_options.addWidget(QtWidgets.QLabel("参考起点"))
        self.reference_start = QtWidgets.QSpinBox()
        self.reference_start.setRange(0, 48 * 3600 - 1)
        self.reference_start.setSuffix(" 秒")
        self.reference_start.setToolTip("截取此处开始约 10 秒的声音；视频中有人声时，建议选配乐较清晰的位置")
        reference_options.addWidget(self.reference_start)
        self.reference_button = QtWidgets.QPushButton("找相似配乐")
        self.reference_button.clicked.connect(self.search_reference)
        reference_options.addWidget(self.reference_button)
        reference_options.addWidget(QtWidgets.QLabel("视频含口播时，结果可能受到人声影响"))
        reference_options.addStretch(1)
        layout.addLayout(reference_options)

        duration_line = QtWidgets.QHBoxLayout()
        duration_line.addWidget(QtWidgets.QLabel("视频时长"))
        self.duration = QtWidgets.QSpinBox()
        self.duration.setRange(0, 12 * 3600)
        self.duration.setSpecialValueText("不限")
        self.duration.setSuffix(" 秒")
        self.duration.setToolTip("留空不筛选时长；输入时优先找长度足够、能连续使用的音乐")
        duration_line.addWidget(self.duration)
        self.video_path = _MediaDropEdit("可选：拖入视频自动读取时长")
        self.video_path.pathDropped.connect(self.read_video_duration)
        duration_line.addWidget(self.video_path, 1)
        choose = QtWidgets.QPushButton("选择视频…")
        choose.clicked.connect(self._choose_video)
        duration_line.addWidget(choose)
        read = QtWidgets.QPushButton("读取时长")
        read.clicked.connect(self.read_video_duration)
        duration_line.addWidget(read)
        self.include_short = QtWidgets.QCheckBox("也显示短曲")
        duration_line.addWidget(self.include_short)
        layout.addLayout(duration_line)

        self.english = QtWidgets.QLabel("")
        self.english.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.english)
        self.table = _DraggableMusicResults(self)
        self.table.setHorizontalHeaderLabels(["匹配方式 / 相似度", "歌曲", "所在目录", "曲长", "建议试听起点"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self.preview_selected)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.table, 1)
        drag_hint = QtWidgets.QLabel("可选中一首或多首，直接拖到达芬奇；只引用原文件，不会移动配乐库。")
        drag_hint.setWordWrap(True)
        layout.addWidget(drag_hint)
        seek_line = QtWidgets.QHBoxLayout()
        self.preview_current_time = QtWidgets.QLabel("0:00")
        seek_line.addWidget(self.preview_current_time)
        self.seek_back_button = QtWidgets.QPushButton("-10 秒")
        self.seek_back_button.clicked.connect(lambda: self._seek_preview_relative(-10_000))
        seek_line.addWidget(self.seek_back_button)
        self.seek_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal, self)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setEnabled(False)
        self.seek_slider.setToolTip("拖动以跳转试听位置；左右按钮每次跳转 10 秒")
        self.seek_slider.sliderPressed.connect(self._seek_pressed)
        self.seek_slider.sliderMoved.connect(self._seek_moved)
        self.seek_slider.sliderReleased.connect(self._seek_released)
        seek_line.addWidget(self.seek_slider, 1)
        self.seek_forward_button = QtWidgets.QPushButton("+10 秒")
        self.seek_forward_button.clicked.connect(lambda: self._seek_preview_relative(10_000))
        seek_line.addWidget(self.seek_forward_button)
        self.preview_total_time = QtWidgets.QLabel("0:00")
        seek_line.addWidget(self.preview_total_time)
        layout.addLayout(seek_line)
        bottom = QtWidgets.QHBoxLayout()
        self.preview_button = QtWidgets.QPushButton("试听选中片段")
        self.preview_button.clicked.connect(self.preview_selected)
        bottom.addWidget(self.preview_button)
        self.pause_button = QtWidgets.QPushButton("停止试听")
        self.pause_button.clicked.connect(self.stop_preview)
        bottom.addWidget(self.pause_button)
        self.more_button = QtWidgets.QPushButton("加载更多")
        self.more_button.setEnabled(False)
        self.more_button.clicked.connect(self._append_results)
        bottom.addWidget(self.more_button)
        bottom.addStretch(1)
        bottom.addWidget(QtWidgets.QLabel("相似度仅用于排序，仍需试听确认。"))
        layout.addLayout(bottom)
        self._refresh_count()

    def _selected_tags(self):
        return self.tag_input.selected_tags()

    def update_settings(self, settings):
        if self.is_busy():
            self._pending_settings = normalize_settings(settings)
            return
        updated = normalize_settings(settings)
        changed = any(updated[key] != self.settings[key] for key in ("model_key", "coverage"))
        self.settings = updated
        if changed and self._managed_encoder:
            self.stop_preview()
            self.encoder = MusicEncoder(updated["model_key"], updated["coverage"])
            if self._managed_index:
                self.index = MusicIndex.for_encoder(self.encoder, self.index.root)
            self._feedback_key = ""
            self._show_results([])
            self.english.clear()
        for widget, key in ((self.model_combo, "model_key"), (self.coverage_combo, "coverage")):
            with QtCore.QSignalBlocker(widget):
                widget.setCurrentIndex(widget.findData(updated[key]))
        with QtCore.QSignalBlocker(self.use_feedback):
            self.use_feedback.setChecked(updated["use_feedback"])
        self._refresh_count()

    def _search_options_changed(self, *_args):
        if self.is_busy():
            return
        updated = {**self.settings, "model_key": self.model_combo.currentData(),
                   "coverage": self.coverage_combo.currentData(),
                   "use_feedback": self.use_feedback.isChecked()}
        self.update_settings(updated)
        if self._results:
            self._show_results(self.feedback.apply(self._results, self._feedback_key,
                                                   self.settings["use_feedback"]))
        self.settingsChanged.emit(dict(self.settings))

    def _refresh_count(self):
        count = self.index.count(self.encoder.model_id)
        hint = "（当前模型尚未建索引；旧索引保留）" if not count else ""
        self.status.setText(f"已索引 {count} 首{hint} | 配乐库："
                            f"{self.settings['library_root']}")

    def is_busy(self):
        # Retain ownership until the queued finished handler runs. Otherwise a
        # second task can start before the first finished signal is processed.
        return self.worker is not None

    def _start(self, kind, task):
        if self.is_busy():
            return
        self._task_kind = kind
        self.worker = _Worker(task, self)
        self.worker.progressChanged.connect(self._progress)
        self.worker.completed.connect(self._completed)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._finished)
        self.index_button.setEnabled(False)
        self.search_button.setEnabled(False)
        self.reference_button.setEnabled(False)
        self.model_combo.setEnabled(False)
        self.coverage_combo.setEnabled(False)
        self.use_feedback.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.worker.start()

    def _progress(self, done, total, message):
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.status.setText(message)

    def _finished(self):
        self.index_button.setEnabled(True)
        self.search_button.setEnabled(True)
        self.reference_button.setEnabled(True)
        self.model_combo.setEnabled(True)
        self.coverage_combo.setEnabled(True)
        self.use_feedback.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.progress.hide()
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()
        if self._pending_settings is not None:
            updated, self._pending_settings = self._pending_settings, None
            self.update_settings(updated)

    def _failed(self, message):
        self.status.setText("智能搜音乐出错：" + message)
        QtWidgets.QMessageBox.warning(self, "智能搜音乐", message)

    def _completed(self, result):
        if self._task_kind == "index":
            self._refresh_count()
            suffix = "（已暂停，可继续增量更新）" if result["cancelled"] else ""
            self.status.setText(
                f"已索引 {self.index.count(self.encoder.model_id)} 首；本次更新 "
                f"{result['updated']} 首，跳过过短 {result['skipped']} 首，"
                f"失败 {result['failed']} 首{suffix}"
            )
            if result["failed"]:
                failures = self.index.failures(self.encoder.model_id)
                QtWidgets.QMessageBox.warning(self, "部分音频无法索引",
                    "失败文件已保留记录，修改源文件后可以重试。\n" +
                    "\n".join(f"{Path(path).name}: {error}" for path, error in failures[:12]))
        elif self._task_kind in ("search", "reference"):
            if self._task_kind == "search":
                rows, translated, filename_count = result
            else:
                rows, translated = result
                filename_count = 0
            self.english.setText(
                ("参考片段：" if self._task_kind == "reference" else "模型检索描述：")
                + translated
            )
            self._show_results(rows)
            self.status.setText(
                f"找到 {len(rows)} 首候选"
                + (f"（文件名直接命中 {filename_count} 首）" if filename_count else "")
                + f"，已显示 {self._shown_count} 首；"
                "双击试听，拖到达芬奇可直接使用原文件"
            )
        elif self._task_kind == "duration":
            self.duration.setValue(max(1, int(round(result))))
            self.status.setText(f"视频长度 {format_time(result)}；下次搜索会使用此长度")

    def cancel_work(self):
        if self.is_busy():
            self.worker.requestInterruption()
            self.status.setText("正在停止，请等当前音频片段处理完成…")

    def update_index(self):
        if self.is_busy():
            return
        root = self.settings["library_root"]
        if not Path(root).is_dir():
            QtWidgets.QMessageBox.warning(self, "配乐库不可用", "请在插件设置中选择可访问的配乐库目录。")
            return
        try:
            ffmpeg, ffprobe = resolve_tools(self.settings["ffmpeg_path"])
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "音频工具不可用", str(exc))
            return
        if not self.encoder.music_is_cached():
            answer = QtWidgets.QMessageBox.question(
                self, "首次下载模型",
                f"当前选择：{getattr(self.encoder, 'label', '音乐模型')}。\n"
                "建立语义索引需要首次下载约 615～776 MB 的本地音乐模型。"
                "这只在首次使用时进行；库中歌曲不会上传。继续吗？"
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self._start("index", lambda progress, cancelled: self.index.sync(
            root, self.encoder, ffmpeg, ffprobe, progress, cancelled
        ))

    def search(self):
        if self.is_busy():
            return
        query = self.query.text().strip()
        moods, sounds = self._selected_tags()
        if not query and not moods and not sounds:
            self.query.setFocus()
            return
        self._show_results([])
        self.english.setText("")
        self._feedback_key = search_key(query, moods, sounds)
        feedback_key = self._feedback_key
        use_feedback = self.settings["use_feedback"]
        stable = self.settings["coverage"] != "legacy"
        seconds = self.duration.value()
        include_short = self.include_short.isChecked()
        filename_hits = (
            self.index.search_filename(query, seconds=seconds)
            if self.filename_match.isChecked() and query else []
        )
        if filename_hits:
            self._show_results(filename_hits)
            self.english.setText("文件名直接命中；这部分不使用音频相似度评分")
            self.status.setText(f"文件名找到 {len(filename_hits)} 首；音频语义结果正在准备…")
        if not self.index.count(self.encoder.model_id):
            if filename_hits:
                self.status.setText(f"文件名找到 {len(filename_hits)} 首；要搜索相似声音，请先更新索引")
            else:
                QtWidgets.QMessageBox.information(self, "尚未建索引", "请先点击“建立 / 更新索引”。")
            return
        if (query not in moods and query not in sounds
                and self.encoder.needs_translation(query)
                and not self.encoder.translation_is_cached()):
            answer = QtWidgets.QMessageBox.question(
                self, "首次下载中文翻译模型",
                "中文搜索需要首次下载约 312 MB 的本地翻译模型。"
                "查询不会发送给在线翻译服务。继续吗？"
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        def work(_progress, cancelled):
            if cancelled():
                return filename_hits, "", len(filename_hits)
            prompt = build_music_prompt(query, moods, sounds, self.encoder.translate)
            vector, translated = self.encoder.text(prompt)
            if cancelled():
                return filename_hits, translated, len(filename_hits)
            semantic = self.index.search(self.encoder.model_id, vector, seconds=seconds,
                                         limit=None, include_short=include_short, stable=stable)
            named_paths = {os.path.normcase(row["path"]) for row in filename_hits}
            rows = filename_hits + [row for row in semantic
                                    if os.path.normcase(row["path"]) not in named_paths]
            return self.feedback.apply(rows, feedback_key, use_feedback), translated, len(filename_hits)
        self._start("search", work)

    def search_reference(self):
        if self.is_busy():
            return
        path = Path(self.reference_path.text().strip())
        if not path.is_file():
            QtWidgets.QMessageBox.warning(self, "参考文件不存在", "请拖入或选择一个本地音频 / 视频文件。")
            return
        if not self.index.count(self.encoder.model_id):
            QtWidgets.QMessageBox.information(self, "尚未建索引", "请先点击“建立 / 更新索引”。")
            return
        try:
            ffmpeg, ffprobe = resolve_tools(self.settings["ffmpeg_path"])
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "音频工具不可用", str(exc))
            return
        start = self.reference_start.value()
        seconds = self.duration.value()
        include_short = self.include_short.isChecked()
        self._feedback_key = ""  # Emotion feedback must not leak into audio-to-audio searches.
        stable = self.settings["coverage"] != "legacy"
        def work(_progress, cancelled):
            if cancelled():
                return [], ""
            vector = encode_reference(path, start, self.encoder, ffmpeg, ffprobe)
            if cancelled():
                return [], ""
            rows = self.index.search(self.encoder.model_id, vector, seconds=seconds,
                                     limit=None, include_short=include_short, stable=stable)
            # An indexed reference song is not a useful "similar" recommendation.
            source = os.path.normcase(os.path.abspath(path))
            rows = [row for row in rows
                    if os.path.normcase(os.path.abspath(row["path"])) != source]
            return rows, f"{path.name}，从 {format_time(start)} 起约 10 秒"
        self._start("reference", work)

    def _show_results(self, rows):
        self._results = rows
        self._shown_count = 0
        self.table.setRowCount(0)
        self._append_results()

    def _append_results(self):
        start = self._shown_count
        end = min(len(self._results), start + self.settings["result_limit"])
        self.table.setRowCount(end)
        root = Path(self.settings["library_root"])
        for row_index in range(start, end):
            result = self._results[row_index]
            path = Path(result["path"])
            try:
                folder = str(path.parent.relative_to(root))
            except ValueError:
                folder = str(path.parent)
            match_label = "文件名命中" if result.get("match_type") == "filename" else f"{result.get('audio_score', result['score']):.3f}"
            preference = result.get("preference", 0)
            if preference:
                match_label += "·符合" if preference > 0 else "·不符"
            fields = [match_label, path.name, folder,
                      format_time(result["duration"]), format_time(result["start"])]
            for column, value in enumerate(fields):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(str(path))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, str(path))
                self.table.setItem(row_index, column, item)
            if result.get("match_type") == "filename":
                self.table.item(row_index, 0).setToolTip("文件名直接匹配；不代表音频情绪相似度")
                self.table.item(row_index, 0).setBackground(QtGui.QColor("#e4efff"))
            elif preference:
                self.table.item(row_index, 0).setToolTip(
                    "显示的是原始音频相似度；你曾标记为"
                    + ("符合本次情绪" if preference > 0 else "不符合本次情绪")
                    + ("，排序已参考该偏好。" if self.settings["use_feedback"] else "；偏好排序当前关闭。")
                )
            if result["short"]:
                for column in range(5):
                    self.table.item(row_index, column).setBackground(QtGui.QColor("#fff0d6"))
                self.table.item(row_index, 3).setToolTip("此曲短于当前视频，需要循环或另行剪辑")
        self._shown_count = end
        remaining = len(self._results) - end
        self.more_button.setEnabled(remaining > 0)
        self.more_button.setText(f"加载更多（剩余 {remaining} 首）" if remaining else "已显示全部")
        if start and remaining >= 0:
            self.status.setText(f"已显示 {end}/{len(self._results)} 首候选")

    def _selected(self):
        row = self.table.currentRow()
        return self._results[row] if 0 <= row < len(self._results) else None

    def preview_selected(self, *_args):
        result = self._selected()
        if not result:
            return
        if QtMultimedia is None:
            self._open_file(result["path"])
            return
        if self._player is None:
            self._player = QtMultimedia.QMediaPlayer(self)
            self._audio_output = QtMultimedia.QAudioOutput(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.errorOccurred.connect(
                lambda _error, message: self.status.setText("试听失败：" + message)
            )
            self._player.mediaStatusChanged.connect(self._media_status_changed)
            self._player.positionChanged.connect(self._preview_position_changed)
            self._player.durationChanged.connect(self._preview_duration_changed)
        self._player.stop()
        self._pending_seek_ms = int(result["start"] * 1000)
        self._preview_duration_changed(int(float(result["duration"]) * 1000))
        self.seek_slider.setValue(min(self._pending_seek_ms, self.seek_slider.maximum()))
        self.preview_current_time.setText(format_time(self._pending_seek_ms / 1000))
        self._preview_end_ms = (
            self._pending_seek_ms + self.duration.value() * 1000
            if self.duration.value() else None
        )
        self._player.setSource(QtCore.QUrl.fromLocalFile(result["path"]))
        self._player.setPosition(self._pending_seek_ms)
        self._player.play()
        self.status.setText(f"试听：{Path(result['path']).name}，从 {format_time(result['start'])} 开始")

    def _media_status_changed(self, status):
        if self._pending_seek_ms is None or self._player is None:
            return
        if status in (
            QtMultimedia.QMediaPlayer.MediaStatus.LoadedMedia,
            QtMultimedia.QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._player.setPosition(self._pending_seek_ms)
            self._pending_seek_ms = None

    def _preview_position_changed(self, position):
        if not self._seek_dragging:
            self.seek_slider.setValue(max(0, min(int(position), self.seek_slider.maximum())))
            self.preview_current_time.setText(format_time(position / 1000))
        if self._preview_end_ms is not None and position >= self._preview_end_ms:
            self.stop_preview()

    def _preview_duration_changed(self, duration_ms):
        duration_ms = max(0, int(duration_ms))
        if duration_ms:
            self.seek_slider.setRange(0, duration_ms)
            self.seek_slider.setEnabled(True)
            self.preview_total_time.setText(format_time(duration_ms / 1000))

    def _seek_pressed(self):
        self._seek_dragging = True

    def _seek_moved(self, position):
        self.preview_current_time.setText(format_time(position / 1000))

    def _seek_released(self):
        self._seek_dragging = False
        self._seek_preview_to(self.seek_slider.value())

    def _seek_preview_relative(self, change_ms):
        if self._player is not None and self.seek_slider.isEnabled():
            self._seek_preview_to(self.seek_slider.value() + change_ms)

    def _seek_preview_to(self, position_ms):
        if self._player is None or not self.seek_slider.isEnabled():
            return
        position_ms = max(0, min(int(position_ms), self.seek_slider.maximum()))
        self.seek_slider.setValue(position_ms)
        self.preview_current_time.setText(format_time(position_ms / 1000))
        self._pending_seek_ms = position_ms
        self._preview_end_ms = (
            position_ms + self.duration.value() * 1000 if self.duration.value() else None
        )
        self._player.setPosition(position_ms)
        self._player.play()

    def stop_preview(self):
        if self._player is not None:
            self._player.stop()
        self._pending_seek_ms = None
        self._preview_end_ms = None
        self._seek_dragging = False

    def _context_menu(self, position):
        clicked = self.table.indexAt(position)
        if clicked.isValid():
            self.table.setCurrentCell(clicked.row(), 0)
        result = self._selected()
        if not result:
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction("试听", self.preview_selected)
        menu.addAction("以此曲找相似配乐", lambda: self._reference_from_result(result))
        menu.addAction("复制文件路径", lambda: set_internal_clipboard_text(result["path"]))
        menu.addAction("打开所在目录", lambda: self._open_file(str(Path(result["path"]).parent)))
        if self._feedback_key and not self.is_busy():
            menu.addSeparator()
            menu.addAction("符合本次情绪", lambda: self._mark_feedback(result, 1))
            menu.addAction("不符合本次情绪", lambda: self._mark_feedback(result, -1))
            menu.addAction("清除本次情绪标记", lambda: self._mark_feedback(result, 0))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _mark_feedback(self, result, preference):
        if self.is_busy() or not self._feedback_key:
            return
        try:
            self.feedback.set(self._feedback_key, result["path"], preference)
            rows = self.feedback.apply(self._results, self._feedback_key, self.settings["use_feedback"])
            self._show_results(rows)
            position = next((i for i, row in enumerate(rows) if row["path"] == result["path"]), -1)
            if 0 <= position < self.table.rowCount():
                self.table.setCurrentCell(position, 0)
            self.status.setText("已保存本次搜索条件的情绪偏好；模型和原音乐不变。")
        except Exception as exc:
            logger.exception("保存音乐情绪偏好失败")
            QtWidgets.QMessageBox.warning(self, "情绪偏好未保存", str(exc))

    @staticmethod
    def _open_file(path):
        if hasattr(os, "startfile"):
            os.startfile(path)
        else:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _choose_video(self):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择要配乐的视频", "", "视频 (*.mp4 *.mov *.mkv *.avi *.webm);;所有文件 (*)"
        )
        if path:
            self.video_path.setText(path)
            self.read_video_duration()

    def _choose_reference(self):
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择参考音频或视频", "",
            "媒体 (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus *.wma *.mp4 *.mov *.mkv *.avi *.webm);;所有文件 (*)"
        )
        if path:
            self.reference_path.setText(path)

    def _reference_from_result(self, result):
        self.reference_path.setText(result["path"])
        self.reference_start.setValue(int(result["start"]))
        self.search_reference()

    def read_video_duration(self):
        path = self.video_path.text().strip()
        if not Path(path).is_file():
            QtWidgets.QMessageBox.warning(self, "视频不存在", "请拖入视频或选择视频文件。")
            return
        try:
            _ffmpeg, ffprobe = resolve_tools(self.settings["ffmpeg_path"])
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "音频工具不可用", str(exc))
            return
        self._start("duration", lambda _progress, _cancelled: media_duration(path, ffprobe))

    def closeEvent(self, event):
        self.tag_input.picker.close()
        if self.is_busy():
            self.hide()
            event.ignore()
            return
        self.stop_preview()
        super().closeEvent(event)
