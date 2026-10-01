"""Main application's shared Gemini credential editor."""
import time
from qt_compat import QtCore, QtWidgets
from model.GeminiKeyManager import GeminiKeyManager, KEYS_CONFIG_KEY, STATUSES_CONFIG_KEY, key_id, normalize_keys
from model.GeminiKeyRecovery import read_backup_keys
from app_paths import APP_ROOT


class GeminiKeysEditor(QtWidgets.QWidget):
    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self.manager = GeminiKeyManager(config or {}, self)
        layout = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel("主程序统一管理，所有插件共用。调用结果自动记录；冷却期间跳过失败 Key。")
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QtWidgets.QHBoxLayout()
        self.key_edit = QtWidgets.QLineEdit(self)
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("粘贴 Gemini Key，可用分号、逗号或换行分隔多个")
        self.add_button = QtWidgets.QPushButton("添加", self)
        row.addWidget(self.key_edit, 1)
        row.addWidget(self.add_button)
        layout.addLayout(row)
        self.key_list = QtWidgets.QListWidget(self)
        self.key_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.key_list, 1)
        self.status_label = QtWidgets.QLabel(self)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        actions = QtWidgets.QHBoxLayout()
        self.remove_button = QtWidgets.QPushButton("删除选中", self)
        self.reset_button = QtWidgets.QPushButton("重置选中状态", self)
        self.recover_button = QtWidgets.QPushButton("从配置备份恢复…", self)
        self.recover_button.setToolTip("只合并备份中的 Gemini Key，不改变其他设置；点击保存后生效。")
        self.reset_button.setToolTip("恢复为未验证，下次调用重新尝试。不会发送网络请求。")
        actions.addWidget(self.remove_button)
        actions.addWidget(self.reset_button)
        actions.addWidget(self.recover_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.add_button.clicked.connect(self.add_keys)
        self.key_edit.returnPressed.connect(self.add_keys)
        self.remove_button.clicked.connect(self.remove_selected)
        self.reset_button.clicked.connect(self.reset_selected)
        self.recover_button.clicked.connect(self.recover_backup)
        self.key_list.currentItemChanged.connect(self.show_status)
        self.load_config(config or {})

    def load_config(self, config):
        self.manager.apply_config(config, merge_local=False)
        self.key_list.clear()
        for key in normalize_keys(config.get(KEYS_CONFIG_KEY, config.get("gemini_api_key", []))):
            self._append_key(key)
        self.show_status()

    def _append_key(self, key):
        if key not in self.keys():
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
            self.key_list.addItem(item)
        self.refresh_labels()

    def refresh_labels(self):
        for index in range(self.key_list.count()):
            item = self.key_list.item(index)
            key = item.data(QtCore.Qt.ItemDataRole.UserRole)
            suffix = key[-4:] if len(key) > 8 else ""
            status = self.manager.status_text(key)
            item.setText(f"Key {index + 1} · ••••{suffix} · {status}")
            item.setToolTip(status)

    def keys(self):
        return [self.key_list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.key_list.count())]

    def add_keys(self):
        for key in normalize_keys(self.key_edit.text()):
            self._append_key(key)
        self.key_edit.clear()

    def remove_selected(self):
        selected = self.key_list.selectedItems()
        if selected and len(selected) == self.key_list.count():
            answer = QtWidgets.QMessageBox.question(
                self, "清空 Gemini Key？", "这会删除全部 Gemini Key，所有插件将无法调用 Gemini。\n确定继续吗？",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        for item in selected:
            self.key_list.takeItem(self.key_list.row(item))
        self.refresh_labels()
        self.show_status()

    def recover_backup(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择含 Gemini Key 的配置备份", str(APP_ROOT),
            "配置备份 (config.json* *.json);;所有文件 (*)",
        )
        if not path:
            return
        try:
            keys = read_backup_keys(path)
        except (OSError, ValueError, TypeError):
            QtWidgets.QMessageBox.warning(self, "无法恢复", "文件无效，或不包含 Gemini Key。未修改配置。")
            return
        before = len(self.keys())
        for key in keys:
            self._append_key(key)
        QtWidgets.QMessageBox.information(
            self, "已读取备份", f"新增 {len(self.keys()) - before} 个 Key，目前共 {len(self.keys())} 个。\n点击保存后生效，其他设置不会从备份恢复。",
        )

    def reset_selected(self):
        config = self.manager.snapshot()
        for item in self.key_list.selectedItems():
            fingerprint = key_id(item.data(QtCore.Qt.ItemDataRole.UserRole))
            record = config[STATUSES_CONFIG_KEY].setdefault(fingerprint, {"auth": {}, "models": {}})
            reset = {"state": "unknown", "checked_at": time.time(), "blocked_until": 0, "http_status": 0}
            record["auth"] = dict(reset)
            record["models"] = {model: dict(reset) for model in record.get("models", {})}
        # Include newly added keys so their reset is not discarded by snapshot().
        config[KEYS_CONFIG_KEY] = self.keys()
        self.manager.apply_config(config, merge_local=False)
        self.refresh_labels()
        self.show_status()

    def show_status(self, *_args):
        item = self.key_list.currentItem()
        self.status_label.setText(self.manager.status_text(item.data(QtCore.Qt.ItemDataRole.UserRole))
                                 if item else "选中一个 Key 查看状态；首次调用之前显示未验证。")

    def get_config(self):
        config = self.manager.snapshot()
        config[KEYS_CONFIG_KEY] = self.keys() + [k for k in normalize_keys(self.key_edit.text()) if k not in self.keys()]
        return config


class GeminiKeysDialog(QtWidgets.QDialog):
    def __init__(self, keys=None, parent=None, *, config=None):
        super().__init__(parent)
        self.setWindowTitle("AI 密钥管理 · Gemini")
        self.resize(660, 420)
        layout = QtWidgets.QVBoxLayout(self)
        self.editor = GeminiKeysEditor(config if config is not None else {KEYS_CONFIG_KEY: keys or []}, self)
        layout.addWidget(self.editor)
        self.key_edit, self.key_list = self.editor.key_edit, self.editor.key_list
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save
                                                | QtWidgets.QDialogButtonBox.StandardButton.Cancel, parent=self)
        layout.addWidget(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

    def keys(self):
        return self.editor.keys()

    def add_keys(self):
        self.editor.add_keys()

    def remove_selected(self):
        self.editor.remove_selected()

    def get_config(self):
        return self.editor.get_config()

    def accept(self):
        self.add_keys()
        super().accept()
