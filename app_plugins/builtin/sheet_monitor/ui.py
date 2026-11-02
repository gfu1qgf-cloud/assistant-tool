from qt_compat import QtCore, QtWidgets
from model.GoogleSheetMonitor import normalize_monitor_settings, read_monitor_state
from model.GoogleSheetsHelper import extract_spreadsheet_id


class GoogleSheetMonitorDialog(QtWidgets.QDialog):
    def __init__(self, settings, status_text="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Google 表格监视器")
        self.resize(680, 470)
        self.reset_baseline_requested = False
        settings = normalize_monitor_settings(settings)

        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "监视指定表格中新增的 Google Drive/Docs 链接。第一次启用只建立基线，"
            "不会下载表格里原有的旧链接。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self.enabled_checkbox = QtWidgets.QCheckBox("启用后台监视")
        self.enabled_checkbox.setChecked(settings["enabled"])
        form.addRow("状态：", self.enabled_checkbox)

        self.url_edit = QtWidgets.QLineEdit(settings["sheet_url"])
        self.url_edit.setPlaceholderText("粘贴独立的 Google 表格网址（最好包含 gid）")
        form.addRow("表格网址：", self.url_edit)

        self.range_edit = QtWidgets.QLineEdit(settings["sheet_range"])
        self.range_edit.setPlaceholderText("留空监视整张工作表；也可填 A:A 或 A2:F")
        form.addRow("监视范围：", self.range_edit)

        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(15, 3600)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setValue(settings["poll_seconds"])
        form.addRow("检查间隔：", self.interval_spin)

        self.folder_edit = QtWidgets.QLineEdit(settings["download_folder"])
        self.folder_edit.setPlaceholderText("当前项目目录内的子目录")
        form.addRow("下载子目录：", self.folder_edit)
        layout.addLayout(form)

        state = read_monitor_state()
        self.status_label = QtWidgets.QLabel(
            "运行状态：{}\n待下载：{} 个；下载记录：{} 个".format(
                status_text or "未启动",
                len(state.get("pending", [])),
                len(state.get("history", [])),
            )
        )
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        note = QtWidgets.QLabel(
            "新增链接会通过系统通知、提示音和插件菜单状态提醒。"
            "未加载项目时会持久排队，加载项目后自动下载。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        layout.addWidget(note)

        buttons_row = QtWidgets.QHBoxLayout()
        self.reset_button = QtWidgets.QPushButton("重新建立监视基线")
        self.reset_button.setToolTip("保留待下载队列，但把当前表格中的链接重新视为旧链接")
        self.reset_button.clicked.connect(self._request_reset)
        buttons_row.addWidget(self.reset_button)
        buttons_row.addStretch()
        layout.addLayout(buttons_row)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.button(QtWidgets.QDialogButtonBox.Save).setText("保存并立即检查")
        button_box.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        button_box.accepted.connect(self._accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _request_reset(self):
        answer = QtWidgets.QMessageBox.question(
            self,
            "重新建立基线",
            "下次检查会把表格当前已有链接当作旧链接，不会下载它们。\n"
            "已进入待下载队列的项目会保留。是否继续？",
        )
        if answer == QtWidgets.QMessageBox.Yes:
            self.reset_baseline_requested = True
            self.status_label.setText("已标记：保存后重新建立基线。")

    def _accept(self):
        url = self.url_edit.text().strip()
        if self.enabled_checkbox.isChecked() and not extract_spreadsheet_id(url):
            QtWidgets.QMessageBox.warning(self, "设置不完整", "启用监视前请填写有效的 Google 表格网址。")
            return
        folder = self.folder_edit.text().strip()
        folder_path = QtCore.QDir.cleanPath(folder)
        if not folder or QtCore.QDir.isAbsolutePath(folder) or folder_path.startswith(".."):
            QtWidgets.QMessageBox.warning(self, "目录不正确", "下载子目录必须是当前项目内部的相对目录。")
            return
        self.accept()

    def settings(self):
        return normalize_monitor_settings(
            {
                "enabled": self.enabled_checkbox.isChecked(),
                "sheet_url": self.url_edit.text(),
                "sheet_range": self.range_edit.text(),
                "poll_seconds": self.interval_spin.value(),
                "download_folder": self.folder_edit.text(),
            }
        )

    @classmethod
    def get_settings(cls, settings, status_text="", parent=None):
        dialog = cls(settings, status_text, parent)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return None
        return dialog.settings(), dialog.reset_baseline_requested
