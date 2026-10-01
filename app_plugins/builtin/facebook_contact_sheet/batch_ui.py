"""Non-modal batch queue; all widgets are touched on the GUI thread only."""

from qt_compat import QtCore, QtGui, QtWidgets

from .batch import run_batch
from .task_cache import cached_sheets


class _BatchWorker(QtCore.QThread):
    itemChanged = QtCore.pyqtSignal(int, str)
    completed = QtCore.pyqtSignal(object)

    def __init__(self, jobs, settings, parent=None):
        super().__init__(parent)
        self.jobs = list(jobs)
        self.settings = dict(settings)

    def run(self):
        result = run_batch(
            self.jobs, **self.settings,
            progress=lambda index, status: self.itemChanged.emit(index, status),
            canceled=self.isInterruptionRequested,
        )
        self.completed.emit(result)


class BatchContactSheetDialog(QtWidgets.QDialog):
    def __init__(self, logger=None, parent=None):
        super().__init__(parent)
        self._log = logger or (lambda _message: None)
        self._jobs = []
        self._worker = None
        self.setWindowTitle("批量生成 Facebook 参考大图")
        self.resize(850, 560)
        self.setModal(False)
        layout = QtWidgets.QVBoxLayout(self)
        tip = QtWidgets.QLabel("按所选任务顺序逐个处理；已生成的直接跳过。双击已完成项目可打开大图。", self)
        tip.setWordWrap(True)
        layout.addWidget(tip)

        options = QtWidgets.QHBoxLayout()
        self.browser_combo = QtWidgets.QComboBox(self)
        self.browser_combo.addItem("公开链接", "")
        self.browser_combo.addItem("使用 Chrome 登录状态", "chrome")
        self.browser_combo.addItem("使用 Edge 登录状态", "edge")
        self.browser_combo.setToolTip("仅登录后才能观看的视频需要读取浏览器登录状态。")
        options.addWidget(self.browser_combo)
        self.profile_edit = QtWidgets.QLineEdit(self)
        self.profile_edit.setPlaceholderText("浏览器配置名（可选）")
        options.addWidget(self.profile_edit)
        self.sensitivity = QtWidgets.QSpinBox(self)
        self.sensitivity.setRange(1, 10)
        self.sensitivity.setValue(5)
        options.addWidget(QtWidgets.QLabel("灵敏度", self))
        options.addWidget(self.sensitivity)
        self.min_scene = QtWidgets.QDoubleSpinBox(self)
        self.min_scene.setRange(0.2, 10.0)
        self.min_scene.setSingleStep(0.2)
        self.min_scene.setValue(0.8)
        self.min_scene.setSuffix(" 秒")
        options.addWidget(QtWidgets.QLabel("最短镜头", self))
        options.addWidget(self.min_scene)
        self.max_interval = QtWidgets.QSpinBox(self)
        self.max_interval.setRange(0, 120)
        self.max_interval.setValue(15)
        self.max_interval.setSpecialValueText("关闭")
        self.max_interval.setSuffix(" 秒")
        options.addWidget(QtWidgets.QLabel("补帧间隔", self))
        options.addWidget(self.max_interval)
        layout.addLayout(options)

        self.table = QtWidgets.QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["任务", "Facebook 链接", "状态"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._open_cached)
        layout.addWidget(self.table, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.summary = QtWidgets.QLabel("请选择任务。", self)
        bottom.addWidget(self.summary, 1)
        self.start_button = QtWidgets.QPushButton("开始批量生成", self)
        self.start_button.clicked.connect(self._start)
        bottom.addWidget(self.start_button)
        self.cancel_button = QtWidgets.QPushButton("取消", self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_work)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)

    def is_busy(self):
        return self._worker is not None and self._worker.isRunning()

    def set_jobs(self, jobs):
        if self.is_busy():
            return False
        self._jobs = list(jobs)
        self.table.setRowCount(len(self._jobs))
        cached_count = 0
        for index, job in enumerate(self._jobs):
            cached = bool(cached_sheets(job.folder, job.url))
            cached_count += cached
            for column, value in enumerate((job.label, job.url, "已缓存" if cached else "待生成")):
                item = QtWidgets.QTableWidgetItem(value)
                if column == 1:
                    item.setToolTip(job.url)
                self.table.setItem(index, column, item)
        self.summary.setText(f"共 {len(self._jobs)} 个参考视频，已有缓存 {cached_count} 个。")
        self.start_button.setEnabled(bool(self._jobs))
        return True

    def _start(self):
        if self.is_busy() or not self._jobs:
            return
        settings = {
            "browser": self.browser_combo.currentData() or "",
            "profile": self.profile_edit.text().strip(),
            "sensitivity": self.sensitivity.value(),
            "min_scene_seconds": self.min_scene.value(),
            "max_interval": self.max_interval.value(),
        }
        self._worker = _BatchWorker(self._jobs, settings, self)
        self._worker.itemChanged.connect(self._on_item)
        self._worker.completed.connect(self._on_completed)
        self._worker.finished.connect(self._on_finished)
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.summary.setText(f"正在处理 0 / {len(self._jobs)}…")
        self._worker.start()

    def _on_item(self, index, status):
        self.table.item(index, 2).setText(status)
        if status.startswith(("完成", "已缓存", "失败")):
            self.summary.setText(f"已处理 {index + 1} / {len(self._jobs)}")

    def _on_completed(self, outcomes):
        done = sum(status.startswith("完成") for status in outcomes)
        cached = sum(status.startswith("已缓存") for status in outcomes)
        failed = sum(status.startswith("失败") for status in outcomes)
        canceled = len(outcomes) < len(self._jobs)
        self.summary.setText(
            f"{'已取消；' if canceled else '完成；'}新生成 {done} 个，跳过缓存 {cached} 个，失败 {failed} 个。"
        )
        self._log(f"Facebook 参考大图批量生成：新生成 {done}，缓存 {cached}，失败 {failed}。")

    def _on_finished(self):
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _open_cached(self, row, _column):
        if row >= len(self._jobs):
            return
        job = self._jobs[row]
        sheets = cached_sheets(job.folder, job.url)
        if not sheets:
            QtWidgets.QMessageBox.information(self, "尚未生成", "这条参考大图尚未生成成功。")
            return
        local = sheets[0] if len(sheets) == 1 else job.folder
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(local)))

    def cancel_work(self):
        if self.is_busy():
            self._worker.requestInterruption()
            self.summary.setText("正在取消；当前步骤结束后停止…")

    def closeEvent(self, event):
        if self.is_busy():
            self.cancel_work()
            event.ignore()
            return
        super().closeEvent(event)
