from app_paths import DATA_ROOT as APP_ROOT
from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage
from .settings import ClientSettingsPage
from model.QtThreadLifecycle import thread_is_active


class AIModelClientPlugin:
    plugin_id = "ai_model_client"
    display_name = "AI 模型试用台"
    version = "1.3"
    required_api_version = 1

    def __init__(self):
        self.context = self.dialog = self.store = None

    def register(self, context):
        self.context = context
        from .nodes import register_nodes
        register_nodes(context)
        context.register_command(PluginCommand("open", "AI 模型试用台…", lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), order=21, tooltip="测试 Gemini / Mistral / OpenRouter / Cloudflare / Groq，保留本地对话，支持图片"))
        context.register_settings_page(PluginSettingsPage("settings", "AI 模型试用台",
            lambda parent=None: ClientSettingsPage(context, parent), order=221))

    def open_dialog(self):
        if self.dialog is None:
            from .store import ChatStore
            from .ui import AIClientDialog
            self.store = ChatStore(APP_ROOT / "AIModelClient")
            self.dialog = AIClientDialog(self.context, self.store, self.context.parent_widget)
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
        if self.dialog is not None and thread_is_active(self.dialog.handle):
            self.dialog.handle.requestInterruption()
            return False, "AI 试用台正在安全结束请求，请稍候再退出（网络读取超时最多60秒）。"
        return True, ""

    def stop(self):
        allowed, _message = self.can_close()
        if not allowed:
            return False
        if self.dialog is not None:
            self.dialog.close()
        return True
