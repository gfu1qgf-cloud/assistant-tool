from qt_compat import QtCore, QtWidgets

from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage

from .analyzer import (
    MATERIAL_ORGANIZER_CONFIG_KEY,
    normalize_material_organizer_settings,
)
from .settings import MaterialOrganizerSettingsPage
from .store import MaterialCatalogStore
from .ui import MaterialOrganizerDialog


class MaterialOrganizerPlugin:
    plugin_id = "material_organizer"
    display_name = "素材整理"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.settings = normalize_material_organizer_settings({})
        self.store = None
        self.dialog = None

    def register(self, context):
        self.context = context
        config = context.load_config()
        self.settings = normalize_material_organizer_settings(
            config.get(MATERIAL_ORGANIZER_CONFIG_KEY)
        )
        context.register_command(PluginCommand(
            command_id="open",
            title="素材整理…",
            callback=lambda _rows: self.open_manager(),
            locations=frozenset({MAIN_MENU}),
            tooltip="用虚拟目录和视频缩略图整理素材；源文件永不移动",
            order=12,
        ))
        context.register_settings_page(PluginSettingsPage(
            page_id="settings",
            title="素材整理",
            factory=MaterialOrganizerSettingsPage,
            order=210,
        ))

    def start(self):
        # Keep startup lightweight.  The isolated catalog is opened lazily
        # only when the user enters this plugin.
        return None

    def open_manager(self):
        if self.store is None:
            self.store = MaterialCatalogStore()
        if self.dialog is None:
            self.dialog = MaterialOrganizerDialog(
                self.store,
                self.settings,
                logger=self.context.log,
                parent=self.context.parent_widget,
            )
        else:
            self.dialog.update_settings(self.settings)
            self.dialog.refresh_categories()
            self.dialog.refresh_assets()
        self.dialog.show()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def apply_settings(self, config):
        self.settings = normalize_material_organizer_settings(
            config.get(MATERIAL_ORGANIZER_CONFIG_KEY)
        )
        if self.dialog is not None:
            self.dialog.update_settings(self.settings)
        return True

    def update_config(self, config):
        config[MATERIAL_ORGANIZER_CONFIG_KEY] = (
            normalize_material_organizer_settings(self.settings)
        )
        return config

    def can_close(self):
        if self.dialog is None or self.dialog.can_close():
            return True, ""
        return (
            False,
            "已请求停止素材分析。当前视频结束后即可关闭程序；源视频不会被改变。",
        )

    def stop(self):
        if (
            self.dialog is not None
            and self.dialog.worker is not None
            and self.dialog.worker.isRunning()
        ):
            self.dialog.worker.requestInterruption()
