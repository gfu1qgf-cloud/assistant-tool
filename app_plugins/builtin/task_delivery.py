"""Task-result delivery entry points and host-controlled quick actions."""

from qt_compat import QtWidgets

from app_plugins.api import MAIN_MENU, PluginCommand, PluginMainWidget
from app_plugins.builtin.task_delivery_controller import TaskDeliveryController
from app_plugins.builtin.task_delivery_gemini import GeminiKeysDialog
from app_plugins.builtin.task_delivery_inbox import DeliveryInboxDialog
from app_plugins.builtin.task_delivery_quick_upload import (
    QuickUploadDialog,
    QuickUploadThread,
)
from model.TaskResultOrganizer import load_effective_config


class TaskDeliveryQuickActions(QtWidgets.QWidget):
    """Plugin-owned controls mounted only through PluginHost."""

    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setObjectName("task_delivery_quick_actions")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.organize_button = QtWidgets.QPushButton("整理任务结果", self)
        self.organize_button.setObjectName("tidy_task_result_btn")
        layout.addWidget(self.organize_button)

        secondary = QtWidgets.QHBoxLayout()
        secondary.setContentsMargins(0, 0, 0, 0)
        secondary.setSpacing(8)
        self.daily_links_button = QtWidgets.QPushButton("查看每日链接", self)
        self.daily_links_button.setObjectName("daily_links_btn")
        self.review_status_button = QtWidgets.QPushButton("审核提醒", self)
        self.review_status_button.setObjectName("review_status_btn")
        secondary.addWidget(self.daily_links_button)
        secondary.addWidget(self.review_status_button)
        layout.addLayout(secondary)

        self.organize_button.clicked.connect(
            lambda _checked=False: plugin.organize_results()
        )
        self.daily_links_button.clicked.connect(
            lambda _checked=False: plugin.open_daily_links()
        )
        self.review_status_button.clicked.connect(
            lambda _checked=False: plugin.open_review_status()
        )


class TaskDeliveryPlugin:
    plugin_id = "task_delivery"
    display_name = "任务交付"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.quick_actions = None
        self.controller = None
        self.quick_upload_dialog = None
        self.quick_upload_thread = None

    def register(self, context):
        self.context = context
        self.controller = TaskDeliveryController(self, context)
        for command_id, title, callback, order, tooltip in (
            (
                "organize",
                "整理任务结果",
                self.organize_results,
                10,
                "整理、检测、上传当前日期的任务结果",
            ),
            (
                "daily_links",
                "查看每日链接",
                self.open_daily_links,
                20,
                "查看最近七天的人员文件夹链接和任务表待核对记录",
            ),
            (
                "review_status",
                "审核提醒",
                self.open_review_status,
                30,
                "查看审核通过、需要修改和历史提交记录",
            ),
            (
                "delivery_inbox",
                "交付待办…",
                self.open_delivery_inbox,
                35,
                "汇总本地上传、任务表待核对和审核待办记录",
            ),
            (
                "gemini_keys",
                "管理 AI 检测 Gemini Key…",
                self.open_gemini_keys,
                40,
                "添加或删除整理任务结果 AI 检测使用的 Gemini Key",
            ),
            (
                "quick_upload",
                "简易上传…",
                self.open_quick_upload,
                50,
                "直接上传零散文件到当前日期和批次下的自定文件夹",
            ),
        ):
            context.register_command(
                PluginCommand(
                    command_id=command_id,
                    title=title,
                    callback=lambda _rows, action=callback: action(),
                    locations=frozenset({MAIN_MENU}),
                    tooltip=tooltip,
                    order=order,
                    submenu="任务交付",
                )
            )
        context.register_main_widget(
            PluginMainWidget(
                widget_id="quick_actions",
                factory=self.create_quick_actions,
                order=10,
                title="任务交付",
            )
        )

    def create_quick_actions(self, parent=None):
        self.quick_actions = TaskDeliveryQuickActions(self, parent)
        return self.quick_actions

    def organize_results(self):
        return self.controller.organize_results()

    def open_daily_links(self):
        return self.controller.open_daily_links()

    def open_review_status(self):
        return self.controller.open_review_status()

    def open_delivery_inbox(self):
        return DeliveryInboxDialog(
            self.context.load_config(),
            self.controller.daily_link_history,
            self.context.parent_widget,
        ).exec()

    def open_gemini_keys(self):
        dialog = GeminiKeysDialog(
            self.controller.gemini_api_keys, self.context.parent_widget
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return False
        previous = self.controller.gemini_api_keys
        self.controller.gemini_api_keys = dialog.keys()
        if not self.context.save_config():
            self.controller.gemini_api_keys = previous
            return False
        self.context.log(f"AI 检测 Gemini Key 已保存，共 {len(dialog.keys())} 个。")
        return True

    def open_quick_upload(self):
        if self.quick_upload_dialog is None:
            dialog = QuickUploadDialog(self.context.parent_widget)
            dialog.upload_requested.connect(self.start_quick_upload)
            self.quick_upload_dialog = dialog
        self.quick_upload_dialog.refresh_batch()
        self.quick_upload_dialog.show()
        self.quick_upload_dialog.raise_()
        self.quick_upload_dialog.activateWindow()
        return self.quick_upload_dialog

    def start_quick_upload(self, name, sources):
        if self.quick_upload_thread is not None and self.quick_upload_thread.isRunning():
            return False
        config = load_effective_config(self.context.load_config())
        parent_folder = str(config.get("drive_parent_folder_id") or "").strip()
        if not parent_folder:
            QtWidgets.QMessageBox.warning(
                self.quick_upload_dialog,
                "无法上传",
                "请先在程序设置 → 整理任务结果中填写 Drive 父目录。",
            )
            return False
        thread = QuickUploadThread(
            sources, name, parent_folder, self.context.parent_widget
        )
        self.quick_upload_thread = thread
        thread.progress.connect(self._on_quick_upload_progress)
        thread.completed.connect(self._on_quick_upload_completed)
        thread.failed.connect(self._on_quick_upload_failed)
        thread.finished.connect(self._on_quick_upload_finished)
        dialog = self.quick_upload_dialog
        dialog.link_edit.clear()
        dialog.copy_button.setEnabled(False)
        dialog.status_label.setText("正在连接 Google Drive…")
        dialog.progress_bar.setRange(0, 0)
        dialog.set_busy(True)
        self.context.log(f"简易上传开始：{name}（{len(sources)} 个来源）")
        thread.start()
        return True

    def _on_quick_upload_progress(self, done, total, label):
        if self.quick_upload_dialog is not None:
            self.quick_upload_dialog.update_progress(done, total, label)

    def _on_quick_upload_completed(self, result):
        if self.quick_upload_dialog is not None:
            self.quick_upload_dialog.show_result(result)
        self.context.log(
            f"简易上传完成：{result['batch']}/{result['folder_name']}；"
            f"上传 {result['uploaded']}，未变化 {result['skipped']}，"
            f"失败 {len(result['failures'])}。链接：{result['folder_link']}"
        )
        for failure in result["failures"]:
            self.context.log(f"简易上传文件失败：{failure}")
        self.context.notify(
            "简易上传有失败文件" if result["failures"] else "简易上传完成",
            f"{result['folder_name']}：成功 {result['uploaded']}，失败 {len(result['failures'])}。",
            critical=bool(result["failures"]),
        )

    def _on_quick_upload_failed(self, error):
        self.context.log(f"简易上传失败：{error}")
        self.context.notify("简易上传失败", error, critical=True)
        if self.quick_upload_dialog is not None:
            self.quick_upload_dialog.set_busy(False)
            self.quick_upload_dialog.progress_bar.setRange(0, 1)
            self.quick_upload_dialog.status_label.setText(f"上传失败：{error}")
            QtWidgets.QMessageBox.critical(self.quick_upload_dialog, "简易上传失败", error)

    def _on_quick_upload_finished(self):
        thread = self.quick_upload_thread
        self.quick_upload_thread = None
        if thread is not None:
            thread.deleteLater()

    @property
    def global_hotkey(self):
        return self.controller.global_hotkey

    def start(self):
        self.controller.start()

    def apply_settings(self, config):
        return self.controller.apply_settings(config)

    def update_config(self, config):
        return self.controller.update_config(config)

    def can_close(self):
        if self.quick_upload_thread is not None and self.quick_upload_thread.isRunning():
            return False, "简易上传仍在进行，请等待上传完成。"
        return self.controller.can_close()

    def stop(self):
        self.controller.stop()
