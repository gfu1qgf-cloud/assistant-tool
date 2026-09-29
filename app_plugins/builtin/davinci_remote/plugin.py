"""One plugin for the existing DaVinci remote tab and its tools."""

from app_plugins.api import MAIN_MENU, PluginCommand, PluginTabPage
from .ui import DaVinciRemotePanel


class DaVinciRemotePlugin:
    plugin_id = "davinci_remote"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.panel = None
        self.dialog = None
        self.settings = {}

    def register(self, context):
        self.context = context
        raw = context.load_config().get("davinci_remote_settings", {})
        self.settings = raw if isinstance(raw, dict) else {}
        context.register_command(PluginCommand(
            command_id="open",
            title="达芬奇遥控器",
            callback=lambda _rows: self.open_dialog(),
            locations=frozenset({MAIN_MENU}),
            order=45,
        ))
        context.register_tab_page(PluginTabPage(
            page_id="remote",
            title="达芬奇遥控器",
            factory=self.create_panel,
            order=10,
        ))

    def create_panel(self, parent=None):
        self.panel = DaVinciRemotePanel(self, parent)
        return self.panel

    def open_dialog(self, page=0, probe=False):
        from .ui import DaVinciRemoteDialog

        created = self.dialog is None
        if self.dialog is None:
            self.dialog = DaVinciRemoteDialog(self, self.context.parent_widget)
        self.dialog.tabs.setCurrentIndex(page)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        if probe or created:
            self.dialog.probe()

    def update_config(self, config):
        config["davinci_remote_settings"] = dict(self.settings)

    def save_settings(self, section, settings):
        saved = dict(settings)
        if section == "export":
            # The task name must be derived from the *current* timeline next run.
            saved.pop("task_name", None)
        self.settings[section] = saved
        self.context.save_config()

    def report_status(self, status):
        if self.panel is not None:
            self.panel.status.setText(status)

    def can_close(self):
        dialog = self.dialog
        if dialog is not None and dialog.process is not None:
            from qt_compat import QtCore

            if dialog.process.state() != QtCore.QProcess.ProcessState.NotRunning:
                return False, "达芬奇正在处理时间线，请等待完成后再退出程序。"
        return True, ""
