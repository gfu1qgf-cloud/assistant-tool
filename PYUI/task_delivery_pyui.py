from pathlib import Path
import logging
import traceback

from qt_compat import QtCore, QtGui, QtWidgets
from qt_compat import QMessageBox


class _ManualDetailWorker(QtCore.QThread):
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path)

    def run(self):
        try:
            from model.VideoElementDetector import manual_review_video
            self.completed.emit(manual_review_video(self.path))
        except Exception:
            logging.getLogger(__name__).exception("打开人工审核细节失败")
            self.failed.emit(traceback.format_exc())


class _ReviewDecisionCombo(QtWidgets.QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class TaskReviewListDialog(QtWidgets.QDialog):
    """Review all video routes together; contact-sheet inspection is optional."""

    DECISIONS = (("需要审核", "review"), ("不需要审核", "normal_upload"),
                 ("有问题不上传", "skip_upload"))

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.entries = [dict(entry) for entry in entries]
        self._detail_results = {}
        self._detail_worker = None
        self._detail_row = -1
        self.setWindowTitle("视频审核清单")
        self.resize(1050, 620)
        layout = QtWidgets.QVBoxLayout(self)
        tip = QtWidgets.QLabel(
            "直接在下拉框标记；右键“查看审核细节”打开原来的抽帧审核界面。\n"
            "确认后才开始上传。关闭或取消会保留待处理文件，尚未确认的修改不会写入审核记录。", self,
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self.table = QtWidgets.QTableWidget(len(self.entries), 3, self)
        self.table.setHorizontalHeaderLabels(["视频文件", "处理方式", "检测／审核结果"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 170)
        self.combos = []
        for row, entry in enumerate(self.entries):
            path = Path(entry["file_path"])
            item = QtWidgets.QTableWidgetItem(path.name)
            item.setToolTip(str(path))
            self.table.setItem(row, 0, item)
            summary = QtWidgets.QTableWidgetItem(str(entry.get("summary") or ""))
            summary.setToolTip(summary.text())
            self.table.setItem(row, 2, summary)
            combo = _ReviewDecisionCombo(self.table)
            for title, decision in self.DECISIONS:
                combo.addItem(title, decision)
            combo.setCurrentIndex(max(0, combo.findData(entry["decision"])))
            combo.currentIndexChanged.connect(self._update_summary)
            self.table.setCellWidget(row, 1, combo)
            self.combos.append(combo)
        layout.addWidget(self.table, 1)
        self.summary = QtWidgets.QLabel(self)
        layout.addWidget(self.summary)
        buttons = QtWidgets.QHBoxLayout()
        self.detail_button = QtWidgets.QPushButton("查看审核细节", self)
        self.detail_button.clicked.connect(self._open_detail)
        buttons.addWidget(self.detail_button)
        buttons.addStretch(1)
        self.cancel_button = QtWidgets.QPushButton("取消，稍后继续", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.confirm_button = QtWidgets.QPushButton("确认清单并上传", self)
        self.confirm_button.clicked.connect(self.accept)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)
        self._update_summary()

    def _update_summary(self, *_args):
        counts = {decision: 0 for _title, decision in self.DECISIONS}
        colors = {"review": "#FFF3CD", "normal_upload": "#DDF3E4", "skip_upload": "#F8D7DA"}
        for row, combo in enumerate(self.combos):
            decision = combo.currentData()
            counts[decision] += 1
            self.table.item(row, 0).setBackground(QtGui.QColor(colors[decision]))
            self.table.item(row, 0).setForeground(QtGui.QColor("#263238"))
        self.summary.setText(
            f"共 {len(self.entries)} 个视频：需要审核 {counts['review']} 个 · "
            f"不需要审核 {counts['normal_upload']} 个 · 不上传 {counts['skip_upload']} 个"
        )

    def decisions(self):
        return {
            entry["file_path"]: {"decision": combo.currentData(), "result": self._detail_results.get(row)}
            for row, (entry, combo) in enumerate(zip(self.entries, self.combos))
        }

    def _context_menu(self, position):
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        if not self.table.selectionModel().isRowSelected(index.row(), QtCore.QModelIndex()):
            self.table.selectRow(index.row())
        menu = QtWidgets.QMenu(self)
        detail = menu.addAction("查看审核细节")
        detail.setEnabled(not (self._detail_worker and self._detail_worker.isRunning()))
        open_video = menu.addAction("打开原视频")
        menu.addSeparator()
        actions = {menu.addAction(f"选中项：{title}"): decision for title, decision in self.DECISIONS}
        selected = menu.exec(self.table.viewport().mapToGlobal(position))
        if selected == detail:
            self._open_detail(index.row())
        elif selected == open_video:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.entries[index.row()]["file_path"]))
        elif selected in actions:
            for row_index in self.table.selectionModel().selectedRows():
                combo = self.combos[row_index.row()]
                combo.setCurrentIndex(combo.findData(actions[selected]))

    def _open_detail(self, row=None):
        if self._detail_worker and self._detail_worker.isRunning():
            return
        if row is None or isinstance(row, bool):
            row = self.table.currentRow()
        if row < 0:
            return
        self._detail_row = row
        self._detail_worker = _ManualDetailWorker(self.entries[row]["file_path"], self)
        self._detail_worker.completed.connect(self._on_detail_completed)
        self._detail_worker.failed.connect(self._on_detail_failed)
        self._detail_worker.finished.connect(self._on_detail_finished)
        for button in (self.confirm_button, self.cancel_button, self.detail_button):
            button.setEnabled(False)
        self._detail_worker.start()

    def _on_detail_completed(self, result):
        row = self._detail_row
        self._detail_results[row] = result
        decision = "skip_upload" if result.get("skip_upload") else "review" if result.get("found") else "normal_upload"
        self.combos[row].setCurrentIndex(self.combos[row].findData(decision))
        self.table.item(row, 2).setText(str(result.get("summary") or "人工审核完成"))
        self.table.item(row, 2).setToolTip(self.table.item(row, 2).text())

    def _on_detail_finished(self):
        for button in (self.confirm_button, self.cancel_button, self.detail_button):
            button.setEnabled(True)

    def _on_detail_failed(self, error):
        QMessageBox.warning(self, "审核细节失败", error)

    def reject(self):
        if self._detail_worker and self._detail_worker.isRunning():
            return
        super().reject()

    def accept(self):
        if self._detail_worker and self._detail_worker.isRunning():
            return
        super().accept()

    def closeEvent(self, event):
        if self._detail_worker and self._detail_worker.isRunning():
            event.ignore()
            return
        super().closeEvent(event)


class UpdatedFilesDetectionDialog(QtWidgets.QDialog):
    """Preview changed output files before choosing the review workflow."""

    video_suffixes = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

    def __init__(self, updated_files, parent=None):
        super().__init__(parent)
        self.updated_files = [Path(path) for path in updated_files]
        self.selected_mode = "cancel"
        self.setWindowTitle("选择视频审核方式")
        self.setModal(True)
        self.resize(900, 520)

        layout = QtWidgets.QVBoxLayout(self)
        video_count = sum(
            path.suffix.lower() in self.video_suffixes
            for path in self.updated_files
        )
        summary_label = QtWidgets.QLabel(
            f"本次实际新增/更新 {len(self.updated_files)} 个文件，"
            f"其中视频 {video_count} 个。\n"
            "双击文件可先用系统默认程序打开，确认后再选择本轮处理方式。"
        )
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        self.file_tree = QtWidgets.QTreeWidget(self)
        self.file_tree.setColumnCount(3)
        self.file_tree.setHeaderLabels(["文件名", "大小", "所在目录"])
        self.file_tree.setRootIsDecorated(False)
        self.file_tree.setAlternatingRowColors(True)
        self.file_tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        self.file_tree.setToolTip("双击文件可用系统默认程序打开")
        self.file_tree.itemDoubleClicked.connect(self.open_file_item)
        layout.addWidget(self.file_tree, 1)

        for file_path in self.updated_files:
            item = QtWidgets.QTreeWidgetItem(
                [file_path.name, self.format_file_size(file_path), str(file_path.parent)]
            )
            item.setData(0, QtCore.Qt.UserRole, str(file_path))
            item.setToolTip(0, str(file_path))
            item.setToolTip(2, str(file_path.parent))
            self.file_tree.addTopLevelItem(item)

        header = self.file_tree.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.file_tree.setColumnWidth(2, 330)
        if self.file_tree.topLevelItemCount():
            self.file_tree.setCurrentItem(self.file_tree.topLevelItem(0))

        button_layout = QtWidgets.QHBoxLayout()
        open_button = QtWidgets.QPushButton("打开选中文件", self)
        open_button.clicked.connect(self.open_selected_file)
        button_layout.addWidget(open_button)
        button_layout.addStretch(1)

        cancel_button = QtWidgets.QPushButton("取消本次操作", self)
        cancel_button.setToolTip("停止本次整理和上传，并保留文件列表供下次继续")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)

        for title, tooltip, mode in (
            ("人工清单审核", "在列表中快速标记，需要时再打开抽帧审核细节", "manual"),
            ("无需检测", "不做检测，本轮视频全部按正常文件处理", "skip"),
            ("使用 AI 检测", "使用 AI 检测视频元素并自动分流", "ai"),
        ):
            button = QtWidgets.QPushButton(title, self)
            button.setToolTip(tooltip)
            button.clicked.connect(
                lambda _checked=False, selected=mode: self.choose_mode(selected)
            )
            if mode == "ai":
                button.setDefault(True)
            button_layout.addWidget(button)
        layout.addLayout(button_layout)

    @staticmethod
    def format_file_size(file_path):
        try:
            size = file_path.stat().st_size
        except OSError:
            return "无法读取"
        units = ("B", "KB", "MB", "GB", "TB")
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return str(size)

    def choose_mode(self, mode):
        self.selected_mode = mode
        self.accept()

    def open_selected_file(self):
        item = self.file_tree.currentItem()
        if item is not None:
            self.open_file_item(item)

    def open_file_item(self, item, _column=0):
        file_path = Path(str(item.data(0, QtCore.Qt.UserRole) or ""))
        if not file_path.is_file():
            QMessageBox.warning(self, "打开文件", f"文件不存在：\n{file_path}")
            return
        opened = QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(file_path.resolve()))
        )
        if not opened:
            QMessageBox.warning(
                self,
                "打开文件",
                f"无法调用系统默认程序：\n{file_path}",
            )
