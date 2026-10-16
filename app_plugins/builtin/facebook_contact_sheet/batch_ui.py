"""Non-modal batch queue; all widgets are touched on the GUI thread only."""

from qt_compat import QtCore, QtGui, QtWidgets

from .batch import run_batch, run_reference_job
from .task_cache import cached_sheets
from model.BackgroundTasks import BackgroundTaskManager, directory_resource, ACTIVE


class BatchContactSheetDialog(QtWidgets.QDialog):
    def __init__(self, logger=None, parent=None, task_manager=None):
        super().__init__(parent)
        self._log = logger or (lambda _message: None)
        self._jobs = []
        self._manager = task_manager or BackgroundTaskManager(
            parent=QtWidgets.QApplication.instance(), logger=lambda text, _level: self._log(text))
        self._handles = {}
        self._was_busy = False
        self._active_settings = None
        self._manager.changed.connect(self._on_changed)
        self.setWindowTitle("批量生成 Facebook 参考大图")
        self.resize(850, 560)
        self.setModal(False)
        layout = QtWidgets.QVBoxLayout(self)
        tip = QtWidgets.QLabel("提交到主界面后台任务；可同时处理 2 个参考视频。关闭窗口后继续生成；已缓存的跳过下载。", self)
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
        return any(handle.isRunning() for handle in self._handles.values())

    def set_jobs(self, jobs):
        busy = self.is_busy()
        if busy:
            keys = {(str(job.folder), job.url) for job in self._jobs}
            start = len(self._jobs)
            self._jobs.extend(job for job in jobs if (str(job.folder), job.url) not in keys)
        else:
            self._jobs = list(jobs)
            self._handles = {}
            start = 0
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
        if busy:
            self._submit_rows(range(start, len(self._jobs)), self._active_settings)
        self.start_button.setEnabled(bool(self._jobs) and not self.is_busy())
        self._refresh_status()
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
        self._active_settings = settings
        self._submit_rows(range(len(self._jobs)), settings)

    def _submit_rows(self, rows, settings):
        for index in rows:
            job = self._jobs[index]
            try:
                self._handles[index] = self._manager.submit(
                    owner="facebook_contact_sheet", title="FB 参考大图 · " + job.label,
                    runner=run_reference_job, payload={"job": job, "settings": settings}, kind="media",
                    resources=[directory_resource(job.folder)], dedup_key="batch:" + directory_resource(job.folder),
                    retryable=True,
                )
            except (RuntimeError, ValueError, TypeError) as error:
                self._log(f"无法提交参考大图任务：{error}")
                QtWidgets.QMessageBox.information(self, "无法提交任务", str(error))
                break
        self._was_busy = self.is_busy()
        self._refresh_status()

    @QtCore.pyqtSlot(str)
    def _on_changed(self, _task_id):
        self._refresh_status()

    def _refresh_status(self):
        done = failed = canceled = active = 0
        for index, handle in self._handles.items():
            record = self._manager.get(handle.task_id)
            status = record["status"]
            done += status == "succeeded"
            failed += status == "failed"
            canceled += status == "canceled"
            active += status in ACTIVE
            item = self.table.item(index, 2)
            if item:
                item.setText(record["stage"])
                item.setToolTip(record["error"] or record["stage"])
        self.start_button.setEnabled(bool(self._jobs) and not active)
        self.cancel_button.setEnabled(bool(active))
        if self._handles:
            self.summary.setText(f"共 {len(self._jobs)} 个 · 运行/等待 {active} · 完成 {done} · 失败 {failed} · 取消 {canceled}")
        if self._was_busy and not active:
            self._was_busy = False
            self._log("Facebook 参考大图批量处理：" + self.summary.text())

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
            self._manager.cancel_many([handle.task_id for handle in self._handles.values() if handle.isRunning()])
            self.summary.setText("正在取消；当前步骤结束后停止…")

    def closeEvent(self, event):
        super().closeEvent(event)
