"""Non-visual audio component cards, separate from canvas layers."""
from qt_compat import QtCore, QtWidgets
from .component_ui import Combo
from .custom_pools import pool_choices
from .audio_sources import AUDIO_ROLES, VOICE_MODES


class AudioSourceCard(QtWidgets.QFrame):
    def __init__(self, kind, state, parent=None, *, enabled=None, volume=None, pool=None):
        super().__init__(parent)
        self.kind, self.role = kind, AUDIO_ROLES[kind]
        self.setObjectName("audioSourceCard")
        self.setStyleSheet("QFrame#audioSourceCard { border: 1px solid " + self.role["color"] +
                          "; border-radius: 7px; } QComboBox { min-height: 24px; }")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(4)
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(self.role["title"])
        title.setStyleSheet("font-weight: 600;")
        header.addWidget(title, 1)
        self.enabled = enabled
        if enabled is not None:
            enabled.setText("启用")
            header.addWidget(enabled)
        self.menu_button = QtWidgets.QToolButton()
        self.menu_button.setText("...")
        self.menu_button.setToolTip("音频控件操作")
        self.menu_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QtWidgets.QMenu(self.menu_button)
        self.menu_button.setMenu(self.menu)
        self.menu_button.hide()
        header.addWidget(self.menu_button)
        layout.addLayout(header)
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.pool = pool or Combo()
        self.pool.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.pool.setMinimumContentsLength(8)
        self.pool.setMinimumWidth(0)
        self.pool.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Fixed)
        self.mode = None
        if kind == "voice":
            self.mode = Combo()
            for label, value in VOICE_MODES:
                self.mode.addItem(label, value)
            self.mode.setCurrentIndex(max(0, self.mode.findData(state["settings"].get("voice_pool_mode", "task"))))
            form.addRow("来源方式", self.mode)
            self.mode.currentIndexChanged.connect(self.update_note)
        form.addRow("来源", self.pool)
        self.file_button = QtWidgets.QPushButton("来自文件…")
        self.file_button.setToolTip("固定使用一个文件；不移动源文件。人声整批共用，音乐仍保留续用位置。")
        self.file_button.clicked.connect(self.choose_file)
        form.addRow(self.file_button)
        if volume is not None:
            form.addRow("音量 %" if kind == "voice" else "无人声时音量 %", volume)
        layout.addLayout(form)
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.info.setMinimumWidth(0)
        layout.addWidget(self.info)
        self._state = state
        self.sync_pools(state)
        self.pool.currentIndexChanged.connect(self.update_note)
        self.pool.activated.connect(self.choose_pool)
        if enabled is not None:
            enabled.toggled.connect(self.update_note)

    def sync_pools(self, state):
        self._state = state
        current = state["settings"].get(self.role["pool_key"], "")
        blocker = QtCore.QSignalBlocker(self.pool)
        self.pool.clear()
        for identifier, name in pool_choices(state, self.kind, self.role["default"]):
            self.pool.addItem(name, identifier)
        if current and self.pool.findData(current) < 0:
            self.pool.addItem("原资源池已不存在（请重选）", current)
        self.pool.setCurrentIndex(max(0, self.pool.findData(current)))
        del blocker
        self.update_note()

    def choose_pool(self, *_args):
        if self.kind == "voice" and self.pool.currentData():
            if self.mode.currentData() in {"task", "none"}:
                self.mode.setCurrentIndex(self.mode.findData("rotate"))
            if self.enabled is not None:
                self.enabled.setChecked(True)
        elif self.kind == "music" and self.enabled is not None:
            self.enabled.setChecked(True)

    def choose_file(self):
        from .custom_pools import create_pool, add_pool_paths
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "固定音频来源", "", "音频 (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.opus *.wma)")
        if not path: return
        # A single-file source uses the same ledger as an ordinary pool, keeping
        # music seek/undo/profile isolation and failed-export rollback intact.
        from pathlib import Path
        base = "固定" + ("人声" if self.kind == "voice" else "音乐") + " · " + Path(path).stem[:45]
        name, number = base, 2
        names = {pool["name"] for pool in self._state.get("resource_pools", [])}
        while name in names:
            name, number = f"{base} ({number})", number + 1
        pool = None
        try:
            pool = create_pool(self._state, name, self.kind)
            if not add_pool_paths(pool, [path]):
                raise ValueError("音频文件不存在或格式不支持，未修改来源。")
        except (ValueError, OSError) as error:
            if pool is not None:
                self._state["resource_pools"].remove(pool)
            self.info.setText(str(error)); return
        self._state["settings"][self.role["pool_key"]] = pool["id"]
        self.sync_pools(self._state)
        self.pool.setCurrentIndex(self.pool.findData(pool["id"]))
        self.choose_pool()
        if self.mode is not None:
            self.mode.setCurrentIndex(self.mode.findData("single"))
        self.pool.activated.emit(self.pool.currentIndex())
        self.pool.currentIndexChanged.emit(self.pool.currentIndex())

    def update_note(self, *_args):
        identifier = self.pool.currentData()
        pool = next((p for p in self._state.get("resource_pools", []) if p["id"] == identifier), None)
        count = len(pool["items"]) if pool else len(self._state.get("music", [])) if self.kind == "music" and not identifier else 0
        if self.kind == "voice" and self.mode.currentData() == "task":
            text = "任务人声 · 选池可直接切换轮换"
        elif self.kind == "voice" and self.mode.currentData() == "none":
            text = "不添加人声 · 保持背景原有长度"
        else:
            text = f"{self.pool.currentText()} · {count} 条"
        if self.enabled is not None and not self.enabled.isChecked():
            text = "当前关闭。" + text
        self.info.setText(text)
        self.info.setToolTip(self.role["note"] + "\n音频控件不占画布，不会出现在成品画面中。")
