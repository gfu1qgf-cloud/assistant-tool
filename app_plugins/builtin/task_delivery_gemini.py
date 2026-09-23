"""Small editor for the Gemini keys used by task-result AI detection."""

from qt_compat import QtCore, QtWidgets

from model.TaskResultOrganizer import config_list


class GeminiKeysDialog(QtWidgets.QDialog):
    def __init__(self, keys, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 检测 · Gemini Key 管理")
        self.resize(480, 320)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("整理任务结果的 AI 检测会依次使用这里的 Key。"))

        entry_row = QtWidgets.QHBoxLayout()
        self.key_edit = QtWidgets.QLineEdit(self)
        self.key_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("粘贴 Gemini API Key；可一次粘贴多个")
        self.add_button = QtWidgets.QPushButton("添加", self)
        entry_row.addWidget(self.key_edit, 1)
        entry_row.addWidget(self.add_button)
        layout.addLayout(entry_row)

        self.key_list = QtWidgets.QListWidget(self)
        self.key_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.key_list, 1)
        self.remove_button = QtWidgets.QPushButton("删除选中 Key", self)
        layout.addWidget(self.remove_button)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        layout.addWidget(self.buttons)
        self.add_button.clicked.connect(self.add_keys)
        self.key_edit.returnPressed.connect(self.add_keys)
        self.remove_button.clicked.connect(self.remove_selected)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        for key in config_list({"gemini_api_keys": keys}, "gemini_api_keys"):
            self._append_key(key)

    def _append_key(self, key):
        if key in self.keys():
            return
        item = QtWidgets.QListWidgetItem(f"Key {self.key_list.count() + 1} · ••••{key[-4:]}")
        item.setData(QtCore.Qt.UserRole, key)
        self.key_list.addItem(item)

    def keys(self):
        return [
            self.key_list.item(index).data(QtCore.Qt.UserRole)
            for index in range(self.key_list.count())
        ]

    def add_keys(self):
        for key in config_list(
            {"gemini_api_keys": self.key_edit.text()}, "gemini_api_keys"
        ):
            self._append_key(key)
        self.key_edit.clear()

    def remove_selected(self):
        for item in self.key_list.selectedItems():
            self.key_list.takeItem(self.key_list.row(item))
        for index in range(self.key_list.count()):
            item = self.key_list.item(index)
            key = item.data(QtCore.Qt.UserRole)
            item.setText(f"Key {index + 1} · ••••{key[-4:]}")

    def accept(self):
        self.add_keys()  # Saving directly after pasting should not discard the pending key.
        super().accept()
