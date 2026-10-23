"""Oral duration result UI and its dedicated worker; no main-window dependency."""
import traceback
from qt_compat import QtWidgets, QtCore, QtGui, pyqtSignal, QMessageBox, QHeaderView
from model.OralVideoDurationChecker import check_oral_video_durations


class OralDurationCheckThread(QtCore.QThread):
    log = pyqtSignal(str)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str, str)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = dict(config)

    def run(self):
        try:
            result = check_oral_video_durations(
                self.config,
                progress_callback=self.log.emit,
            )
        except (Exception, SystemExit) as error:
            self.failed.emit(
                f'{type(error).__name__}: {error}',
                traceback.format_exc(),
            )
            return
        self.succeeded.emit(result)


class OralDurationCheckResultDialog(QtWidgets.QDialog):
    def __init__(self, result, parent=None):
        super().__init__(parent)
        self.result = result
        self.setWindowTitle('口播长度检测结果')
        self.resize(920, 560)

        layout = QtWidgets.QVBoxLayout(self)
        summary = QtWidgets.QLabel(
            '符合筛选条件：{candidate_count} 行；成功读取时长：{checked_count} 行；'
            '实际修改：{updated_count} 行；未能检测：{skipped_count} 行。'.format(
                candidate_count=result.get('candidate_count', 0),
                checked_count=result.get('checked_count', 0),
                updated_count=result.get('updated_count', 0),
                skipped_count=len(result.get('skipped_rows', [])),
            ),
            self,
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.tabs = QtWidgets.QTabWidget(self)
        self.updated_tree = QtWidgets.QTreeWidget(self.tabs)
        self.updated_tree.setColumnCount(5)
        self.updated_tree.setHeaderLabels([
            '表格行号', '视频文件', '时长', '原视频类型', '新视频类型'
        ])
        self.updated_tree.setRootIsDecorated(False)
        self.updated_tree.setAlternatingRowColors(True)
        for item in result.get('updated_rows', []):
            row_item = QtWidgets.QTreeWidgetItem([
                str(item.get('row', '')),
                item.get('file_name') or item.get('task_name') or '未命名视频',
                str(item.get('duration', '')),
                str(item.get('old_video_type', '')),
                str(item.get('new_video_type', '')),
            ])
            row_item.setToolTip(1, str(item.get('task_name') or ''))
            self.updated_tree.addTopLevelItem(row_item)
        self.tabs.addTab(
            self.updated_tree,
            f"已修改（{result.get('updated_count', 0)}）",
        )

        self.skipped_tree = QtWidgets.QTreeWidget(self.tabs)
        self.skipped_tree.setColumnCount(3)
        self.skipped_tree.setHeaderLabels(['表格行号', '任务内容', '未检测原因'])
        self.skipped_tree.setRootIsDecorated(False)
        self.skipped_tree.setAlternatingRowColors(True)
        for item in result.get('skipped_rows', []):
            self.skipped_tree.addTopLevelItem(QtWidgets.QTreeWidgetItem([
                str(item.get('row', '')),
                str(item.get('task_name', '')),
                str(item.get('reason', '')),
            ]))
        self.tabs.addTab(
            self.skipped_tree,
            f"未检测（{len(result.get('skipped_rows', []))}）",
        )
        layout.addWidget(self.tabs, 1)

        for tree in (self.updated_tree, self.skipped_tree):
            header = tree.header()
            header.setSectionResizeMode(QHeaderView.ResizeToContents)
            header.setStretchLastSection(True)

        button_layout = QtWidgets.QHBoxLayout()
        open_sheet_button = QtWidgets.QPushButton('打开任务提交表格', self)
        open_sheet_button.clicked.connect(self.openSheet)
        button_layout.addWidget(open_sheet_button)
        button_layout.addStretch(1)
        close_button = QtWidgets.QPushButton('关闭', self)
        close_button.clicked.connect(self.accept)
        close_button.setDefault(True)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)

    def openSheet(self):
        url = str(self.result.get('sheet_url') or '').strip()
        if not url or not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            QMessageBox.warning(self, '打开任务提交表格', '无法打开任务提交表格链接。')
