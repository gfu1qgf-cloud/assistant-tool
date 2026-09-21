"""Task-result delivery entry points and host-controlled quick actions."""

from qt_compat import QtWidgets

from app_plugins.api import MAIN_MENU, PluginCommand, PluginMainWidget
from app_plugins.builtin.task_delivery_controller import TaskDeliveryController


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
        return self.controller.can_close()

    def stop(self):
        self.controller.stop()
