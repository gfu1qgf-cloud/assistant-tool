"""Nonblocking status and manual refresh for daily quantity statistics."""

from qt_compat import QtCore, QtWidgets

from model.DailyQuantityStats import reconcile_daily_quantity


class DailyQuantityThread(QtCore.QThread):
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, config, root, parent=None):
        super().__init__(parent)
        self.config = dict(config)
        self.root = str(root)

    def run(self):
        try:
            self.completed.emit(reconcile_daily_quantity(self.config, self.root))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class DailyQuantityDialog(QtWidgets.QDialog):
    refresh_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("每日数量统计")
        self.resize(740, 470)
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "根据上传历史的首次交付日期与批次计数；刷新会重新读取本地任务表。"
            "只填写本人分类的三个时段数量，不改定额和合计。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QtWidgets.QLabel("尚未刷新")
        layout.addWidget(self.status)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        self.refresh_button = QtWidgets.QPushButton("刷新并自动修正")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        layout.addWidget(self.refresh_button)

    def set_busy(self, busy):
        self.refresh_button.setEnabled(not busy)
        if busy:
            self.status.setText("正在读取上传历史、本地任务表和 Google 表格…")

    def show_result(self, result):
        warnings = result.get("warnings", [])
        updated = result.get("updated", [])
        self.status.setText(
            f"已归类视频 {result.get('counted', 0)} 个；更新数字格 {len(updated)} 个；"
            f"待处理提示 {len(warnings)} 条"
        )
        lines = [f"{item['range']} → {item['count']}" for item in updated]
        if warnings:
            lines += ["", "待处理："] + [f"• {text}" for text in warnings]
        self.details.setPlainText("\n".join(lines) or "数量已是最新，无需写入。")

    def show_error(self, error):
        self.status.setText("刷新失败，原统计数据未改动")
        self.details.setPlainText(error)
