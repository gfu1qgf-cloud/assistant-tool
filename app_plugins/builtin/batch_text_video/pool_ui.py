"""Small reusable pool tables and an explicit pairing preview."""
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets
from .component_ui import Combo
from .pools import pairing_plan
from .custom_pools import pool_choices, get_pool, assign_voices
from .audio_sources import pool_voice
from .audio_ui import AudioSourceCard


class ResourceTable(QtWidgets.QTableWidget):
    removeRequested = QtCore.pyqtSignal()
    moveRequested = QtCore.pyqtSignal(int)
    filesDropped = QtCore.pyqtSignal(object)

    def dragEnterEvent(self, event):
        if self.acceptDrops() and event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        if self.acceptDrops() and event.mimeData().hasUrls():
            self.filesDropped.emit([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
            event.setDropAction(QtCore.Qt.DropAction.CopyAction)
            event.accept()

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
        # Parameter edits do not change pool contents. Keep existing items,
        # selection, scroll position and decoded icons instead of rebuilding.
        snapshot = [(identifier, tuple(map(str, values)), tooltip, icon)
                    for identifier, values, tooltip, icon in rows]
        if getattr(self, '_display_rows', None) == snapshot and self.rowCount() == len(snapshot):
            return
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
        self._display_rows = snapshot


class PairingDialog(QtWidgets.QDialog):
    def __init__(self, copies, videos, selected_copies=(), selected_videos=(), parent=None, *, state=None, selected_pool="", selected_voice_pool=""):
        super().__init__(parent)
        self.setWindowTitle("批量搭配 · 先核对，再加入生成队列")
        self.resize(1050, 720)
        self.copies, self.videos = copies, videos
        if state is None:
            from .store import fresh_state
            state = fresh_state()
        self.pool_state = state
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
        sources = QtWidgets.QFormLayout()
        self.video_source = Combo()
        for identifier, name in pool_choices(self.pool_state, "video", "默认视频池"):
            self.video_source.addItem(name, identifier)
        self.video_source.setCurrentIndex(max(0, self.video_source.findData(selected_pool)))
        sources.addRow("背景视频池", self.video_source)
        layout.addLayout(sources)
        cards = QtWidgets.QHBoxLayout()
        self.voice_card = AudioSourceCard("voice", self.pool_state)
        self.voice_source, self.voice_mode = self.voice_card.pool, self.voice_card.mode
        if selected_voice_pool:
            self.voice_source.setCurrentIndex(max(0, self.voice_source.findData(selected_voice_pool)))
            self.voice_mode.setCurrentIndex(self.voice_mode.findData("rotate"))
        self.music_enabled = QtWidgets.QCheckBox("启用")
        self.music_enabled.setChecked(self.pool_state["settings"].get("music_enabled", True))
        self.music_card = AudioSourceCard("music", self.pool_state, enabled=self.music_enabled)
        cards.addWidget(self.voice_card, 1)
        cards.addWidget(self.music_card, 1)
        layout.addLayout(cards)
        scopes = QtWidgets.QHBoxLayout()
        self.only_copies = QtWidgets.QCheckBox("仅使用选中的文案")
        self.only_videos = QtWidgets.QCheckBox("仅使用选中的视频")
        self.only_copies.setEnabled(bool(selected_copies))
        self.only_videos.setEnabled(bool(selected_videos))
        scopes.addWidget(self.only_copies)
        scopes.addWidget(self.only_videos)
        scopes.addStretch()
        layout.addLayout(scopes)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["文案 / 任务", "背景视频（可更换）", "标题", "任务目录", "人声（可更换）"])
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(self.table.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.voice_menu)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(2, 185)
        self.table.setColumnWidth(3, 155)
        self.table.setColumnWidth(4, 255)
        self.table.verticalHeader().setDefaultSectionSize(32)
        layout.addWidget(self.table, 1)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        note = QtWidgets.QLabel("上方卡片设默认；每行可直接选任意人声池或指定音频。多选行后右键可批量换人声。\n人声从头播放，成品时长跟随人声；音乐保留续播及避让人声。表格任务仍导出到原任务目录。")
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
        self.video_source.currentIndexChanged.connect(self.video_pool_changed)
        self.voice_source.currentIndexChanged.connect(self.rebuild)
        self.voice_mode.currentIndexChanged.connect(self.rebuild)
        self.rebuild()

    def video_pool_changed(self, *_args):
        self.only_videos.setChecked(False)
        self.rebuild()

    def rebuild(self, *_args):
        copies = [item for item in self.copies if not self.only_copies.isChecked() or item["id"] in self.selected_copies]
        identifier = self.video_source.currentData()
        pool_videos = [item["path"] for item in get_pool(self.pool_state, identifier, "video")["items"]] if identifier else self.videos
        videos = [path for path in pool_videos if not self.only_videos.isChecked() or path in self.selected_videos]
        voice_id = self.voice_source.currentData()
        voices = get_pool(self.pool_state, voice_id, "voice")["items"] if voice_id else []
        try:
            self.pairs = assign_voices(pairing_plan(copies, videos, self.mode.currentData()), voices, self.voice_mode.currentData())
            self.status.setText(f"将搭配{len(self.pairs)}个视频；背景和人声下拉框可逐行调整。")
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
            voice_combo = Combo()
            task_path = source.get("voice_path", "")
            voice_combo.addItem("沿用任务人声" + ("：" + Path(task_path).name if task_path else ""), None)
            voice_combo.addItem("不使用人声", "")
            for pool in self.pool_state.get("resource_pools", []):
                if pool["kind"] == "voice":
                    voice_combo.addItem("池：" + pool["name"] + " · 轮换", "pool:" + pool["id"])
                    voice_combo.setItemData(voice_combo.count()-1, f"{len(pool['items'])} 条音频；按当前行顺序轮换。", QtCore.Qt.ItemDataRole.ToolTipRole)
            for item in voices:
                pool_name = get_pool(self.pool_state, voice_id, "voice")["name"]
                voice_combo.addItem(pool_name + " / " + Path(item["path"]).name, item["path"])
                voice_combo.setItemData(voice_combo.count()-1, item["path"], QtCore.Qt.ItemDataRole.ToolTipRole)
            voice_combo.setCurrentIndex(voice_combo.findData(pair["voice_path"]) if "voice_path" in pair else 0)
            voice_combo._accepted_voice = voice_combo.currentData()
            voice_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            voice_combo.setMinimumContentsLength(6)
            voice_combo.currentIndexChanged.connect(lambda _index, c=voice_combo, p=pair, r=row: self.change_row_voice(p, c, r))
            self.table.setCellWidget(row, 4, voice_combo)

    def set_pair_voice(self, pair, path, row=0):
        if isinstance(path, str) and path.startswith("pool:"):
            path = pool_voice(self.pool_state, path[5:], row)
        if path is None:
            pair.pop("voice_path", None)
            pair.pop("voice_override", None)
        else:
            pair.update(voice_path=path, voice_override=True)

    def change_row_voice(self, pair, combo, row):
        before = combo._accepted_voice
        try:
            self.set_pair_voice(pair, combo.currentData(), row)
            combo._accepted_voice = combo.currentData()
            combo.setToolTip((pair["voice_path"] or "不使用人声") if "voice_path" in pair else "沿用任务人声")
            self.status.setText(f"第 {row+1} 行人声已更新；共 {len(self.pairs)} 个搭配。")
        except (ValueError, OSError) as error:
            self.status.setText(str(error))
            blocker = QtCore.QSignalBlocker(combo)
            combo.setCurrentIndex(combo.findData(before))
            del blocker

    def apply_row_voices(self, value, rows=None):
        rows = sorted(set(rows if rows is not None else [index.row() for index in self.table.selectedIndexes()]))
        if not rows:
            return
        # Resolve the entire selection first; an empty/missing pool must not
        # partially change a batch and leave an accepted table lying about it.
        paths = [pool_voice(self.pool_state, value[5:], row) if isinstance(value, str) and value.startswith("pool:") else value for row in rows]
        for row, path in zip(rows, paths):
            self.set_pair_voice(self.pairs[row], path, row)
            combo = self.table.cellWidget(row, 4)
            blocker = QtCore.QSignalBlocker(combo)
            if path is not None and combo.findData(path) < 0:
                combo.addItem(Path(path).name, path)
            combo.setCurrentIndex(combo.findData(path) if path is not None else 0)
            combo._accepted_voice = combo.currentData()
            combo.setToolTip((path or "不使用人声") if path is not None else "沿用任务人声")
            del blocker
        self.status.setText(f"已更新 {len(rows)} 行的人声；任务目录不变。")

    def voice_menu(self, position):
        row = self.table.rowAt(position.y())
        selected = {index.row() for index in self.table.selectedIndexes()}
        if row >= 0 and row not in selected:
            self.table.selectRow(row)
        menu = QtWidgets.QMenu(self)
        pool_menu = menu.addMenu("从人声池轮换")
        for pool in self.pool_state.get("resource_pools", []):
            if pool["kind"] == "voice":
                action = pool_menu.addAction(pool["name"] + f"（{len(pool['items'])} 条）")
                action.setData("pool:" + pool["id"])
        task = menu.addAction("沿用任务人声")
        none = menu.addAction("不使用人声")
        menu.addSeparator()
        all_rows = menu.addAction("选择全部")
        clear = menu.addAction("取消选择")
        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        try:
            if chosen is all_rows:
                self.table.selectAll()
            elif chosen is clear:
                self.table.clearSelection()
            elif chosen is task:
                self.apply_row_voices(None)
            elif chosen is none:
                self.apply_row_voices("")
            elif chosen is not None and chosen.data():
                self.apply_row_voices(chosen.data())
        except (ValueError, OSError) as error:
            self.status.setText(str(error))
