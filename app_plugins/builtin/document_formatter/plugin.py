from app_plugins.api import MAIN_MENU, PluginCommand


class DocumentFormatterPlugin:
    plugin_id = 'document_formatter'
    display_name = '文档自动格式化'
    version = '1.0'
    required_api_version = 1

    def __init__(self): self.context, self.dialog, self.stopping = None, None, False

    def register(self, context):
        self.context = context
        context.register_command(PluginCommand('open', '文档自动格式化…', lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), order=18, tooltip='按页面、缩进、字号和行距规范处理ODT或Google Docs；不改写原文'))

    def open_dialog(self):
        if self.dialog is None:
            from .ui import DocumentFormatterDialog
            self.dialog = DocumentFormatterDialog(self.context, self.context.parent_widget)
        self.dialog.show(); self.dialog.raise_(); self.dialog.activateWindow()
        return self.dialog

    def start(self): self.stopping = False
    def apply_settings(self, _config): return True
    def can_close(self): return True, ''
    def stop(self):
        self.stopping = True
        if self.dialog is not None: self.dialog.stopping = True; self.dialog.close()
