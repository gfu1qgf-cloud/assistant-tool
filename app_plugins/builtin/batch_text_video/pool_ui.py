"""Small reusable pool tables and an explicit pairing preview."""
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets
from .component_ui import Combo
from .pools import pairing_plan


class ResourceTable(QtWidgets.QTableWidget):
    removeRequested = QtCore.pyqtSignal()
    moveRequested = QtCore.pyqtSignal(int)

    def __init__(self, headers, parent=None):
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(headers)
        self.setSelectionBehavior(self.SelectionBehavior.SelectRows)
        self.setSelectionMode(self.SelectionMode.ExtendedSelection)
        self.setEditTriggers(self.EditTrigger.NoEditTriggers)
        self.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)

    def identifiers(self):
        return [self.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                for row in sorted({index.row() for index in self.selectedIndexes()})]

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Delete:
            self.removeRequested.emit()
            event.accept()
        elif event.modifiers() == QtCore.Qt.KeyboardModifier.AltModifier and event.key() in {
                QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_Down}:
            self.moveRequested.emit(-1 if event.key() == QtCore.Qt.Key.Key_Up else 1)
            event.accept()
        else:
            super().keyPressEvent(event)

    def populate(self, rows):
        selected = set(self.identifiers())
        current = self.item(self.currentRow(), 0)
        current = current.data(QtCore.Qt.ItemDataRole.UserRole) if current else None
        blocker = QtCore.QSignalBlocker(self)
        self.setRowCount(len(rows))
        self.clearSelection()
        selection = self.selectionModel()
        current_found = False
        for row, (identifier, values, tooltip, icon) in enumerate(rows):
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setToolTip(tooltip if column == 0 else str(value))
                if column == 0:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, identifier)
                    if icon:
                        item.setIcon(QtGui.QIcon(icon))
                self.setItem(row, column, item)
            if identifier == current:
                current_found = True
                selection.setCurrentIndex(self.model().index(row, 0), QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
            if identifier in selected:
                selection.select(self.model().index(row, 0), QtCore.QItemSelectionModel.SelectionFlag.Select |
                                 QtCore.QItemSelectionModel.SelectionFlag.Rows)
        if not current_found:
            selection.setCurrentIndex(QtCore.QModelIndex(),QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        del blocker


class PairingDialog(QtWidgets.QDialog):
    def __init__(self, copies, videos, selected_copies=(), selected_videos=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("批量搭配 · 先核对，再加入生成队列")
        self.resize(860, 530)
        self.copies, self.videos = copies, videos
        self.selected_copies, self.selected_videos = set(selected_copies), set(selected_videos)
        self.pairs = []
        layout = QtWidgets.QVBoxLayout(self)
        self.mode = Combo()
        for name, key in (("每条文案配一个视频，视频用完循环", "copies"),
                          ("每个视频配一条文案，文案用完循环", "videos"),
                          ("一对一（文案与视频数量须相同）", "one_to_one"),
                          ("所有文案共用第一个视频", "single")):
            self.mode.addItem(name, key)
        if not copies:
            self.mode.setCurrentIndex(1)
        layout.addWidget(self.mode)
        scopes = QtWidgets.QHBoxLayout()
        self.only_copies = QtWidgets.QCheckBox("仅使用选中的文案")
        self.only_videos = QtWidgets.QCheckBox("仅使用选中的视频")
        self.only_copies.setEnabled(bool(selected_copies))
        self.only_videos.setEnabled(bool(selected_videos))
        scopes.addWidget(self.only_copies)
        scopes.addWidget(self.only_videos)
        scopes.addStretch()
        layout.addLayout(scopes)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["文案 / 任务", "背景视频（可更换）", "标题", "任务目录"])
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(2, 215)
        self.table.setColumnWidth(3, 200)
        layout.addWidget(self.table, 1)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        note = QtWidgets.QLabel("普通文本框固定共用；轮换文本框/图片从对应池循环。表格任务导出到原任务目录。\n重复应用同一文案＋视频不会重复建任务；不同背景版本用数字区分。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                                 QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("应用搭配，加入队列")
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.mode.currentIndexChanged.connect(self.rebuild)
        self.only_copies.toggled.connect(self.rebuild)
        self.only_videos.toggled.connect(self.rebuild)
        self.rebuild()

    def rebuild(self, *_args):
        copies = [item for item in self.copies if not self.only_copies.isChecked() or item["id"] in self.selected_copies]
        videos = [path for path in self.videos if not self.only_videos.isChecked() or path in self.selected_videos]
        try:
            self.pairs = pairing_plan(copies, videos, self.mode.currentData())
            self.status.setText(f"将搭配{len(self.pairs)}个视频；背景下拉框可以逐行调整。")
        except ValueError as error:
            self.pairs = []
            self.status.setText(str(error))
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.pairs))
        self.table.setRowCount(len(self.pairs))
        for row, pair in enumerate(self.pairs):
            source = pair["copy"] or {}
            directory = source.get("task_dir", "")
            short_directory = str(Path(*Path(directory).parts[-3:])) if directory else "普通视频输出目录"
            for column, value in ((0, source.get("label") or source.get("name") or "固定 / 轮换组件"),
                                  (2, source.get("title", "")), (3, source.get("task_dir") or "普通视频输出目录")):
                item = QtWidgets.QTableWidgetItem(short_directory if column == 3 else value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
            combo = Combo()
            for path in videos:
                combo.addItem(Path(path).name, path)
                combo.setItemData(combo.count()-1, path, QtCore.Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(combo.findData(pair["path"]))
            combo.currentIndexChanged.connect(lambda _index, c=combo, p=pair: p.update(path=c.currentData()))
            self.table.setCellWidget(row, 1, combo)
