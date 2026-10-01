"""Non-modal editor for one Facebook/local video contact sheet."""

import hashlib
import logging
from pathlib import Path
import tempfile

from qt_compat import QtCore, QtGui, QtWidgets

from .download import DownloadCanceled, download_facebook_video, is_facebook_video_url
from .engine import AnalysisCanceled, Cut, add_overview_frames, detect_cuts, format_timestamp, render_contact_sheets
from .task_cache import remember_sheets


logger = logging.getLogger(__name__)


class _InputEdit(QtWidgets.QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.setText(urls[0].toLocalFile() if urls[0].isLocalFile() else urls[0].toString())
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class _Worker(QtCore.QThread):
    progressChanged = QtCore.pyqtSignal(str, int)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, *, source, cache_dir, output_dir, browser, profile, sensitivity,
                 min_scene_seconds, max_interval, previous=None, parent=None):
        super().__init__(parent)
        self.source = source
        self.cache_dir = cache_dir
        self.output_dir = output_dir
        self.browser = browser
        self.profile = profile
        self.sensitivity = sensitivity
        self.min_scene_seconds = min_scene_seconds
        self.max_interval = max_interval
        self.previous = previous

    def run(self):
        try:
            if self.previous is None or self.previous.get("redetect"):
                if self.previous is not None:
                    video = Path(self.previous["video"])
                    title = self.previous["title"]
                    if not video.is_file():
                        raise ValueError("临时视频已清理，请重新读取 Facebook 链接。")
                elif is_facebook_video_url(self.source):
                    self.progressChanged.emit("正在后台获取 Facebook 视频", 0)
                    video, title = download_facebook_video(
                        self.source, self.cache_dir,
                        browser=self.browser, profile=self.profile,
                        progress=lambda done, total: self.progressChanged.emit(
                            "正在后台获取 Facebook 视频", int(done / total * 100) if total else 0
                        ),
                        canceled=self.isInterruptionRequested,
                    )
                else:
                    video = Path(self.source)
                    title = video.stem
                    if not video.is_file():
                        raise ValueError("请选择存在的本地视频，或粘贴 Facebook 视频链接。")
                self.progressChanged.emit("正在检测换镜头", 0)
                duration, cuts = detect_cuts(
                    video, sensitivity=self.sensitivity,
                    min_scene_seconds=self.min_scene_seconds,
                    progress=lambda percent: self.progressChanged.emit("正在检测换镜头", percent),
                    canceled=self.isInterruptionRequested,
                )
                cuts = add_overview_frames(cuts, duration, self.max_interval)
            else:
                video = Path(self.previous["video"])
                title = self.previous["title"]
                duration = self.previous["duration"]
                cuts = self.previous["cuts"]
            identifier = hashlib.sha256(self.source.encode("utf-8")).hexdigest()[:8]
            self.progressChanged.emit("正在拼接分镜大图", 0)
            paths = render_contact_sheets(
                video, duration, cuts, self.output_dir,
                title=f"{title}_{identifier}",
                progress=lambda percent: self.progressChanged.emit("正在拼接分镜大图", percent),
                canceled=self.isInterruptionRequested,
            )
            self.completed.emit({"source": self.source, "video": str(video), "title": title, "duration": duration,
                                 "cuts": cuts, "paths": [str(path) for path in paths]})
        except (AnalysisCanceled, DownloadCanceled):
            self.failed.emit("操作已取消。")
        except Exception as exc:
            if self.isInterruptionRequested():
                self.failed.emit("操作已取消。")
            else:
                logger.exception("Facebook 视频分镜处理失败")
                self.failed.emit(f"{type(exc).__name__}: {exc}")


class ContactSheetDialog(QtWidgets.QDialog):
    def __init__(self, logger=None, parent=None):
        super().__init__(parent)
        self._log = logger or (lambda _message: None)
        self._cache = tempfile.TemporaryDirectory(prefix="facebook_contact_sheet_")
        self._worker = None
        self._result = None
        self._source = ""
        self._task_reference = None
        self.setWindowTitle("Facebook 视频分镜速览")
        self.setMinimumSize(930, 730)
        self.resize(1080, 830)
        self.setModal(False)
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        source_row = QtWidgets.QHBoxLayout()
        self.source_edit = _InputEdit(self)
        self.source_edit.setPlaceholderText("粘贴 Facebook 视频 / Reels 链接，或拖入本地视频")
        source_row.addWidget(self.source_edit, 1)
        browse = QtWidgets.QPushButton("选择视频…", self)
        browse.clicked.connect(self._browse_source)
        source_row.addWidget(browse)
        form.addRow("视频", source_row)

        output_row = QtWidgets.QHBoxLayout()
        self.output_edit = QtWidgets.QLineEdit(self)
        desktop = Path.home() / "Desktop"
        self.output_edit.setText(str(desktop if desktop.is_dir() else Path.home()))
        output_row.addWidget(self.output_edit, 1)
        output_browse = QtWidgets.QPushButton("选择目录…", self)
        output_browse.clicked.connect(self._browse_output)
        output_row.addWidget(output_browse)
        form.addRow("导出到", output_row)
        layout.addLayout(form)

        options = QtWidgets.QHBoxLayout()
        self.browser_combo = QtWidgets.QComboBox(self)
        self.browser_combo.addItem("公开链接（不读取登录状态）", "")
        self.browser_combo.addItem("使用 Chrome 登录状态", "chrome")
        self.browser_combo.addItem("使用 Edge 登录状态", "edge")
        self.browser_combo.setToolTip("只有登录后才能观看的视频才需要此项；不会保存账号密码或 Cookie 文件。")
        options.addWidget(self.browser_combo)
        self.profile_edit = QtWidgets.QLineEdit(self)
        self.profile_edit.setPlaceholderText("浏览器配置名（可选，如 Profile 1）")
        self.profile_edit.setMaximumWidth(220)
        self.profile_edit.setToolTip("留空使用浏览器默认配置；多账号时填写 Chrome 配置文件名。")
        options.addWidget(self.profile_edit)
        options.addStretch(1)
        layout.addLayout(options)

        detection_options = QtWidgets.QHBoxLayout()
        detection_options.addWidget(QtWidgets.QLabel("镜头检测", self))
        detection_options.addWidget(QtWidgets.QLabel("灵敏度", self))
        self.sensitivity = QtWidgets.QSpinBox(self)
        self.sensitivity.setRange(1, 10)
        self.sensitivity.setValue(5)
        self.sensitivity.setToolTip("值越高，越容易识别细微的换镜头，也可能增加误判。")
        detection_options.addWidget(self.sensitivity)
        detection_options.addWidget(QtWidgets.QLabel("最短镜头", self))
        self.min_scene = QtWidgets.QDoubleSpinBox(self)
        self.min_scene.setRange(0.2, 10.0)
        self.min_scene.setSingleStep(0.2)
        self.min_scene.setDecimals(1)
        self.min_scene.setSuffix(" 秒")
        self.min_scene.setValue(0.8)
        detection_options.addWidget(self.min_scene)
        detection_options.addWidget(QtWidgets.QLabel("最长补帧间隔", self))
        self.max_interval = QtWidgets.QSpinBox(self)
        self.max_interval.setRange(0, 120)
        self.max_interval.setSpecialValueText("关闭")
        self.max_interval.setSuffix(" 秒")
        self.max_interval.setValue(15)
        self.max_interval.setToolTip("一个镜头持续太久时补几张预览图；这些会标为“定时补帧”，不是检测到的切换。")
        detection_options.addWidget(self.max_interval)
        detection_options.addStretch(1)
        layout.addLayout(detection_options)

        run_row = QtWidgets.QHBoxLayout()
        self.run_button = QtWidgets.QPushButton("读取视频并生成分镜图", self)
        self.run_button.clicked.connect(self._analyze)
        run_row.addWidget(self.run_button)
        self.cancel_button = QtWidgets.QPushButton("取消", self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_work)
        run_row.addWidget(self.cancel_button)
        self.status = QtWidgets.QLabel("等待视频链接或本地视频。", self)
        run_row.addWidget(self.status, 1)
        layout.addLayout(run_row)
        self.progress = QtWidgets.QProgressBar(self)
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        left = QtWidgets.QWidget(splitter)
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.addWidget(QtWidgets.QLabel("检测到的换镜头点（可手动增删）", left))
        self.cuts_table = QtWidgets.QTableWidget(0, 2, left)
        self.cuts_table.setHorizontalHeaderLabels(["时间", "方式"])
        self.cuts_table.horizontalHeader().setStretchLastSection(True)
        self.cuts_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.cuts_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        left_layout.addWidget(self.cuts_table, 1)
        correction = QtWidgets.QHBoxLayout()
        self.new_cut = QtWidgets.QDoubleSpinBox(left)
        self.new_cut.setRange(0, 86400)
        self.new_cut.setDecimals(2)
        self.new_cut.setSuffix(" 秒")
        correction.addWidget(self.new_cut)
        add_button = QtWidgets.QPushButton("添加切点", left)
        add_button.clicked.connect(self._add_cut)
        correction.addWidget(add_button)
        remove_button = QtWidgets.QPushButton("移除选中", left)
        remove_button.clicked.connect(self._remove_cut)
        correction.addWidget(remove_button)
        left_layout.addLayout(correction)
        self.reexport_button = QtWidgets.QPushButton("按修正后的切点重新生成", left)
        self.reexport_button.setEnabled(False)
        self.reexport_button.clicked.connect(self._reexport)
        left_layout.addWidget(self.reexport_button)

        right = QtWidgets.QWidget(splitter)
        right_layout = QtWidgets.QVBoxLayout(right)
        self.summary = QtWidgets.QLabel("分镜大图会显示在这里。", right)
        self.summary.setWordWrap(True)
        right_layout.addWidget(self.summary)
        self.preview = QtWidgets.QLabel(right)
        self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: #1b222c; color: white;")
        scroll = QtWidgets.QScrollArea(right)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.preview)
        right_layout.addWidget(scroll, 1)
        open_row = QtWidgets.QHBoxLayout()
        self.open_image = QtWidgets.QPushButton("打开大图", right)
        self.open_image.clicked.connect(self._open_image)
        open_row.addWidget(self.open_image)
        self.open_folder = QtWidgets.QPushButton("打开导出目录", right)
        self.open_folder.clicked.connect(self._open_folder)
        open_row.addWidget(self.open_folder)
        right_layout.addLayout(open_row)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([350, 680])
        layout.addWidget(splitter, 1)

    def _browse_source(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择视频", "", "视频 (*.mp4 *.mov *.mkv *.webm *.avi);;所有文件 (*)"
        )
        if path:
            self.source_edit.setText(path)

    def _browse_output(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择导出目录", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def is_busy(self):
        return self._worker is not None and self._worker.isRunning()

    def cancel_work(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self.status.setText("正在取消，请等待当前下载或解码步骤结束…")

    def dispose(self):
        if not self.is_busy():
            self._cache.cleanup()

    def clear_task_reference(self):
        self._task_reference = None

    def start_task_reference(self, url, folder):
        if self.is_busy():
            return False
        self._task_reference = (str(url), Path(folder))
        self.source_edit.setText(str(url))
        self.output_edit.setText(str(folder))
        self._analyze()
        return self.is_busy()

    def _analyze(self):
        source = self.source_edit.text().strip()
        if not source:
            QtWidgets.QMessageBox.warning(self, "缺少视频", "请粘贴 Facebook 视频链接或选择本地视频。")
            return
        if source.lower().startswith(("http://", "https://")) and not is_facebook_video_url(source):
            QtWidgets.QMessageBox.warning(self, "链接不支持", "目前只支持 Facebook / fb.watch 视频链接。")
            return
        previous = None
        if self._result is not None and source == self._source:
            previous = {"video": self._result["video"], "title": self._result["title"], "redetect": True}
        else:
            self._result = None
        self._source = source
        self._launch(previous)

    def _reexport(self):
        if self._result is None:
            return
        previous = dict(self._result)
        previous["cuts"] = self._table_cuts()
        self._launch(previous)

    def _launch(self, previous):
        if self.is_busy():
            return
        output_text = self.output_edit.text().strip()
        if not output_text:
            QtWidgets.QMessageBox.warning(self, "缺少目录", "请选择导出目录。")
            return
        output = Path(output_text).expanduser()
        self._worker = _Worker(
            source=self._source,
            cache_dir=str(Path(self._cache.name) / hashlib.sha256(self._source.encode("utf-8")).hexdigest()[:12]),
            output_dir=output,
            browser=self.browser_combo.currentData(), profile=self.profile_edit.text().strip(),
            sensitivity=self.sensitivity.value(), min_scene_seconds=self.min_scene.value(),
            max_interval=self.max_interval.value(),
            previous=previous, parent=self,
        )
        self._worker.progressChanged.connect(self._on_progress)
        self._worker.completed.connect(self._on_completed)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_finished)
        self.run_button.setEnabled(False)
        self.reexport_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self.status.setText("开始处理…")
        self._worker.start()

    def _on_progress(self, message, percent):
        self.status.setText(message)
        self.progress.setValue(max(0, min(100, percent)))

    def _on_completed(self, result):
        self._result = result
        self._fill_cuts(result["cuts"])
        count = len(result["cuts"]) + 1
        self.summary.setText(
            f"共 {count} 个镜头 · 视频 {format_timestamp(result['duration'])} · "
            f"已导出 {len(result['paths'])} 张大图\n" + "\n".join(result["paths"])
        )
        self.status.setText("完成。可修正切点后重新生成。")
        self.progress.setValue(100)
        self._show_preview(result["paths"][0])
        self._log(f"分镜生成完成：{count} 个镜头，{len(result['paths'])} 张大图。")
        if self._task_reference is not None:
            url, folder = self._task_reference
            if result.get("source") == url and all(
                Path(path).parent.resolve() == folder.resolve() for path in result["paths"]
            ):
                try:
                    remember_sheets(folder, url, result["paths"])
                    self._log(f"任务参考大图已缓存到：{folder}")
                except (OSError, ValueError) as error:
                    self.status.setText("大图已生成，但缓存索引写入失败；请查看程序日志。")
                    self._log(f"任务参考大图缓存索引失败：{error}")
                local = result["paths"][0] if len(result["paths"]) == 1 else str(folder)
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(local)))

    def _on_failed(self, message):
        self.status.setText(message)
        self._log(f"视频分镜失败：{message}")
        if message != "操作已取消。":
            QtWidgets.QMessageBox.warning(self, "视频分镜失败", message)

    def _on_finished(self):
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.reexport_button.setEnabled(self._result is not None)

    def _fill_cuts(self, cuts):
        self.cuts_table.setRowCount(len(cuts))
        for row, cut in enumerate(cuts):
            time_item = QtWidgets.QTableWidgetItem(f"{format_timestamp(cut.seconds)}  ({cut.seconds:.2f}s)")
            time_item.setData(QtCore.Qt.ItemDataRole.UserRole, cut.seconds)
            self.cuts_table.setItem(row, 0, time_item)
            self.cuts_table.setItem(row, 1, QtWidgets.QTableWidgetItem(cut.kind))

    def _table_cuts(self):
        cuts = []
        for row in range(self.cuts_table.rowCount()):
            item = self.cuts_table.item(row, 0)
            kind = self.cuts_table.item(row, 1)
            cuts.append(Cut(float(item.data(QtCore.Qt.ItemDataRole.UserRole)), kind.text() if kind else "手动", 1.0))
        return sorted(cuts, key=lambda cut: cut.seconds)

    def _add_cut(self):
        if self._result is None or self.is_busy():
            return
        seconds = self.new_cut.value()
        if not 0.05 < seconds < self._result["duration"] - 0.05:
            QtWidgets.QMessageBox.information(self, "时间无效", "切点必须在视频内部。")
            return
        cuts = self._table_cuts()
        if any(abs(cut.seconds - seconds) < 0.1 for cut in cuts):
            return
        cuts.append(Cut(seconds, "手动", 1.0))
        self._fill_cuts(sorted(cuts, key=lambda cut: cut.seconds))
        self.status.setText("切点已修改；点击重新生成才会更新大图。")

    def _remove_cut(self):
        if self.is_busy():
            return
        for row in sorted({index.row() for index in self.cuts_table.selectedIndexes()}, reverse=True):
            self.cuts_table.removeRow(row)
        if self._result is not None:
            self.status.setText("切点已修改；点击重新生成才会更新大图。")

    def _show_preview(self, path):
        reader = QtGui.QImageReader(path)
        size = reader.size()
        if size.isValid() and size.width() > 700:
            reader.setScaledSize(QtCore.QSize(700, max(1, round(size.height() * 700 / size.width()))))
        image = reader.read()
        if image.isNull():
            self.preview.setText("大图已保存，但预览读取失败。")
        else:
            self.preview.setPixmap(QtGui.QPixmap.fromImage(image))

    def _open_image(self):
        if self._result and self._result["paths"]:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._result["paths"][0]))

    def _open_folder(self):
        if self._result and self._result["paths"]:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(Path(self._result["paths"][0]).parent)))

    def closeEvent(self, event):
        if self.is_busy():
            self.cancel_work()
            event.ignore()
            return
        super().closeEvent(event)
