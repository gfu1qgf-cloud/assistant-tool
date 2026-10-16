"""One settings entry for the Life Assistant; existing config keys stay intact."""
from qt_compat import QtWidgets

from app_plugins.builtin.cooking_assistant.settings import CookingSettingsPage
from .settings import WasteSettingsPage


class LifeSettingsPage:
    def __init__(self, context, parent=None):
        self.widget = QtWidgets.QWidget(parent)
        layout = QtWidgets.QVBoxLayout(self.widget)
        self.tabs = QtWidgets.QTabWidget(self.widget)
        layout.addWidget(self.tabs)
        self.waste = WasteSettingsPage(self.tabs)
        self.cooking = CookingSettingsPage(context, self.tabs)
        self.tabs.addTab(self.waste.widget, "垃圾与天气")
        self.tabs.addTab(self.cooking.widget, "做饭与食材库存")
        self.load_config(context.load_config())

    def load_config(self, config):
        self.waste.load_config(config)
        self.cooking.load_config(config)

    def validate(self):
        self.cooking.validate()

    def update_config(self, config):
        self.waste.update_config(config)
        self.cooking.update_config(config)
        return config
