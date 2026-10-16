"""GUI preflight for missing delivery settings; no network or file operations."""

import re
from urllib.parse import urlsplit

from qt_compat import QtCore, QtWidgets
from model.TaskResultOrganizer import config_bool, config_str


FIELD_LABELS = {
    "drive_parent_folder_id": "网盘上传父目录",
    "creator_marker": "文件名制作人标记",
    "task_submission_creator": "任务制作人姓名",
    "review_sheet_submitter": "审核提交人姓名",
}


def missing_delivery_fields(config):
    """Check only settings needed by the enabled operation, not optional sheets."""
    if not config_bool(config, "run_upload", True):
        return {}
    fields = {}
    if not config_str(config, "drive_parent_folder_id"):
        fields["drive_parent_folder_id"] = ""
    submission = config_bool(config, "task_submission_sheet_enabled", True) and bool(
        config_str(config, "task_submission_sheet_url")
    )
    routing = config_bool(config, "upload_only_changed_files", True)
    video_marker = config_str(config, "video_filename_creator_marker")
    submission_marker = config_str(config, "task_submission_creator_marker", video_marker)
    if (routing and not video_marker) or (submission and not submission_marker):
        fields["creator_marker"] = video_marker or submission_marker
    if submission and not config_str(
        config, "task_submission_creator", config_str(config, "review_sheet_submitter")
    ):
        fields["task_submission_creator"] = config_str(config, "review_sheet_submitter")
    if (config_bool(config, "review_sheet_enabled", True)
            and config_str(config, "review_sheet_url")
            and not config_str(config, "review_sheet_submitter")):
        fields["review_sheet_submitter"] = config_str(config, "task_submission_creator")
    return fields


def delivery_config_updates(config, values):
    """Patch missing fields only; never overwrite an already configured identity."""
    updates = {}
    fields = missing_delivery_fields(config)
    for key in fields:
        value = str(values.get(key) or "").strip()
        if not value:
            raise ValueError(f"请填写{FIELD_LABELS[key]}。")
        if any(ord(char) < 32 for char in value):
            raise ValueError(f"{FIELD_LABELS[key]}不能包含换行或控制字符。")
        if key == "creator_marker":
            if any(char in value for char in '<>:"/\\|?*'):
                raise ValueError("文件名制作人标记不能包含文件名禁用字符。")
            if not config_str(config, "video_filename_creator_marker"):
                updates["video_filename_creator_marker"] = value
            if not config_str(config, "task_submission_creator_marker"):
                updates["task_submission_creator_marker"] = value
        else:
            if key == "drive_parent_folder_id":
                url = urlsplit(value)
                valid_id = bool(re.fullmatch(r"[a-zA-Z0-9_-]+", value))
                valid_link = (url.scheme == "https" and url.hostname == "drive.google.com"
                              and bool(re.search(r"/folders/[a-zA-Z0-9_-]+", url.path)))
                if not (valid_id or valid_link):
                    raise ValueError("请填写 Google Drive 文件夹链接或文件夹 ID，不能填写文件链接。")
            updates[key] = value
    return updates


class TaskDeliveryConfigDialog(QtWidgets.QDialog):
    def __init__(self, config, parent=None):
        app = QtWidgets.QApplication.instance()
        if app is None or QtCore.QThread.currentThread() != app.thread():
            raise RuntimeError("配置填写窗口只能在 GUI 主线程打开")
        super().__init__(parent)
        self.config = dict(config)
        self._updates = {}
        self.editors = {}
        self.setWindowTitle("补全整理 / 上传配置")
        self.setMinimumWidth(460)
        layout = QtWidgets.QVBoxLayout(self)
        description = QtWidgets.QLabel(
            "本次操作还缺少以下配置。填写后保存并继续，以后无需重复填写。\n"
            "取消不会整理、上传或改动待上传记录。", self
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QtWidgets.QFormLayout()
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for key, default in missing_delivery_fields(config).items():
            editor = QtWidgets.QLineEdit(default, self)
            editor.setObjectName(key)
            if key == "drive_parent_folder_id":
                editor.setPlaceholderText("粘贴 Google Drive 文件夹链接或 ID")
            elif key == "creator_marker":
                editor.setPlaceholderText("填写成品文件名中使用的制作人标记")
                editor.setToolTip("例如：管理员ABC-1006-01-标题.mp4 中的 ABC。不是管理员姓名。")
            else:
                editor.setPlaceholderText("与对应谷歌表格中的姓名保持一致")
            form.addRow(FIELD_LABELS[key] + "：", editor)
            self.editors[key] = editor
        layout.addLayout(form)
        self.error_label = QtWidgets.QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b42318;")
        self.error_label.hide()
        layout.addWidget(self.error_label)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText("保存并继续")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if self.editors:
            next(iter(self.editors.values())).setFocus()

    def accept(self):
        try:
            self._updates = delivery_config_updates(
                self.config, {key: editor.text() for key, editor in self.editors.items()}
            )
        except ValueError as error:
            self.error_label.setText(str(error))
            self.error_label.show()
            return
        super().accept()

    def config_updates(self):
        return dict(self._updates)
