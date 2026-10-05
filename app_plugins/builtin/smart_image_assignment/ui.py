import logging
from pathlib import Path
import tempfile

from qt_compat import QtCore, QtGui, QtWidgets
from PYUI.utility_managers_pyui import MaterialGroupAssignmentDialog
from .engine import Canceled, STATUS_LABELS, analyze, path_key, task_records
from .settings import CONFIG_KEY, normalize_settings


class GroupPicker(MaterialGroupAssignmentDialog):
    """Reuse entry selection, without the old sequential-allocation decision."""
    def __init__(self, groups, targets, parent=None):
        super().__init__(groups, targets, parent)
        self.worker = None
        self._close_pending = False
        self.setWindowTitle("分配图片智能版 · 选择素材条目")
        self.intro_label.setText(f"已选 {len(targets)} 个任务。勾选素材／人物素材条目，AI 将从中挑图。")
        self.intro_label.setToolTip("只发送候选图片缩略图和任务文案到 Gemini；管理员信息与本地路径不发送。确认预览后才移动图片。")
        self.assign_button.setText("分析并预览…")
        self.assign_button.setToolTip("这一步只分析，不移动文件；下一步可换图、跳过或取消。")
        self.move_up_btn.hide()
        self.move_down_btn.hide()
        for label in self.findChildren(QtWidgets.QLabel):
            if label.text() == "取图顺序：从上到下":
                label.setText("按文案智能匹配，不按素材顺序分配")

    def update_distribution_label(self, _item=None):
        count = len(self.selected_images())
        self.distribution_label.setText(f"已选 {len(self.selected_groups())} 个素材条目，共 {count} 张候选图片。")
        self.distribution_label.setToolTip("每任务最多分配一张；无合适图片的任务留空，不以数量够用作为匹配通过。")
        self.assign_button.setEnabled(bool(count))

    def load_groups(self, store):
        self.distribution_label.setText("正在后台读取素材／人物素材条目…")
        self.worker = WorkThread(lambda _progress, _canceled: store.list_image_groups(), self)
        self.worker.completed.connect(self.on_groups)
        self.worker.failed.connect(self.on_load_failed)
        self.worker.finished.connect(self.on_load_finished)
        self.worker.start()

    @QtCore.pyqtSlot(object)
    def on_groups(self, groups):
        self.groups = groups
        self.populate()
        self.assign_button.setEnabled(False)

    @QtCore.pyqtSlot(str)
    def on_load_failed(self, message):
        self.distribution_label.setText("读取素材失败：" + message)

    @QtCore.pyqtSlot()
    def on_load_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        if self._close_pending:
            self.reject()
        else:
            self.update_distribution_label()

    def _accept(self):
        if not self.is_busy():
            super()._accept()

    def is_busy(self):
        return self.worker is not None

    def reject(self):
        if self.is_busy():
            self._close_pending = True
            self.distribution_label.setText("正在结束素材扫描，请稍候…")
            return
        super().reject()

    def closeEvent(self, event):
        if self.is_busy():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)


class WorkThread(QtCore.QThread):
    progress = QtCore.pyqtSignal(str)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)
    canceled = QtCore.pyqtSignal()

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            result = self.operation(self.progress.emit, self.isInterruptionRequested)
        except Canceled:
            self.canceled.emit()
        except Exception as error:
            logging.getLogger("assistant_tool").exception("智能图片分配后台任务失败")
            self.failed.emit(f"{type(error).__name__}: {error}")
        else:
            self.completed.emit(result)


class FittedPreview(QtWidgets.QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.original = QtGui.QPixmap()
        self.setMinimumSize(180, 180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setStyleSheet("background:#20252d;color:#ffffff;border-radius:5px;")

    def show_image(self, path):
        self.original = QtGui.QPixmap(str(path)) if path else QtGui.QPixmap()
        self.fit()

    def fit(self):
        if self.original.isNull():
            self.setPixmap(QtGui.QPixmap())
            self.setText("选择任务查看图片；未生成预览时可打开原图。")
        else:
            self.setPixmap(self.original.scaled(self.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()


class SmartAssignmentDialog(QtWidgets.QDialog):
    def __init__(self, groups, targets, store, context, parent=None):
        super().__init__(parent)
        self.groups, self.tasks, self.store, self.context = list(groups), task_records(targets), store, context
        config = context.load_config()
        self.settings = normalize_settings(config.get(CONFIG_KEY))
        self.search_settings = config.get("smart_image_search")
        self.worker = None
        self.analysis_result = self.move_result = None
        self._close_when_finished = False
        self._temporary = tempfile.TemporaryDirectory(prefix="smart-image-assignment-")
        self.setWindowTitle("分配图片智能版 · 看图核对后再移动")
        self.setWindowFlag(QtCore.Qt.WindowMinimizeButtonHint, True)
        self.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, True)
        self.resize(1180, 730)
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(f"{self.settings['model']} · 仅处理所选素材，每任务一张；发送候选缩略图与完整文案给 Gemini。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QtWidgets.QLabel("点击“开始分析”，先看推荐，再确认移动。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["移动", "任务", "候选图片（可切换）", "AI 判断"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
        self.table.setColumnWidth(0, 48)
        self.table.setColumnWidth(1, 230)
        self.table.setColumnWidth(2, 230)
        self.table.setColumnWidth(3, 140)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        splitter.addWidget(self.table)
        right = QtWidgets.QWidget()
        detail = QtWidgets.QVBoxLayout(right)
        self.preview = FittedPreview()
        detail.addWidget(self.preview, 1)
        self.reason = QtWidgets.QLabel("AI 评分是适配建议，不是准确率；人工确认优先。")
        self.reason.setWordWrap(True)
        self.reason.setMaximumHeight(90)
        detail.addWidget(self.reason)
        self.document = QtWidgets.QPlainTextEdit()
        self.document.setReadOnly(True)
        self.document.setMaximumHeight(190)
        detail.addWidget(self.document)
        actions = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton("打开原图")
        open_button.clicked.connect(self.open_original)
        other_button = QtWidgets.QPushButton("换其他库存图片…")
        other_button.clicked.connect(self.pick_other)
        actions.addWidget(open_button)
        actions.addWidget(other_button)
        detail.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([760, 400])
        layout.addWidget(splitter, 1)
        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("开始分析／重新分析")
        self.start_button.clicked.connect(self.start_analysis)
        self.cancel_button = QtWidgets.QPushButton("取消分析")
        self.cancel_button.clicked.connect(self.cancel_work)
        self.cancel_button.setEnabled(False)
        self.move_button = QtWidgets.QPushButton("确认移动勾选的图片")
        self.move_button.setEnabled(False)
        self.move_button.clicked.connect(self.start_move)
        close = QtWidgets.QPushButton("关闭")
        close.clicked.connect(self.reject)
        for button in (self.start_button, self.cancel_button, self.move_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.table.currentCellChanged.connect(lambda *_args: self.show_detail())
        self.table.itemChanged.connect(lambda _item: self.update_counts())
        self.table.cellDoubleClicked.connect(lambda *_args: self.open_original())

    def is_busy(self):
        return self.worker is not None

    def _launch(self, operation, completion, *, moving=False):
        self.worker = WorkThread(operation, self)
        self.worker.progress.connect(self.on_progress)
        self.worker.completed.connect(completion)
        self.worker.failed.connect(self.on_failed)
        self.worker.canceled.connect(self.on_canceled)
        self.worker.finished.connect(self.on_finished)
        self.start_button.setEnabled(False)
        self.move_button.setEnabled(False)
        self.cancel_button.setEnabled(not moving)
        self.table.setEnabled(False)
        self.worker.start()

    def start_analysis(self):
        if self.is_busy():
            return
        self._launch(lambda progress, canceled: analyze(
            self.groups, self.tasks, self.settings, self.search_settings, self.context.gemini_keys,
            self._temporary.name, progress=progress, canceled=canceled), self.on_analysis)

    @QtCore.pyqtSlot(str)
    def on_progress(self, message):
        self.status.setText(message)
        self.context.log("智能图片分配：" + message)

    @QtCore.pyqtSlot(str)
    def on_failed(self, message):
        self.on_progress("失败：" + self.context.gemini_keys.redact(message))

    @QtCore.pyqtSlot()
    def on_canceled(self):
        self.on_progress("已取消；没有移动图片。")

    @QtCore.pyqtSlot(object)
    def on_analysis(self, result):
        self.analysis_result = result
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            image_id = result["choices"].get(task["id"])
            check = QtWidgets.QTableWidgetItem()
            check.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsUserCheckable)
            check.setCheckState(QtCore.Qt.Checked if image_id else QtCore.Qt.Unchecked)
            label = QtWidgets.QTableWidgetItem(task["label"])
            label.setToolTip(task["text"] or task["speech_text"] or "任务文案为空，不自动分配")
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, label)
            combo = QtWidgets.QComboBox()
            combo.setMinimumWidth(0)
            combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
            combo.addItem("不分配", None)
            for candidate_id in result["candidates"].get(task["id"], []):
                image = result["images"][candidate_id]
                combo.addItem(f"{image['group_name']} · {Path(image['path']).name}", candidate_id)
                combo.setItemData(combo.count() - 1, image["path"], QtCore.Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(max(0, combo.findData(image_id)))
            combo.currentIndexChanged.connect(lambda _index, row=row: self.changed_choice(row))
            self.table.setCellWidget(row, 2, combo)
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem())
            self.update_judgment(row)
            self.table.setRowHeight(row, 42)
        self.table.blockSignals(False)
        if self.tasks:
            self.table.selectRow(0)
        self.on_progress(f"分析完成，复用 {result['cached_count']} 个匹配。" + (" 有接口异常，未完成项未勾选。" if result["errors"] else ""))
        self.status.setToolTip("\n".join(result["errors"]))
        self.show_detail()

    def judgment(self, row):
        task_id = self.tasks[row]["id"]
        image_id = self.table.cellWidget(row, 2).currentData()
        return self.analysis_result["matches"].get((task_id, image_id), {
            "status": "manual" if image_id else "uncertain", "score": None,
            "reason": "此图未经 AI 确认；如果你认为合适，可手动勾选。" if image_id else "没有自动分配；可选候选图人工核对，或暂时跳过。"})

    def update_judgment(self, row):
        value = self.judgment(row)
        score = "" if value["score"] is None else f" · {value['score']} 分"
        item = self.table.item(row, 3)
        item.setText(STATUS_LABELS[value["status"]] + score)
        item.setToolTip(value["reason"])
        color = {"suitable": "#d6efdc", "uncertain": "#ffedbf", "unsuitable": "#ffd8df", "manual": "#e2e8f0"}[value["status"]]
        item.setBackground(QtGui.QColor(color))
        item.setForeground(QtGui.QColor("#202124"))

    def changed_choice(self, row):
        self.table.item(row, 0).setCheckState(QtCore.Qt.Unchecked)
        self.update_judgment(row)
        self.table.selectRow(row)
        self.show_detail()
        self.update_counts()

    def assignments(self):
        assignments, used = [], set()
        if self.analysis_result is None:
            return assignments
        for row, task in enumerate(self.tasks):
            if self.table.item(row, 0).checkState() != QtCore.Qt.Checked:
                continue
            image_id = self.table.cellWidget(row, 2).currentData()
            if image_id not in self.analysis_result["images"]:
                raise ValueError(f"任务“{task['label']}”勾选了移动，但没有选择图片。")
            image = self.analysis_result["images"][image_id]
            key = path_key(image["path"])
            if key in used:
                raise ValueError("同一张图片选给了多个任务，请换图或取消其中一个任务的勾选。")
            if not Path(image["path"]).is_file():
                raise ValueError("选中的图片已被移动或删除，请重新分析。")
            stat = Path(image["path"]).stat()
            if image.get("size") != stat.st_size or image.get("mtime_ns") != stat.st_mtime_ns:
                raise ValueError("图片在分析后已更新，请重新分析或重新人工选择，不移动旧预览对应的新文件。")
            used.add(key)
            assignments.append({"path": image["path"], "target_dir": task["target_dir"], "task_label": task["label"],
                                "expected_size": stat.st_size, "expected_mtime_ns": stat.st_mtime_ns})
        return assignments

    def update_counts(self):
        if self.is_busy():
            return
        try:
            count = len(self.assignments())
        except ValueError as error:
            self.move_button.setEnabled(False)
            self.move_button.setToolTip(str(error))
        else:
            self.move_button.setEnabled(bool(count))
            self.move_button.setText(f"确认移动 {count} 张图片")
            self.move_button.setToolTip("仅移动勾选的任务图片；未勾选的不动。")

    def show_detail(self):
        row = self.table.currentRow()
        if self.analysis_result is None or row < 0:
            return
        task = self.tasks[row]
        image_id = self.table.cellWidget(row, 2).currentData()
        image = self.analysis_result["images"].get(image_id, {})
        self.preview.show_image(image.get("preview", ""))
        reason = self.judgment(row)["reason"]
        self.reason.setText(reason)
        self.reason.setToolTip(reason)
        self.document.setPlainText(task["text"] + ("\n\n任务语音：\n" + task["speech_text"]
                                                   if task["speech_text"] and task["speech_text"] != task["text"] else ""))

    def open_original(self):
        row = self.table.currentRow()
        if self.analysis_result is None or row < 0:
            return
        image = self.analysis_result["images"].get(self.table.cellWidget(row, 2).currentData())
        if image and Path(image["path"]).is_file():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(Path(image["path"]).resolve())))

    def pick_other(self):
        row = self.table.currentRow()
        if self.is_busy() or self.analysis_result is None or row < 0:
            return
        images = self.analysis_result["images"]
        first = next(iter(images.values()), {})
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(self, "从本次所选素材条目中换图", str(Path(first.get("path", "")).parent), "图片 (*.jpg *.jpeg *.png *.webp *.bmp *.jfif *.tif *.tiff)")
        if not path:
            return
        image_id = next((key for key, value in images.items() if path_key(value["path"]) == path_key(path)), None)
        if image_id is None:
            QtWidgets.QMessageBox.warning(self, "图片不在所选素材中", "只能从本次勾选的素材／人物素材条目中换图；其他图片请先入库。")
            return
        stat = Path(path).stat()
        if (images[image_id].get("size"), images[image_id].get("mtime_ns")) != (stat.st_size, stat.st_mtime_ns):
            images[image_id]["preview"] = ""
        images[image_id].update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        # A manual override is not an AI approval, even if this ID was a prior candidate.
        self.analysis_result["matches"].pop((self.tasks[row]["id"], image_id), None)
        combo = self.table.cellWidget(row, 2)
        index = combo.findData(image_id)
        if index < 0:
            combo.addItem(Path(path).name + "（人工选择）", image_id)
            index = combo.count() - 1
        if combo.currentIndex() == index:
            self.changed_choice(row)
        else:
            combo.setCurrentIndex(index)

    def start_move(self):
        if self.is_busy():
            return
        try:
            assignments = self.assignments()
            if not assignments:
                return
        except ValueError as error:
            QtWidgets.QMessageBox.warning(self, "请检查分配", str(error))
            return
        self.on_progress(f"正在移动 {len(assignments)} 张图片；此阶段不强制中断，失败会尝试回滚。")
        self._launch(lambda _progress, _canceled: self.store.move_material_images(assignments), self.on_moved, moving=True)

    @QtCore.pyqtSlot(object)
    def on_moved(self, result):
        self.move_result = result

    @QtCore.pyqtSlot()
    def on_finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.table.setEnabled(True)
        self.update_counts()
        if self.move_result is not None:
            self.accept()
        elif self._close_when_finished:
            self.reject()

    def cancel_work(self):
        if self.worker is not None and self.cancel_button.isEnabled():
            self.worker.requestInterruption()
            self.status.setText("正在取消，等待当前模型／网络请求结束；没有移动图片。")

    def reject(self):
        if self.is_busy():
            self._close_when_finished = True
            self.cancel_work()
            return
        super().reject()

    def closeEvent(self, event):
        if self.is_busy():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)

    def cleanup(self):
        if not self.is_busy():
            self._temporary.cleanup()
