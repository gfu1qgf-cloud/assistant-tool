from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage
from qt_compat import QtCore

from .settings import CONFIG_KEY, SmartImageSearchSettingsPage, normalize_settings


class SmartImageSearchPlugin:
    plugin_id = "smart_image_search"
    display_name = "智能搜图"
    version = "1.1"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.settings = normalize_settings()
        self.dialog = None

    def register(self, context):
        self.context = context
        self.settings = normalize_settings(context.load_config().get(CONFIG_KEY))
        context.register_command(PluginCommand(
            command_id="open",
            title="智能搜图…",
            callback=lambda _rows: self.open_dialog(),
            locations=frozenset({MAIN_MENU}),
            tooltip="用中文描述或参考图片搜索库存、人物素材和指定图片库",
            order=14,
        ))
        context.register_settings_page(PluginSettingsPage(
            page_id="settings",
            title="智能搜图",
            factory=SmartImageSearchSettingsPage,
            order=216,
        ))

    def start(self):
        # No model, database or inventory scan at application startup.
        return None

    def open_dialog(self):
        if self.dialog is None:
            from .ui import SmartImageSearchDialog
            self.dialog = SmartImageSearchDialog(
                self.settings, parent=self.context.parent_widget
            )
            self.dialog.settingsChanged.connect(self._save_search_settings)
        else:
            self.dialog.update_settings(self.settings)
        self.dialog.show()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        # Once the first index exists, opening the plugin quietly catches up
        # with images added or removed since the previous session.
        if (self.dialog.index.count(self.dialog.encoder.model_id)
                and self.dialog.encoder.model_is_cached()
                and not self.dialog.is_busy()):
            QtCore.QTimer.singleShot(0, self.dialog.update_index)
        return self.dialog

    def _save_search_settings(self, settings):
        self.settings = normalize_settings(settings)
        try:
            if not self.context.save_config():
                self.context.log("搜图选项未能保存；本次仍可使用，请检查配置文件。")
        except Exception as error:
            self.context.log(f"保存搜图选项失败：{error}")

    def apply_settings(self, config):
        self.settings = normalize_settings(config.get(CONFIG_KEY))
        if self.dialog is not None:
            self.dialog.update_settings(self.settings)
        return True

    def update_config(self, config):
        config[CONFIG_KEY] = normalize_settings(self.settings)
        return config

    def can_close(self):
        if self.dialog is not None and self.dialog.is_busy():
            self.dialog.cancel_work()
            return False, "智能搜图正在停止后台任务，请稍候再退出。"
        return True, ""

    def stop(self):
        if self.dialog is not None:
            self.dialog.cancel_work()
