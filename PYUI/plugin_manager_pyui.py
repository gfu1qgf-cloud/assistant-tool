"""Host-owned plugin startup preferences, independent of plugin business data."""

from copy import deepcopy

from qt_compat import QtCore, QtWidgets
from app_plugins.host import plugin_enabled_states


class PluginManagerDialog(QtWidgets.QDialog):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.setWindowTitle("插件管理")
        self.resize(780, 580)
        self.setMinimumSize(590, 400)
        self._loading = False
        self._snapshot = {}
        self._baseline = {}
        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("选择这台电脑需要的插件", self)
        title.setStyleSheet("font-weight:600;font-size:16px;")
        layout.addWidget(title)
        note = QtWidgets.QLabel(
            "勾选启用，取消勾选禁用；保存后重启生效。禁用不会删除配置或业务数据。\n"
            "发给组员前，可关闭垃圾分类提醒、做饭小助手等个人用途插件。", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        search_row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit(self)
        self.search.setPlaceholderText("搜索插件名称或 ID…")
        self.search.setClearButtonEnabled(True)
        search_row.addWidget(self.search, 1)
        bulk = QtWidgets.QToolButton(self)
        bulk.setText("批量设置")
        bulk.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(bulk)
        self.add_selection_actions(menu)
        bulk.setMenu(menu)
        search_row.addWidget(bulk)
        layout.addLayout(search_row)
        self.table = QtWidgets.QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(("下次启用", "插件", "版本", "本次运行"))
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.itemChanged.connect(self.refresh_status)
        self.search.textChanged.connect(self.filter_rows)
        layout.addWidget(self.table, 1)
        self.status = QtWidgets.QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QtWidgets.QDialogButtonBox(self)
        self.save_button = buttons.addButton("保存启停设置", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("关闭", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def reload(self):
        self._loading = True
        try:
            self._snapshot = deepcopy(self.host.main_window.load_config())
            states = plugin_enabled_states(self._snapshot)
            entries = self.host.plugin_entries()
            self._baseline = {entry["plugin_id"]: states.get(entry["plugin_id"], True)
                              for entry in entries}
            self.table.setRowCount(len(entries))
            for row, entry in enumerate(entries):
                check = QtWidgets.QTableWidgetItem()
                check.setData(QtCore.Qt.ItemDataRole.UserRole, entry["plugin_id"])
                check.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled |
                               QtCore.Qt.ItemFlag.ItemIsSelectable |
                               QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(QtCore.Qt.CheckState.Checked if self._baseline[entry["plugin_id"]]
                                    else QtCore.Qt.CheckState.Unchecked)
                self.table.setItem(row, 0, check)
                name = QtWidgets.QTableWidgetItem(entry["name"])
                name.setToolTip("插件 ID：" + entry["plugin_id"])
                self.table.setItem(row, 1, name)
                self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(entry["version"]))
                status = "启动异常" if entry["error"] else "已启用" if entry["enabled"] else "已禁用"
                item = QtWidgets.QTableWidgetItem(status)
                item.setToolTip(entry["error"] or "这是本次运行状态，不随勾选即时改变。")
                self.table.setItem(row, 3, item)
            self.filter_rows()
        finally:
            self._loading = False
        self.refresh_status()

    def choices(self):
        return {self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole):
                self.table.item(row, 0).checkState() == QtCore.Qt.CheckState.Checked
                for row in range(self.table.rowCount())}

    def refresh_status(self, *_args):
        if self._loading:
            return
        choices = self.choices()
        unsaved = sum(value != self._baseline[key] for key, value in choices.items())
        pending = sum(value != self.host.is_enabled(key) for key, value in choices.items())
        self.status.setText(f"共 {len(choices)} 个插件 · 下次启用 {sum(choices.values())} 个 · "
                            f"未保存 {unsaved} 项 · 重启后改变 {pending} 项")
        self.save_button.setEnabled(bool(unsaved))

    def filter_rows(self, *_args):
        text = self.search.text().strip().casefold()
        for row in range(self.table.rowCount()):
            name = self.table.item(row, 1)
            identifier = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            self.table.setRowHidden(row, text not in (name.text() + " " + identifier).casefold())

    def add_selection_actions(self, menu):
        for title, value in (("启用选中的插件", True), ("禁用选中的插件", False)):
            menu.addAction(title).triggered.connect(lambda _checked=False, state=value: self.set_selected(state))
        menu.addSeparator()
        menu.addAction("选择当前显示的全部插件").triggered.connect(self.select_visible)
        menu.addAction("取消选择").triggered.connect(lambda: self.table.clearSelection())

    def set_selected(self, enabled):
        rows = {index.row() for index in self.table.selectionModel().selectedRows()
                if not self.table.isRowHidden(index.row())}
        for row in rows:
            self.table.item(row, 0).setCheckState(QtCore.Qt.CheckState.Checked if enabled
                                                else QtCore.Qt.CheckState.Unchecked)

    def select_visible(self):
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                self.table.setRangeSelected(QtWidgets.QTableWidgetSelectionRange(row, 0, row, 3), True)

    def context_menu(self, position):
        menu = QtWidgets.QMenu(self)
        self.add_selection_actions(menu)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def save(self):
        choices = self.choices()
        updates = {key: value for key, value in choices.items() if self._baseline[key] != value}
        if not updates:
            return
        try:
            if not self.host.save_plugin_states(updates, expected=self._snapshot):
                return
        except Exception as error:
            self.host.main_window.appendLog(f"[插件管理] 保存失败：{error}")
            QtWidgets.QMessageBox.warning(self, "插件管理", "保存失败，未改变运行状态。\n" + str(error))
            return
        self.host.main_window.appendLog("[插件管理] 启停设置已保存，请重启程序后生效；当前任务未中断。")
        self.reload()
        QtWidgets.QMessageBox.information(self, "启停设置已保存",
                                         "请重启程序后生效。当前运行中的任务没有被中断，插件数据未删除。")
