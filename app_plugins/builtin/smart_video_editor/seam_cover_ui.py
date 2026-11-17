"""Shared review/settings controls for optional join-cover footage."""
from pathlib import Path
from qt_compat import QtWidgets
from .seam_cover import ENABLED, SOURCE, DURATION, normalize_options, validate_source


class SeamCoverControls(QtWidgets.QWidget):
    def __init__(self, settings, parent=None, *, compact=False):
        super().__init__(parent)
        self.compact = compact
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.enabled_checkbox = QtWidgets.QCheckBox("接缝补画面" if compact else "接缝补画面（B-roll）", self)
        self.source_edit = QtWidgets.QLineEdit(self)
        self.source_edit.setPlaceholderText("选择用于遮住跳切的视频")
        self.source_edit.setMinimumWidth(100)
        self.browse_button = QtWidgets.QPushButton("设置…" if compact else "选择视频…", self)
        self.duration_spinbox = QtWidgets.QDoubleSpinBox(self)
        self.duration_spinbox.setRange(0.5, 5.0)
        self.duration_spinbox.setDecimals(1)
        self.duration_spinbox.setSingleStep(0.5)
        self.duration_spinbox.setSuffix(" 秒 / 处")
        row.addWidget(self.enabled_checkbox)
        if not compact:
            row.addWidget(self.source_edit, 1)
        else:
            self.source_edit.hide()
            self.duration_spinbox.hide()
        row.addWidget(self.browse_button)
        if not compact:
            row.addWidget(self.duration_spinbox)
        self.setToolTip("仅在合并视频时生效：在每个实际拼接点前后覆盖补画面，保留访谈原声和总时长。"
                        "补画面从头顺序取用，用完循环；相邻覆盖重叠时合并。"
                        "只处理本次剪辑接缝，不检测源视频内部转场。效果在导出成片中可见，核对预览仍显示原片。")
        self.enabled_checkbox.toggled.connect(self._update_state)
        self.browse_button.clicked.connect(self._configure if compact else self._browse)
        self.source_edit.textChanged.connect(self._update_summary)
        self.duration_spinbox.valueChanged.connect(self._update_summary)
        self.load_settings(settings)

    def _update_state(self):
        enabled = self.enabled_checkbox.isChecked()
        for widget in (self.source_edit, self.browse_button, self.duration_spinbox):
            widget.setEnabled(enabled)

    def _browse(self):
        selected, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择接缝补画面视频（不使用其声音）", self.source_edit.text().strip(),
            "视频文件 (*.mp4 *.mov *.mkv *.m4v *.avi *.webm *.mts);;所有文件 (*)")
        if selected:
            self.source_edit.setText(selected)

    def _update_summary(self):
        name = Path(self.source_edit.text().strip()).name
        self.browse_button.setToolTip(
            f"{name or '尚未选择补画面视频'} · 每处 {self.duration_spinbox.value():g} 秒\n"
            "补画面静音，顺序取用并循环；访谈原声和总时长保持。")

    def _configure(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("接缝补画面设置")
        layout = QtWidgets.QVBoxLayout(dialog)
        note = QtWidgets.QLabel("在每个拼接点前后覆盖补画面，保留访谈原声和成片总时长。\n"
                               "补画面顺序取用，用完循环；效果在导出成片中可见。", dialog)
        note.setWordWrap(True)
        layout.addWidget(note)
        controls = SeamCoverControls(self.selection(), dialog)
        layout.addWidget(controls)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel, dialog)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("取消")
        def accept():
            try:
                controls.selection(validate=True)
            except ValueError as error:
                QtWidgets.QMessageBox.warning(dialog, "补画面视频未选择", str(error))
                return
            dialog.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(700, dialog.sizeHint().height())
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.load_settings(controls.selection())
        dialog.deleteLater()

    def load_settings(self, settings):
        options = normalize_options(settings)
        self.enabled_checkbox.setChecked(options[ENABLED])
        self.source_edit.setText(options[SOURCE])
        self.duration_spinbox.setValue(options[DURATION] / 1000)
        self._update_state()
        self._update_summary()

    def selection(self, validate=False):
        options = {ENABLED: self.enabled_checkbox.isChecked(), SOURCE: self.source_edit.text().strip(),
                   DURATION: round(self.duration_spinbox.value() * 1000)}
        if validate and options[ENABLED]:
            validate_source(options)
        return options
