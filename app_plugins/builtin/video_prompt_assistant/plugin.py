from app_plugins.api import MAIN_MENU, PluginCommand


class VideoPromptAssistantPlugin:
    plugin_id = "video_prompt_assistant"
    display_name = "视频提示词助手"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.dialog = None

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand(
            command_id="open", title="视频提示词助手…",
            callback=lambda _rows: self.open_dialog(),
            locations=frozenset({MAIN_MENU}), order=16,
            tooltip="拖入图片获取提示词方案，保存成功案例",
        ))

    def start(self):
        pass

    def open_dialog(self):
        if self.dialog is None:
            from .ui import VideoPromptDialog
            self.dialog = VideoPromptDialog(self.context, self.context.parent_widget)
        self.dialog.show()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def apply_settings(self, _config):
        return True

    def can_close(self):
        if self.dialog is not None and self.dialog.is_busy():
            return False, "视频提示词助手正在分析图片，请稍候再退出。"
        return True, ""

    def stop(self):
        if self.dialog is not None:
            self.dialog.close()
