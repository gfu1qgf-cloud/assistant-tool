"""Final outcomes, not a mandatory review gate. All actions run on GUI thread."""
import json
from qt_compat import QtCore, QtGui, QtWidgets


class WorkflowResults(QtWidgets.QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["任务", "状态", "处理结果／提醒"])
        self.setRootIsDecorated(False)
        self.setMinimumHeight(100)
        self.setMaximumHeight(210)
        self.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Interactive)
        self.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(0, 230)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.menu)
        self.itemDoubleClicked.connect(lambda item, _column: self.details(item))
        self.rows = {}
        self.setVisible(False)

    def add(self, key, label, directory):
        self.setVisible(True)
        item = QtWidgets.QTreeWidgetItem([label, "排队", directory])
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, {"task_label": label, "directory": directory})
        self.addTopLevelItem(item)
        self.rows[key] = item
        return item

    def update_result(self, key, result):
        item = self.rows[key]
        record = {**item.data(0, QtCore.Qt.ItemDataRole.UserRole), **result}
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, record)
        status = {"queued": "排队", "running": "运行中", "canceling": "取消中", "canceled": "已取消",
                  "completed": "完成", "completed_with_notes": "完成 · 有提醒", "failed": "失败", "skipped": "已跳过"}.get(record.get("status"), "待执行")
        text = record.get("error") or record.get("stage") or (f"{len(record.get('notes', []))} 条提醒；双击查看详情" if record.get("notes") else record.get("video", ""))
        item.setText(1, status)
        item.setText(2, text)
        item.setToolTip(2, text)

    def details(self, item):
        record = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("工作流结果 · " + record["task_label"])
        dialog.resize(740, 460)
        layout = QtWidgets.QVBoxLayout(dialog)
        text = QtWidgets.QPlainTextEdit(dialog)
        text.setReadOnly(True)
        text.setPlainText(json.dumps(record, ensure_ascii=False, indent=2))
        layout.addWidget(text)
        close = QtWidgets.QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def menu(self, position):
        item = self.itemAt(position)
        if item is None:
            return
        record = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        menu = QtWidgets.QMenu(self)
        for title, key in (("打开成品", "video"), ("打开字幕", "subtitles"), ("打开处理报告", "report"), ("打开任务目录", "directory")):
            path = record.get(key)
            if path:
                menu.addAction(title, lambda _checked=False, p=path: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(p)))
        handle = getattr(item, "handle", None)
        if handle and handle.isRunning():
            menu.addAction("取消此任务", handle.requestInterruption)
        menu.addSeparator()
        menu.addAction("查看完整详情", lambda: self.details(item))
        menu.addAction("复制结果摘要", lambda: QtWidgets.QApplication.clipboard().setText(
            record["task_label"] + "\n" + (record.get("video") or record.get("error") or record.get("stage", ""))))
        menu.exec(self.viewport().mapToGlobal(position))
