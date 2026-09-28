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
from app_plugins.builtin.task_delivery_daily_quantity import (
    DailyQuantityCategoriesThread,
    DailyQuantityDateThread,
    DailyQuantityDialog,
    DailyQuantityFolderThread,
    DailyQuantityThread,
)
from model.DailyQuantityStats import (
    external_video_records,
    external_video_sources,
    pending_review_quantity_records,
    update_external_video_records,
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
        self.daily_quantity_dialog = None
        self.daily_quantity_thread = None
        self.daily_quantity_categories_thread = None
        self.daily_quantity_folder_thread = None
        self.daily_quantity_date_thread = None

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
                "查看历史人员文件夹链接和最近七天的任务表待核对记录",
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
            (
                "daily_quantity",
                "每日数量统计…",
                self.open_daily_quantity,
                55,
                "查看漏填分类并刷新每日数量统计",
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

    def open_daily_quantity(self):
        if self.daily_quantity_dialog is None:
            dialog = DailyQuantityDialog(self.context.parent_widget)
            dialog.refresh_requested.connect(self.refresh_daily_quantity)
            dialog.scan_requested.connect(self.scan_daily_quantity_folder)
            dialog.scan_date_requested.connect(self.scan_daily_quantity_date)
            dialog.view_date_requested.connect(self.view_daily_quantity_date)
            dialog.review_check_requested.connect(
                lambda: self.controller.request_review_status_check(manual=True)
            )
            dialog.edit_sheet_requested.connect(self.edit_daily_quantity_sheet)
            self.daily_quantity_dialog = dialog
            try:
                dialog.show_external_records(external_video_records(
                    self.context.load_config(),
                    self.context.parent_widget.task_path_edit.text().strip(),
                ), day=dialog.folder_day.date().toString("yyyy-MM-dd"))
                dialog.show_external_sources(external_video_sources(
                    self.context.load_config(),
                    self.context.parent_widget.task_path_edit.text().strip(),
                ))
            except ValueError:
                pass
        self.refresh_daily_quantity_review_queue()
        self.daily_quantity_dialog.set_sheet_url(
            self.context.load_config().get("daily_quantity_sheet_url")
        )
        self.daily_quantity_dialog.show()
        self.daily_quantity_dialog.raise_()
        self.daily_quantity_dialog.activateWindow()
        self.load_daily_quantity_categories()
        return self.daily_quantity_dialog

    def refresh_daily_quantity_review_queue(self):
        dialog = self.daily_quantity_dialog
        if dialog is None:
            return
        try:
            dialog.show_pending_reviews(pending_review_quantity_records(
                self.context.load_config(),
                self.context.parent_widget.task_path_edit.text().strip(),
            ))
        except (OSError, ValueError) as error:
            self.context.log(f"每日数量待审核清单读取失败：{error}")

    def edit_daily_quantity_sheet(self):
        self.context.parent_widget.openSettings(focus_daily_quantity=True)
        if self.daily_quantity_dialog is not None:
            self.daily_quantity_dialog.set_sheet_url(
                self.context.load_config().get("daily_quantity_sheet_url")
            )
        self.load_daily_quantity_categories()

    def load_daily_quantity_categories(self):
        dialog = self.daily_quantity_dialog
        if dialog is None:
            return False
        config = self.context.load_config()
        url = str(config.get("daily_quantity_sheet_url") or "").strip()
        if not url:
            dialog.show_category_error("请先设置每日数量表格链接")
            return False
        thread = self.daily_quantity_categories_thread
        if thread is not None and thread.isRunning():
            return False
        thread = DailyQuantityCategoriesThread(config, self.context.parent_widget)
        self.daily_quantity_categories_thread = thread
        thread.completed.connect(lambda options: self._daily_quantity_categories_completed(thread, options))
        thread.failed.connect(lambda error: self._daily_quantity_categories_failed(thread, error))
        thread.finished.connect(lambda: self._daily_quantity_categories_finished(thread))
        dialog.set_category_loading()
        thread.start()
        return True

    def _daily_quantity_categories_completed(self, thread, options):
        if (self.daily_quantity_dialog is not None
                and thread.sheet_url == self.daily_quantity_dialog.sheet_url.text()):
            self.daily_quantity_dialog.show_category_options(options)

    def _daily_quantity_categories_failed(self, thread, error):
        if (self.daily_quantity_dialog is not None
                and thread.sheet_url == self.daily_quantity_dialog.sheet_url.text()):
            self.daily_quantity_dialog.show_category_error(error)
        self.context.log("每日数量分类读取失败：" + error)

    def _daily_quantity_categories_finished(self, thread):
        if self.daily_quantity_categories_thread is thread:
            self.daily_quantity_categories_thread = None
        thread.deleteLater()
        if (self.daily_quantity_dialog is not None
                and self.daily_quantity_dialog.sheet_url.text()
                != thread.sheet_url):
            self.load_daily_quantity_categories()

    def refresh_daily_quantity(self):
        if self.daily_quantity_thread is not None and self.daily_quantity_thread.isRunning():
            return False
        if self.daily_quantity_folder_thread is not None and self.daily_quantity_folder_thread.isRunning():
            return False
        if self.daily_quantity_date_thread is not None and self.daily_quantity_date_thread.isRunning():
            return False
        root = self.context.parent_widget.task_path_edit.text().strip()
        config = self.context.load_config()
        from pathlib import Path
        if not Path(root).is_dir():
            if self.daily_quantity_dialog is not None:
                self.daily_quantity_dialog.show_error("请先选择有效的任务根目录。")
            return False
        if not str(config.get("daily_quantity_sheet_url") or "").strip():
            if self.daily_quantity_dialog is not None:
                self.daily_quantity_dialog.show_error(
                    "请先在程序设置 → 整理任务结果填写每日数量表格链接。"
                )
            return False
        if not self._save_daily_quantity_external_edits(config, root):
            return False
        thread = DailyQuantityThread(config, root, self.context.parent_widget)
        self.daily_quantity_thread = thread
        thread.completed.connect(self._daily_quantity_completed)
        thread.failed.connect(self._daily_quantity_failed)
        thread.finished.connect(self._daily_quantity_finished)
        if self.daily_quantity_dialog is not None:
            self.daily_quantity_dialog.set_busy(True)
        thread.start()
        return True

    def _save_daily_quantity_external_edits(self, config, root):
        dialog = self.daily_quantity_dialog
        if dialog is None or not dialog.external_table.rowCount():
            return True
        edits = dialog.changed_external_edits()
        if not edits:
            return True
        try:
            update_external_video_records(config, root, edits)
        except (OSError, ValueError) as error:
            message = f"保存视频分类失败：{error}。当前清单未切换，修改仍保留在窗口中。"
            dialog.show_error(message)
            dialog.folder_status.setText(message)
            return False
        dialog.mark_external_edits_saved()
        return True

    def scan_daily_quantity_folder(self, link, day, slot):
        if self.daily_quantity_folder_thread is not None and self.daily_quantity_folder_thread.isRunning():
            return False
        if self.daily_quantity_thread is not None and self.daily_quantity_thread.isRunning():
            return False
        if self.daily_quantity_date_thread is not None and self.daily_quantity_date_thread.isRunning():
            return False
        root = self.context.parent_widget.task_path_edit.text().strip()
        config = self.context.load_config()
        from pathlib import Path
        if not Path(root).is_dir() or not str(config.get("daily_quantity_sheet_url") or "").strip():
            self.daily_quantity_dialog.show_error("请先选择任务根目录并设置每日数量表格链接。")
            return False
        if not self._save_daily_quantity_external_edits(config, root):
            return False
        thread = DailyQuantityFolderThread(
            config, root, link, day, slot, self.context.parent_widget
        )
        self.daily_quantity_folder_thread = thread
        thread.completed.connect(self._daily_quantity_folder_completed)
        thread.failed.connect(self._daily_quantity_folder_failed)
        thread.finished.connect(self._daily_quantity_folder_finished)
        self.daily_quantity_dialog.set_folder_busy(True)
        thread.start()
        return True

    def _daily_quantity_folder_completed(self, result):
        dialog = self.daily_quantity_dialog
        if dialog is not None:
            dialog.set_folder_busy(False)
            dialog.show_external_records(
                result["records"], day=dialog.folder_day.date().toString("yyyy-MM-dd")
            )
            dialog.show_video_list()
            dialog.show_external_sources(result["sources"])
            dialog.folder_status.setText(
                f"扫描到 {result['found']} 个视频；新增 {result['added']} 个，"
                f"同名替换 {result['replaced']} 个。请填写统计分页和类别后刷新。"
            )
        self.context.log(
            f"每日数量：扫描网盘文件夹，视频 {result['found']}，新增 {result['added']}。"
        )
        self.refresh_daily_quantity_review_queue()

    def _daily_quantity_folder_failed(self, error):
        self.context.log(f"每日数量文件夹扫描失败：{error}")
        if self.daily_quantity_dialog is not None:
            self.daily_quantity_dialog.set_folder_busy(False)
            self.daily_quantity_dialog.folder_status.setText(f"扫描失败：{error}")

    def _daily_quantity_folder_finished(self):
        thread = self.daily_quantity_folder_thread
        self.daily_quantity_folder_thread = None
        if thread is not None:
            thread.deleteLater()

    def view_daily_quantity_date(self, day):
        dialog = self.daily_quantity_dialog
        if dialog is None:
            return False
        config = self.context.load_config()
        root = self.context.parent_widget.task_path_edit.text().strip()
        if not self._save_daily_quantity_external_edits(config, root):
            return False
        try:
            rows = external_video_records(config, root)
        except (OSError, ValueError) as error:
            dialog.show_error(str(error))
            return False
        dialog.show_external_records(rows, day=day)
        dialog.show_video_list()
        dialog.folder_status.setText(
            f"{day} 已保存 {dialog.external_table.rowCount()} 个视频。"
        )
        return True

    def scan_daily_quantity_date(self, day):
        if any(thread is not None and thread.isRunning() for thread in (
            self.daily_quantity_thread,
            self.daily_quantity_folder_thread,
            self.daily_quantity_date_thread,
        )):
            return False
        dialog = self.daily_quantity_dialog
        root = self.context.parent_widget.task_path_edit.text().strip()
        config = self.context.load_config()
        from pathlib import Path
        if not Path(root).is_dir() or not str(config.get("daily_quantity_sheet_url") or "").strip():
            dialog.show_error("请先选择任务根目录并设置每日数量表格链接。")
            return False
        if not self._save_daily_quantity_external_edits(config, root):
            return False
        thread = DailyQuantityDateThread(config, root, day, self.context.parent_widget)
        self.daily_quantity_date_thread = thread
        thread.completed.connect(self._daily_quantity_date_completed)
        thread.failed.connect(self._daily_quantity_date_failed)
        thread.finished.connect(self._daily_quantity_date_finished)
        dialog.set_folder_busy(True)
        thread.start()
        return True

    def _daily_quantity_date_completed(self, result):
        dialog = self.daily_quantity_dialog
        if dialog is not None:
            dialog.set_folder_busy(False)
            dialog.show_external_records(result["records"], day=result["date"])
            dialog.show_video_list()
            dialog.folder_status.setText(
                f"{result['date']}：网盘视频 {result['found']} 个，新增 {result['added']} 个，"
                f"同路径替换 {result['replaced']} 个，"
                f"待分类 {result['pending']} 个，审核暂存 {result['review']} 个，"
                f"疑似重复 {result['possible_duplicates']} 个，"
                f"疑似旧任务修订 {result['possible_revisions']} 个。"
            )
        self.context.log(
            f"每日数量：已扫描 {result['date']} 网盘目录，共 {result['found']} 个视频。"
        )
        self.refresh_daily_quantity_review_queue()

    def _daily_quantity_date_failed(self, error):
        self.context.log(f"每日数量日期目录扫描失败：{error}")
        if self.daily_quantity_dialog is not None:
            self.daily_quantity_dialog.set_folder_busy(False)
            self.daily_quantity_dialog.folder_status.setText(f"日期目录扫描失败：{error}")

    def _daily_quantity_date_finished(self):
        thread = self.daily_quantity_date_thread
        self.daily_quantity_date_thread = None
        if thread is not None:
            thread.deleteLater()

    def _daily_quantity_completed(self, result):
        if self.daily_quantity_dialog is not None:
            self.daily_quantity_dialog.set_busy(False)
            self.daily_quantity_dialog.show_result(result)
        self.refresh_daily_quantity_review_queue()
        warnings = result.get("warnings", [])
        self.context.log(
            f"每日数量统计：已归类 {result.get('counted', 0)} 个视频，"
            f"更新 {len(result.get('updated', []))} 格，待处理 {len(warnings)} 条。"
        )
        if warnings:
            self.context.notify(
                "每日数量统计待核对",
                f"有 {len(warnings)} 条漏填分类或表格冲突；在任务交付 → 每日数量统计中查看并刷新。",
                critical=False,
            )
            if self.daily_quantity_dialog is not None:
                self.daily_quantity_dialog.show()
                self.daily_quantity_dialog.raise_()

    def _daily_quantity_failed(self, error):
        self.context.log(f"每日数量统计失败：{error}")
        self.context.notify("每日数量统计失败", error, critical=True)
        if self.daily_quantity_dialog is not None:
            self.daily_quantity_dialog.set_busy(False)
            self.daily_quantity_dialog.show_error(error)

    def _daily_quantity_finished(self):
        thread = self.daily_quantity_thread
        self.daily_quantity_thread = None
        if thread is not None:
            thread.deleteLater()

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
        if self.daily_quantity_categories_thread is not None and self.daily_quantity_categories_thread.isRunning():
            return False, "每日数量分类正在读取，请稍等。"
        if self.daily_quantity_date_thread is not None and self.daily_quantity_date_thread.isRunning():
            return False, "每日数量日期目录正在扫描，请稍等。"
        if self.daily_quantity_folder_thread is not None and self.daily_quantity_folder_thread.isRunning():
            return False, "每日数量文件夹正在扫描，请稍等。"
        if self.daily_quantity_thread is not None and self.daily_quantity_thread.isRunning():
            return False, "每日数量统计正在更新，请稍等。"
        if self.quick_upload_thread is not None and self.quick_upload_thread.isRunning():
            return False, "简易上传仍在进行，请等待上传完成。"
        return self.controller.can_close()

    def stop(self):
        self.controller.stop()
