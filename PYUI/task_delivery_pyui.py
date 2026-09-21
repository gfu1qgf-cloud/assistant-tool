from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets
from qt_compat import QMessageBox


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
            ("逐个手动审核", "逐个显示抽帧图，由你人工判断", "manual"),
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
