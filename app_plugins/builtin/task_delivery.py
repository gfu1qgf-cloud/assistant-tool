"""Task-result delivery entry points and host-controlled quick actions."""

from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
import logging

from qt_compat import QtCore, QtWidgets

from app_plugins.api import MAIN_MENU, PluginCommand, PluginMainWidget
from app_plugins.builtin.task_delivery_controller import TaskDeliveryController
from app_plugins.builtin.task_delivery_board import DeliveryBoardDialog, DeliveryBoardWidget
from model.DeliveryTodoStore import DeliveryTodoStore, collect_delivery_todos
from model.ReviewSubmissionHistory import review_history_snapshot
from model.VideoUploadHistory import load_video_upload_history
from app_plugins.builtin.task_delivery_quick_upload import (
    QuickUploadDialog,
)
from app_plugins.builtin.task_delivery_daily_quantity import (
    DailyQuantityDateThread,
    DailyQuantityDialog,
    DailyQuantityFolderThread,
    DailyQuantityInspectionThread,
    DailyQuantityThread,
)
from model.DailyQuantityStats import (
    daily_quantity_sync_progress,
    external_video_records,
    external_video_sources,
    pending_review_quantity_records,
    set_daily_quantity_ignored,
    update_external_video_records,
)
from model.TaskResultOrganizer import load_effective_config
from model.TaskResultOrganizer import get_upload_batch
from model.TaskDeliveryBackground import UPLOAD_RESOURCE, run_quick_upload
from model.DailyQuantityCategories import CategoryStore
from app_plugins.builtin.task_delivery_category_editor import CategoryEditorDialog


BOARD_EXPANDED_CONFIG_KEY = "task_delivery_board_expanded"


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
        from .task_delivery_workflow_ui import PostprocessChoice
        postprocess = QtWidgets.QHBoxLayout()
        postprocess.addWidget(QtWidgets.QLabel('后续处理', self))
        self.postprocess_choice = PostprocessChoice(plugin.context, self)
        postprocess.addWidget(self.postprocess_choice, 1)
        layout.addLayout(postprocess)

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

        self.board_widget = None
        disclosure = QtWidgets.QHBoxLayout()
        self.todo_toggle = QtWidgets.QToolButton(self)
        self.todo_toggle.setCheckable(True)
        self.todo_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.todo_toggle.setArrowType(QtCore.Qt.ArrowType.RightArrow)
        self.todo_toggle.setText("交付待办 · 0 项")
        self.todo_toggle.setToolTip("展开 / 收起四象限；勾选完成，右键查看视频明细与网盘链接")
        self.todo_toggle.toggled.connect(self._toggle_board)
        disclosure.addWidget(self.todo_toggle)
        disclosure.addStretch(1)
        self.full_board_button = QtWidgets.QToolButton(self)
        self.full_board_button.setText("↗")
        self.full_board_button.setToolTip("打开完整四象限窗口（含刚完成、归档）")
        self.full_board_button.clicked.connect(lambda: plugin.open_delivery_inbox())
        disclosure.addWidget(self.full_board_button)
        layout.addLayout(disclosure)

        self.organize_button.clicked.connect(
            lambda _checked=False: plugin.organize_results()
        )
        self.daily_links_button.clicked.connect(
            lambda _checked=False: plugin.open_daily_links()
        )
        self.review_status_button.clicked.connect(
            lambda _checked=False: plugin.open_review_status()
        )

    def update_todo_count(self, count):
        self.todo_toggle.setText(f"交付待办 · {count} 项")

    def restore_expanded(self, expanded):
        blocker = QtCore.QSignalBlocker(self.todo_toggle)
        self.todo_toggle.setChecked(bool(expanded))
        del blocker
        if expanded:
            self._toggle_board(True, persist=False)

    def _toggle_board(self, expanded, *, persist=True):
        try:
            if expanded:
                self.plugin.sync_delivery_todos()
                if self.board_widget is None:
                    self.board_widget = DeliveryBoardWidget(
                        self.plugin.todo_store, self.plugin.sync_delivery_todos,
                        self, compact=True,
                    )
                    self.board_widget.changed.connect(
                        lambda: self.plugin.refresh_delivery_views(self.board_widget)
                    )
                    self.layout().addWidget(self.board_widget)
                self.board_widget.render()
            if self.board_widget is not None:
                self.board_widget.setVisible(expanded)
            self.todo_toggle.setArrowType(
                QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow
            )
            if persist:
                config = self.plugin.context.load_config()
                if not self.plugin.context.patch_config(
                    {BOARD_EXPANDED_CONFIG_KEY: bool(expanded)}, expected=config,
                ):
                    self.plugin.context.log("四象限展开状态未保存；本次操作仍然有效。", logging.WARNING)
        except Exception as error:
            self.plugin.context.log(f"四象限展开失败：{error}", logging.ERROR)
            if expanded and self.board_widget is None:
                self.restore_expanded(False)
            QtWidgets.QMessageBox.warning(self, "交付待办", str(error))


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
        self.daily_quantity_folder_thread = None
        self.daily_quantity_date_thread = None
        self.daily_quantity_inspection_thread = None
        self.daily_quantity_dialog_scope = None
        self.todo_store = None
        self.delivery_board_dialog = None

    def register(self, context):
        self.context = context
        from .task_delivery_workflow import register
        register(context)
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
                "按日期和管理员汇总待发送视频，并显示任务表与审核待办",
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
        self.quick_actions.restore_expanded(
            self.context.load_config().get(BOARD_EXPANDED_CONFIG_KEY) is True
        )
        return self.quick_actions

    def organize_results(self):
        return self.controller.organize_results()

    def open_daily_links(self):
        return self.controller.open_daily_links()

    def open_review_status(self):
        return self.controller.open_review_status()

    def open_delivery_inbox(self):
        try:
            self.sync_delivery_todos()
            if self.delivery_board_dialog is None:
                self.delivery_board_dialog = DeliveryBoardDialog(
                    self.todo_store, self.sync_delivery_todos, self.context.parent_widget,
                )
                board = self.delivery_board_dialog.board_widget
                board.changed.connect(lambda: self.refresh_delivery_views(board))
            self.delivery_board_dialog.render()
            self.delivery_board_dialog.show()
            self.delivery_board_dialog.raise_()
            self.delivery_board_dialog.activateWindow()
            return self.delivery_board_dialog
        except Exception as error:
            self.context.log(f"交付待办无法打开：{error}")
            QtWidgets.QMessageBox.warning(self.context.parent_widget, "交付待办", str(error))
            return None

    def sync_delivery_todos(self, snapshot=None):
        """Import new evidence, never auto-complete or discard an existing task."""
        if self.todo_store is None:
            self.todo_store = DeliveryTodoStore()
        reviews = (snapshot if snapshot is not None else review_history_snapshot()).get("all", [])
        config = self.context.load_config()
        records = load_video_upload_history(config).get("records", [])
        sources = collect_delivery_todos(
            records, reviews, self.controller.daily_link_history,
            review_folder_name=str(config.get("review_folder_name") or "review"),
        )
        added = self.todo_store.add_sources(sources)
        self.todo_store.mark_old_review_versions(reviews)
        self.todo_store.archive_completed()
        # Existing admin/day cards can gain videos or new review notes without
        # inserting a card. Refresh on source sync, not only on insert counts.
        self.refresh_delivery_views()
        return added

    def refresh_delivery_views(self, source=None):
        """Refresh visible views and the collapsed badge from one shared store."""
        if self.todo_store is None:
            return
        if self.quick_actions is not None:
            self.quick_actions.update_todo_count(len(self.todo_store.list_tasks("open")))
            board = self.quick_actions.board_widget
            if board is not None and board is not source and board.isVisible():
                board.render()
        dialog = self.delivery_board_dialog
        if dialog is not None and dialog.board_widget is not source and dialog.isVisible():
            dialog.render()

    def open_gemini_keys(self):
        # Compatibility entry point; the plugin no longer owns credentials.
        return self.context.open_gemini_key_manager()

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
        config = self.context.load_config()
        root = self.context.parent_widget.task_path_edit.text().strip()
        previous = self.daily_quantity_dialog_scope
        changed_scope = previous and (previous[0].get("daily_quantity_sheet_url"), previous[1]) != (
            config.get("daily_quantity_sheet_url"), root)
        if changed_scope and self.daily_quantity_dialog is not None:
            if not self._save_daily_quantity_external_edits(*previous):
                self.daily_quantity_dialog.show()
                return self.daily_quantity_dialog
            self.daily_quantity_dialog.show_external_records([])
            self.daily_quantity_dialog.show_inspection({})
            self.daily_quantity_dialog.show_external_sources([])
        self.daily_quantity_dialog_scope = (dict(config), root)
        if self.daily_quantity_dialog is None:
            dialog = DailyQuantityDialog(self.context.parent_widget)
            dialog.refresh_requested.connect(lambda: self.refresh_daily_quantity(manual=True))
            dialog.scan_requested.connect(self.scan_daily_quantity_folder)
            dialog.scan_date_requested.connect(self.scan_daily_quantity_date)
            dialog.view_date_requested.connect(self.view_daily_quantity_date)
            dialog.review_check_requested.connect(
                lambda: self.controller.request_review_status_check(manual=True)
            )
            dialog.edit_sheet_requested.connect(self.edit_daily_quantity_sheet)
            dialog.edit_categories_requested.connect(self.edit_daily_quantity_categories)
            dialog.inspect_requested.connect(self.inspect_daily_quantity)
            dialog.view_pending_requested.connect(self.view_daily_quantity_pending)
            dialog.ignore_requested.connect(self.ignore_daily_quantity_records)
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
        self._load_daily_quantity_progress()
        self.daily_quantity_dialog.show()
        self.daily_quantity_dialog.raise_()
        self.daily_quantity_dialog.activateWindow()
        self.load_daily_quantity_categories()
        self.daily_quantity_dialog._pending_view = None
        self.daily_quantity_dialog.tabs.setCurrentIndex(self.daily_quantity_dialog.pending_tab_index)
        self.inspect_daily_quantity()
        return self.daily_quantity_dialog

    def inspect_daily_quantity(self):
        dialog = self.daily_quantity_dialog
        if dialog is None or any(thread_is_active(thread) for thread in (
            self.daily_quantity_thread, self.daily_quantity_folder_thread,
            self.daily_quantity_date_thread, self.daily_quantity_inspection_thread,
        )):
            return False
        config = self.context.load_config()
        root = self.context.parent_widget.task_path_edit.text().strip()
        from pathlib import Path
        if not Path(root).is_dir() or not str(config.get("daily_quantity_sheet_url") or "").strip():
            dialog.inspection_status.setText("请先选择任务根目录并设置每日数量表格链接，再重新检索。")
            return False
        if not self._save_daily_quantity_external_edits(config, root):
            return False
        thread = DailyQuantityInspectionThread(config, root, self.context.parent_widget)
        self.daily_quantity_inspection_thread = thread
        thread.completed.connect(self._daily_quantity_inspection_completed)
        thread.failed.connect(self._daily_quantity_inspection_failed)
        connect_thread_finished(thread, self, 'daily_quantity_inspection_thread', self._daily_quantity_inspection_finished)
        dialog.set_inspection_busy(True)
        thread.start()
        return True

    def _daily_quantity_inspection_completed(self, result):
        dialog = self.daily_quantity_dialog
        if dialog is None:
            return
        thread = self.daily_quantity_inspection_thread
        if thread is not None and (thread.root != self.context.parent_widget.task_path_edit.text().strip()
                or thread.config.get("daily_quantity_sheet_url") != self.context.load_config().get("daily_quantity_sheet_url")):
            dialog.inspection_status.setText("任务目录或统计表已改变，请重新检索；未使用旧范围的结果。")
            return
        view = dialog._pending_view
        dialog.show_inspection(result)
        if view is not None:
            dialog.show_pending_records(*view)
        else:
            dialog.show_external_records(result["records"], day=dialog.folder_day.date().toString("yyyy-MM-dd"))
        self.context.log(
            f"每日数量清单检索（只读）：已检查 {result['checked']} 个视频，"
            f"待分类 {len(result['pending'])} 个，已忽略 {len(result['ignored'])} 个；"
            "数量写入结果请看“每日数量统计”日志。"
        )

    def _daily_quantity_inspection_failed(self, error):
        self.context.log(f"每日数量检索失败：{error}")
        if self.daily_quantity_dialog is not None:
            self.daily_quantity_dialog.inspection_status.setText(f"检索失败：{error}；原有记录未清空。")

    def _daily_quantity_inspection_finished(self):
        thread = self.daily_quantity_inspection_thread
        self.daily_quantity_inspection_thread = None
        if self.daily_quantity_dialog is not None:
            self.daily_quantity_dialog.set_inspection_busy(False)
        if thread is not None:
            thread.deleteLater()

    def view_daily_quantity_pending(self, day, ignored=False):
        dialog = self.daily_quantity_dialog
        if dialog is None or self.daily_quantity_inspection_thread is not None:
            return False
        config = self.context.load_config()
        root = self.context.parent_widget.task_path_edit.text().strip()
        had_edits = bool(dialog.changed_external_edits())
        if not self._save_daily_quantity_external_edits(config, root):
            return False
        dialog._pending_view = (day, ignored)
        if had_edits:
            return self.inspect_daily_quantity()
        dialog.show_pending_records(day, ignored)
        return True

    def ignore_daily_quantity_records(self, ids, ignored=True):
        dialog = self.daily_quantity_dialog
        if dialog is None or self.daily_quantity_inspection_thread is not None:
            return False
        config = self.context.load_config()
        root = self.context.parent_widget.task_path_edit.text().strip()
        if not self._save_daily_quantity_external_edits(config, root):
            return False
        try:
            count = set_daily_quantity_ignored(config, root, ids, ignored)
        except (OSError, ValueError) as exc:
            dialog.show_error(str(exc))
            return False
        dialog.folder_status.setText(f"已{'忽略' if ignored else '恢复'} {count} 个视频并保存。刷新数量后同步云端。")
        self.refresh_daily_quantity_review_queue()
        return self.inspect_daily_quantity()

    def _load_daily_quantity_progress(self):
        if self.daily_quantity_dialog is None:
            return
        try:
            progress = daily_quantity_sync_progress(
                self.context.load_config(), self.context.parent_widget.task_path_edit.text().strip(),
            )
        except ValueError:
            progress = {}
        self.daily_quantity_dialog.show_sync_progress(progress)

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
        self._load_daily_quantity_progress()

    def load_daily_quantity_categories(self):
        dialog = self.daily_quantity_dialog
        if dialog is None:
            return False
        try:
            dialog.show_category_options(CategoryStore(self.context.load_config()).initialize())
            return True
        except (ValueError, OSError) as exc:
            dialog.show_category_error(str(exc))
            self.context.log("每日数量本地类别加载失败：" + str(exc))
            return False

    def edit_daily_quantity_categories(self):
        try:
            editor = CategoryEditorDialog(CategoryStore(self.context.load_config()), self.daily_quantity_dialog)
        except (ValueError, OSError) as exc:
            self.daily_quantity_dialog.show_category_error(str(exc))
            return False
        if editor.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.load_daily_quantity_categories()
            self.context.log("每日数量固定类别已保存；历史视频分类未改动。")
            return True
        return False

    def refresh_daily_quantity(self, *, manual=False):
        if self.daily_quantity_inspection_thread is not None:
            return False
        if self.daily_quantity_thread is not None and thread_is_active(self.daily_quantity_thread):
            return False
        if self.daily_quantity_folder_thread is not None and thread_is_active(self.daily_quantity_folder_thread):
            return False
        if self.daily_quantity_date_thread is not None and thread_is_active(self.daily_quantity_date_thread):
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
        thread = DailyQuantityThread(config, root, self.context.parent_widget, manual=manual)
        self.daily_quantity_thread = thread
        thread.completed.connect(self._daily_quantity_completed)
        thread.failed.connect(self._daily_quantity_failed)
        connect_thread_finished(thread, self, 'daily_quantity_thread', self._daily_quantity_finished)
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
        if self.daily_quantity_inspection_thread is not None:
            return False
        if self.daily_quantity_folder_thread is not None and thread_is_active(self.daily_quantity_folder_thread):
            return False
        if self.daily_quantity_thread is not None and thread_is_active(self.daily_quantity_thread):
            return False
        if self.daily_quantity_date_thread is not None and thread_is_active(self.daily_quantity_date_thread):
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
        connect_thread_finished(thread, self, 'daily_quantity_folder_thread', self._daily_quantity_folder_finished)
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
        self.inspect_daily_quantity()

    def view_daily_quantity_date(self, day):
        if self.daily_quantity_inspection_thread is not None:
            return False
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
        if any(thread_is_active(thread) for thread in (
            self.daily_quantity_thread,
            self.daily_quantity_folder_thread,
            self.daily_quantity_date_thread,
            self.daily_quantity_inspection_thread,
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
        connect_thread_finished(thread, self, 'daily_quantity_date_thread', self._daily_quantity_date_finished)
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
        self.inspect_daily_quantity()

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
        retired = result.get("retired_periods", [])
        if retired:
            self.context.log(f"每日数量：正常略过 {len(retired)} 组已移除的旧日期/时段列；"
                             "历史记录保留，不重建旧列，不影响当前统计。")
        duplicates = result.get("duplicate_exclusions", [])
        if duplicates:
            self.context.log(f"每日数量：按内容去重，排除 {len(duplicates)} 个重复副本；不增加数量。")
            for item in duplicates[:12]:
                self.context.log(f"每日数量重复排除：{item['file_name']}（{item['date']}）；"
                                 f"保留 {item['retained_date']} 首次交付；{item['reason']}。")
            if len(duplicates) > 12:
                self.context.log("每日数量：完整重复排除清单请在统计窗口的设置与记录中查看。")
        for item in result.get("automatic_classifications", []):
            self.context.log(
                f"每日数量：{item['file_name']}（{item['duration_millis'] / 1000:.3f} 秒）"
                f"统计类别自动调整：{item['from']} → {item['to']}；本地表格未改动。"
            )
        if warnings:
            # Log bounded, distinct write blockers, not just a warning count.
            blockers = list(dict.fromkeys(text for text in warnings if any(marker in text for marker in (
                "在本人区块匹配", "时段列匹配", "未来", "已有手填数字", "外部修改", "统计分页不存在",
            ))))
            for text in blockers[:12]:
                self.context.log("每日数量未写入原因：" + text)
            self.context.notify(
                "每日数量统计待核对",
                f"有 {len(warnings)} 条分类、时长或表格待核对提示；在任务交付 → 每日数量统计中查看并刷新。",
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
        self.inspect_daily_quantity()

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
        try:
            batch_date, batch_slot = get_upload_batch({})
            thread = self.context.submit_background_task(
                title=f"简易上传 · {name}", runner=run_quick_upload,
                payload={"sources": sources, "name": name, "parent_folder": parent_folder,
                         "batch_date": batch_date.isoformat(), "batch_slot": batch_slot},
                kind="network", resources=(UPLOAD_RESOURCE,), retryable=False)
        except Exception as error:
            logging.getLogger(__name__).exception("无法提交简易上传后台任务")
            self.context.log(f"简易上传未启动：{type(error).__name__}: {error}", logging.ERROR)
            self.context.notify("简易上传未启动", str(error), critical=True)
            return False
        self.quick_upload_thread = thread
        thread.progressChanged.connect(self._on_background_quick_upload_progress)
        thread.completed.connect(self._on_quick_upload_completed)
        thread.failed.connect(self._on_quick_upload_failed)
        thread.finished.connect(self._on_quick_upload_finished)
        dialog = self.quick_upload_dialog
        dialog.link_edit.clear()
        dialog.copy_button.setEnabled(False)
        dialog.status_label.setText("已加入后台队列，正在等待前一批上传…")
        dialog.progress_bar.setRange(0, 0)
        dialog.set_busy(True)
        self.context.log(f"简易上传开始：{name}（{len(sources)} 个来源）")
        return True

    def _on_background_quick_upload_progress(self, message, percent):
        if self.quick_upload_dialog is not None:
            bar = self.quick_upload_dialog.progress_bar
            bar.setRange(0, 100 if percent is not None else 0)
            if percent is not None:
                bar.setValue(percent)
            self.quick_upload_dialog.status_label.setText(message)

    def _on_quick_upload_progress(self, done, total, label):
        if self.quick_upload_dialog is not None:
            self.quick_upload_dialog.update_progress(done, total, label)

    def _on_quick_upload_completed(self, result):
        if self.quick_upload_dialog is not None:
            self.quick_upload_dialog.show_result(result, show_warning=False)
            self.quick_upload_dialog.progress_bar.setRange(0, 100)
            self.quick_upload_dialog.progress_bar.setValue(100)
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

    def _on_quick_upload_finished(self):
        self.quick_upload_thread = None
        # Host-owned task handle outlives this plugin window.

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
        if self.daily_quantity_inspection_thread is not None and thread_is_active(self.daily_quantity_inspection_thread):
            return False, "每日数量正在检索已有记录，请稍等。"
        if self.daily_quantity_date_thread is not None and thread_is_active(self.daily_quantity_date_thread):
            return False, "每日数量日期目录正在扫描，请稍等。"
        if self.daily_quantity_folder_thread is not None and thread_is_active(self.daily_quantity_folder_thread):
            return False, "每日数量文件夹正在扫描，请稍等。"
        if self.daily_quantity_thread is not None and thread_is_active(self.daily_quantity_thread):
            return False, "每日数量统计正在更新，请稍等。"
        if self.quick_upload_thread is not None and self.quick_upload_thread.isRunning():
            return False, "简易上传仍在进行，请等待上传完成。"
        return self.controller.can_close()

    def stop(self):
        return self.controller.stop()
