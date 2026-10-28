from app_plugins.api import MAIN_MENU, TASK_CONTEXT_MENU, PluginCommand


class ScriptSegmenterPlugin:
    plugin_id = "script_segmenter"
    display_name = "智能文案分段助手"
    version = "1.1.1"
    required_api_version = 1

    def __init__(self):
        self.context = self.dialog = None
        self.quick_copy = None

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand(
            "open", "智能文案分段助手…", lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), order=17,
            tooltip="优先接近10秒，在停顿标点或完整词之间分段，不改写原文"))
        context.register_command(PluginCommand(
            "split_tasks", "智能文案分段…", self.split_tasks,
            frozenset({TASK_CONTEXT_MENU}), order=32,
            tooltip="直接提取所选任务语音文案，支持多选批量分段"))
        context.register_command(PluginCommand(
            "copy_segments", "复制分段文案", self.copy_segments,
            frozenset({TASK_CONTEXT_MENU}), order=33, enabled=lambda rows: bool(rows),
            tooltip="按已保存的分段参数直接复制，含段号和建议秒数；支持多选，不打开对话框"))

    def open_dialog(self):
        if self.dialog is None:
            from .ui import ScriptSegmenterDialog
            self.dialog = ScriptSegmenterDialog(self.context, self.context.parent_widget)
        self.dialog.show()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def split_tasks(self, rows):
        dialog = self.open_dialog()
        dialog.load_tasks(self.context.task_targets(rows), generate=True)
        return dialog

    def copy_segments(self, rows):
        if self.quick_copy is None:
            from .quick_copy import QuickCopyController
            self.quick_copy = QuickCopyController(self.context)
        self.quick_copy.copy_tasks(rows)

    def start(self):
        pass

    def apply_settings(self, _config):
        return True

    def can_close(self):
        return True, ""

    def stop(self):
        if self.quick_copy is not None:
            self.quick_copy.stop()
        if self.dialog is not None:
            self.dialog.copy_guard.cancel()
            self.dialog.close()
