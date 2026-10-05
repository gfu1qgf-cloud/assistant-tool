from app_plugins.api import TOOLS_MENU, PluginCommand


class VideoStitchPlugin:
    plugin_id = "video_stitch"
    display_name = "视频拼接"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.dialog = None

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand(
            command_id="open", title="视频拼接…", callback=lambda _rows: self.open_dialog(),
            locations=frozenset({TOOLS_MENU}), order=45,
            tooltip="原尺寸左右拼接；选择时长基准，另一侧自动循环或截断，源视频不改动",
        ))

    def open_dialog(self):
        if self.dialog is None:
            from .ui import VideoStitchDialog
            self.dialog = VideoStitchDialog(self.context, self.context.parent_widget)
        self.dialog._close_after = False
        self.dialog.show()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def start(self):
        pass

    def apply_settings(self, _config):
        return True

    def can_close(self):
        if self.dialog is not None and self.dialog.is_busy():
            self.dialog.cancel_work()
            return False, "视频拼接正在取消，请稍候再退出；源视频不会改变。"
        return True, ""

    def stop(self):
        if self.dialog is not None:
            self.dialog.close()
