from qt_compat import QtWidgets

from app_plugins.api import MAIN_MENU, PluginCommand, PluginSettingsPage

from .classifier import (
    IMAGE_CLASSIFIER_CONFIG_KEY,
    normalize_image_classifier_settings,
)
from .settings import ImageClassifierSettingsPage
from .ui import ImageClassifierDialog


class ImageClassifierPlugin:
    plugin_id = "image_classifier"
    display_name = "图片智能分类"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.settings = normalize_image_classifier_settings({})
        self.dialog = None

    def register(self, context):
        self.context = context
        self.settings = normalize_image_classifier_settings(
            context.load_config().get(IMAGE_CLASSIFIER_CONFIG_KEY)
        )
        context.register_command(PluginCommand(
            command_id="open",
            title="图片智能分类…",
            callback=lambda _rows: self.open_dialog(),
            locations=frozenset({MAIN_MENU}),
            tooltip="用 CLIP 将图片分类；先预览核对，再复制或移动",
            order=13,
        ))
        context.register_settings_page(PluginSettingsPage(
            page_id="settings",
            title="图片智能分类",
            factory=ImageClassifierSettingsPage,
            order=215,
        ))

    def start(self):
        # Model imports and weights are intentionally deferred until the user
        # starts analysis, keeping application startup unchanged.
        return None

    def open_dialog(self):
        if self.dialog is None:
            self.dialog = ImageClassifierDialog(
                self.settings,
                logger=self.context.log,
                notifier=lambda title, message, critical=False: (
                    self.context.notify(title, message, critical=critical)
                ),
                parent=self.context.parent_widget,
            )
            self.dialog.settingsChanged.connect(self._runtime_settings_changed)
        else:
            self.dialog.update_settings(self.settings)
        self.dialog.show()
        self.dialog.showNormal()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def _runtime_settings_changed(self, settings):
        self.settings = normalize_image_classifier_settings(settings)
        try:
            self.context.save_config()
        except Exception as error:
            self.context.log(f"保存图片分类最近路径失败：{error}")

    def apply_settings(self, config):
        self.settings = normalize_image_classifier_settings(
            config.get(IMAGE_CLASSIFIER_CONFIG_KEY)
        )
        if self.dialog is not None:
            self.dialog.update_settings(self.settings)
        return True

    def update_config(self, config):
        config[IMAGE_CLASSIFIER_CONFIG_KEY] = (
            normalize_image_classifier_settings(self.settings)
        )
        return config

    def can_close(self):
        if self.dialog is None or self.dialog.can_close():
            return True, ""
        return False, "图片智能分类仍在后台运行，请先停止任务。"

    def stop(self):
        if self.dialog is not None:
            self.dialog.stop_current_work()

