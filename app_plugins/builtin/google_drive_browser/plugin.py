from app_plugins.api import MAIN_MENU, PluginCommand


class GoogleDriveBrowserPlugin:
    plugin_id = 'google_drive_browser'
    display_name = '谷歌网盘浏览器'
    version = '1.0'
    required_api_version = 1

    def __init__(self): self.context, self.dialog = None, None

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand('open', '谷歌网盘浏览器…', lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), order=19, tooltip='完整比例缩略图、离线收藏目录与图片／视频预览；云端只读'))

    def open_dialog(self):
        if self.dialog is None:
            from .ui import DriveBrowserDialog
            self.dialog = DriveBrowserDialog(self.context, self.context.parent_widget)
        self.dialog.show(); self.dialog.raise_(); self.dialog.activateWindow()
        return self.dialog

    def start(self): pass
    def apply_settings(self, _config): return True
    def can_close(self): return True, ''
    def stop(self):
        if self.dialog is not None: self.dialog.shutdown()
