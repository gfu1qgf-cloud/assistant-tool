from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage

from .settings import CONFIG_KEY, SmartMusicSearchSettingsPage, normalize_settings


class SmartMusicSearchPlugin:
    plugin_id = "smart_music_search"
    display_name = "智能搜音乐"
    version = "1.1"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.settings = normalize_settings()
        self.dialog = None

    def register(self, context):
        self.context = context
        config = context.load_config()
        self.settings = self._settings_from_config(config)
        context.register_command(PluginCommand(
            command_id="open", title="智能搜音乐…",
            callback=lambda _rows: self.open_dialog(),
            locations=frozenset({MAIN_MENU}),
            tooltip="搜索本地配乐库，按视频时长筛选和试听",
            order=15,
        ))
        context.register_settings_page(PluginSettingsPage(
            page_id="settings", title="智能搜音乐",
            factory=SmartMusicSearchSettingsPage, order=217,
        ))

    def start(self):
        return None

    def open_dialog(self):
        if self.dialog is None:
            from .ui import SmartMusicSearchDialog
            self.dialog = SmartMusicSearchDialog(self.settings, self.context.parent_widget)
            self.dialog.settingsChanged.connect(self._save_search_settings)
        else:
            self.dialog.update_settings(self.settings)
        self.dialog.show()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def _save_search_settings(self, settings):
        self.settings = normalize_settings(settings)
        try:
            if not self.context.save_config():
                self.context.log("检索选项未能保存；当前会话仍可使用，请检查配置文件。")
        except Exception as error:
            self.context.log(f"保存音乐检索选项失败：{error}")

    def apply_settings(self, config):
        self.settings = self._settings_from_config(config)
        if self.dialog is not None:
            self.dialog.update_settings(self.settings)
        return True

    @staticmethod
    def _settings_from_config(config):
        settings = normalize_settings(config.get(CONFIG_KEY))
        if not settings["ffmpeg_path"]:
            settings["ffmpeg_path"] = str(config.get("shana_ffmpeg_path") or "")
        return settings

    def update_config(self, config):
        config[CONFIG_KEY] = normalize_settings(self.settings)
        return config

    def can_close(self):
        if self.dialog is not None and self.dialog.is_busy():
            self.dialog.cancel_work()
            return False, "智能搜音乐正在停止后台任务，请稍候再退出。"
        return True, ""

    def stop(self):
        if self.dialog is not None:
            self.dialog.cancel_work()
