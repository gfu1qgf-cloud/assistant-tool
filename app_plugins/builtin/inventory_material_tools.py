"""Internal inventory tools, not independently registered host plugins.

Keep the established engines, data paths and config keys. The inventory plugin
owns their settings, windows, saving and shutdown; models stay lazy-loaded.
"""
import logging

from qt_compat import QtWidgets
from app_plugins.api import PluginSettingsPage
from .material_organizer.plugin import MaterialOrganizerPlugin
from .material_organizer.settings import MaterialOrganizerSettingsPage
from .image_classifier.plugin import ImageClassifierPlugin
from .image_classifier.settings import ImageClassifierSettingsPage


class InventoryMaterialTools:
    def __init__(self):
        self.context = None
        self.organizer = MaterialOrganizerPlugin()
        self.classifier = ImageClassifierPlugin()

    def bind_context(self, context):
        self.context = context
        # Do not call register(): no separate plugin/command ownership remains.
        self.organizer.bind_context(context)
        self.classifier.bind_context(context)
        context.register_settings_page(PluginSettingsPage(
            "material_organizer", "素材整理", MaterialOrganizerSettingsPage, order=210))
        context.register_settings_page(PluginSettingsPage(
            "image_classifier", "素材智能分类", ImageClassifierSettingsPage, order=215))

    def mount_menu(self, dialog):
        bar = QtWidgets.QMenuBar(dialog)
        bar.setNativeMenuBar(False)
        menu = bar.addMenu("素材工具")
        menu.setObjectName("inventory_material_tools")
        action = menu.addAction("素材整理…")
        action.setObjectName("inventory_open_material_organizer")
        action.setToolTip("按视频运动分类到虚拟目录，源文件不移动")
        action.triggered.connect(self.open_organizer)
        action = menu.addAction("图片/视频素材分类…")
        action.setObjectName("inventory_open_image_classifier")
        action.setToolTip("使用 CLIP 分类；人工核对后才复制或移动")
        action.triggered.connect(self.open_classifier)
        dialog.layout().setMenuBar(bar)
        return bar

    def _open(self, callback):
        try:
            return callback()
        except (Exception, SystemExit) as error:
            self.context.log(f"素材工具打开失败：{type(error).__name__}: {error}", level=logging.ERROR)
            self.context.notify("素材工具打开失败", "详细信息已写入主界面日志。", critical=True)

    def open_organizer(self, *_args):
        return self._open(self.organizer.open_manager)

    def open_classifier(self, *_args):
        return self._open(self.classifier.open_dialog)

    def apply_settings(self, config):
        self.organizer.apply_settings(config)
        self.classifier.apply_settings(config)

    def update_config(self, config):
        self.organizer.update_config(config)
        self.classifier.update_config(config)

    def can_close(self):
        for tool in (self.organizer, self.classifier):
            allowed, message = tool.can_close()
            if not allowed:
                return False, message
        return True, ""

    def stop(self):
        # Request both workers to stop, even when the first is still active.
        results = [tool.stop() for tool in (self.organizer, self.classifier)]
        if any(result is False for result in results):
            return False
        for tool in (self.organizer, self.classifier):
            if tool.dialog is not None:
                tool.dialog.hide()
        return True
