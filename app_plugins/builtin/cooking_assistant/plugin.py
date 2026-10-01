import logging
from pathlib import Path

from qt_compat import QtCore
from app_paths import APP_ROOT
from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage
from .settings import CONFIG_KEY, CookingSettingsPage, normalize_settings
from .store import FoodStore, stock_status, today


class CookingAssistantPlugin:
    plugin_id = "cooking_assistant"
    display_name = "做饭小助手"
    version = "1.0"
    required_api_version = 1

    def __init__(self, data_root=None):
        self.root = Path(data_root) if data_root else Path(APP_ROOT)
        self.context = self.dialog = self.timer = None
        self.settings = normalize_settings()
        self.store = FoodStore(self.root / "CookingAssistant" / "food.sqlite3")
        self._reported_error = None

    def register(self, context):
        self.context = context
        self.settings = normalize_settings(context.load_config().get(CONFIG_KEY))
        context.register_command(PluginCommand("open", "做饭小助手…", lambda _rows: self.open_dialog(),
            frozenset({MAIN_MENU}), tooltip="食材库存、临期提醒、两顿份量的中文做饭建议", order=20))
        context.register_settings_page(PluginSettingsPage("settings", "做饭小助手",
            lambda parent=None: CookingSettingsPage(context, parent), order=220))

    def start(self):
        if self.timer is None:
            self.timer = QtCore.QTimer(self.context.parent_widget)
            self.timer.setInterval(600000)
            self.timer.timeout.connect(self.tick)
        self.timer.start()
        QtCore.QTimer.singleShot(5000, self.tick)

    def tick(self):
        if self.timer is None or not self.timer.isActive():
            return
        try:
            if self.dialog is not None and self.dialog.isVisible():
                self.dialog.refresh_stock()
            if not self.settings["notify"]:
                return
            items = [item for item in self.store.items() if stock_status(item, warn_days=self.settings["warn_days"])[0]
                     in {"soon", "overdue", "spoiled"}]
            if not items:
                return
            signature = [today().isoformat(), sorted((item["id"], item["due"], item["spoiled"]) for item in items)]
            # JSON uses lists, so compare the canonical persisted representation.
            import json
            signature = json.loads(json.dumps(signature))
            if signature == self.store.get("last_notice"):
                return
            names = "、".join(item["name"] for item in items[:8])
            self.context.notify("食材需要留意", f"{names}。打开“插件 → 做饭小助手”查看；先检查状态再食用。")
            self.store.put("last_notice", signature)
            self._reported_error = None
        except Exception as error:
            message = f"食材库存检查失败：{type(error).__name__}: {error}"
            if message != self._reported_error:
                self._reported_error = message
                self.context.log(message, logging.ERROR)

    def open_dialog(self):
        try:
            if self.dialog is None:
                from .ui import CookingDialog
                self.dialog = CookingDialog(self, self.context.parent_widget)
            self.dialog.refresh_stock()
            self.dialog.show()
            self.dialog.raise_()
            self.dialog.activateWindow()
            return self.dialog
        except Exception as error:
            self.context.log(f"打开做饭小助手失败：{type(error).__name__}: {error}", logging.ERROR)
            raise

    def apply_settings(self, config):
        self.settings = normalize_settings(config.get(CONFIG_KEY))
        if self.dialog is not None:
            self.dialog.refresh_stock()
        return True

    def can_close(self):
        if self.dialog is not None and self.dialog.is_busy():
            return False, "做饭小助手正在生成菜谱，请稍后再退出。"
        return True, ""

    def stop(self):
        if self.timer is not None:
            self.timer.stop()
        if self.dialog is not None:
            self.dialog.close()
