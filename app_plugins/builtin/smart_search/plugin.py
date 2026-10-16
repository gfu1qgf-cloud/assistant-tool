from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage
from ..smart_image_search.plugin import SmartImageSearchPlugin
from ..smart_music_search.plugin import SmartMusicSearchPlugin
from .settings import CONFIG_KEY, SmartSearchSettingsPage, normalize_video_settings


class SmartSearchPlugin:
    plugin_id = 'smart_search'
    display_name = '智能搜索'
    version = '2.0'
    required_api_version = 1

    def __init__(self):
        # Internal controllers, not independently installed host plugins.
        self.image = SmartImageSearchPlugin()
        self.music = SmartMusicSearchPlugin()
        self.context = self.dialog = self.video = None
        self.video_settings = normalize_video_settings()

    def register(self, context):
        self.context = self.image.context = self.music.context = context
        self.apply_settings(context.load_config())
        context.register_command(PluginCommand('open', '智能搜索…', lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), tooltip='搜索本地图片、音乐和视频画面', order=14))
        context.register_settings_page(PluginSettingsPage('settings', '智能搜索', SmartSearchSettingsPage, order=216))

    def start(self):
        # Keep startup light: no scans, databases or models until a tab is used.
        return None

    def open_dialog(self, tab=0):
        if self.dialog is None:
            from .ui import SmartSearchDialog
            self.dialog = SmartSearchDialog(self, self.context.parent_widget)
        self.dialog.ensure_tab(tab)
        self.dialog.showNormal(); self.dialog.raise_(); self.dialog.activateWindow()
        image = self.image.dialog
        if tab == 0 and image is not None and not image.is_busy():
            if image.index.count(image.encoder.model_id) and image.encoder.model_is_cached():
                from qt_compat import QtCore
                QtCore.QTimer.singleShot(0, image.update_index)
        return self.dialog

    def apply_settings(self, config):
        self.image.apply_settings(config); self.music.apply_settings(config)
        self.video_settings = normalize_video_settings(config.get(CONFIG_KEY))
        if self.video is not None:
            self.video.update_settings(self.video_settings)
        return True

    def update_config(self, config):
        self.image.update_config(config); self.music.update_config(config)
        config[CONFIG_KEY] = normalize_video_settings(self.video_settings)
        return config

    def save_video_settings(self, settings):
        self.video_settings = normalize_video_settings(settings)
        try:
            if not self.context.save_config():
                self.context.log('视频搜索设置未能保存；本次仍可使用。')
        except Exception as error:
            self.context.log(f'保存视频搜索设置失败：{error}')

    def can_close(self):
        active = [page for page in (self.image.dialog, self.music.dialog, self.video)
                  if page is not None and page.is_busy()]
        for page in active:
            page.cancel_work()
        return (False, '智能搜索正在停止任务，请稍候退出。') if active else (True, '')

    def stop(self):
        pages = [page for page in (self.image.dialog, self.music.dialog, self.video) if page is not None]
        if any(page.is_busy() for page in pages):
            for page in pages:
                if page.is_busy():
                    page.cancel_work()
            raise RuntimeError('智能搜索任务尚未结束，不能释放模型和窗口。')
        for page in pages:
            if not page.shutdown():
                raise RuntimeError('智能搜索尚未完成安全退出。')
        if self.dialog is not None:
            self.dialog.hide()
