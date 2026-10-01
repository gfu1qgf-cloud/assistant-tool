import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtWidgets

from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
from app_plugins.builtin.task_delivery_gemini import GeminiKeysDialog


class FakeContext:
    parent_widget = None

    def __init__(self):
        self.plugin = None
        self.saved = None
        self.save_success = True

    def register_command(self, _command):
        pass

    def register_main_widget(self, _widget):
        pass

    def save_config(self):
        self.saved = self.plugin.update_config({})
        return self.save_success

    def log(self, _message):
        pass

    def open_gemini_key_manager(self):
        return "main-key-manager"


class GeminiKeysDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_add_deduplicate_remove_and_save_pending_key(self):
        dialog = GeminiKeysDialog(["old-secret"])
        self.assertEqual(dialog.keys(), ["old-secret"])
        self.assertNotIn("old-secret", dialog.key_list.item(0).text())
        dialog.key_edit.setText("old-secret; new-secret")
        dialog.add_keys()
        self.assertEqual(dialog.keys(), ["old-secret", "new-secret"])
        dialog.key_list.item(0).setSelected(True)
        dialog.remove_selected()
        dialog.key_edit.setText("third-secret")
        dialog.accept()
        self.assertEqual(dialog.keys(), ["new-secret", "third-secret"])

    def test_plugin_forwards_management_and_never_overwrites_shared_keys(self):
        context = FakeContext()
        plugin = TaskDeliveryPlugin()
        context.plugin = plugin
        plugin.register(context)
        self.assertEqual(plugin.open_gemini_keys(), "main-key-manager")
        plugin.controller.gemini_api_keys = ["stale-old-plugin-copy"]
        result = plugin.update_config({"gemini_api_keys": ["main-owned"]})
        self.assertEqual(result["gemini_api_keys"], ["main-owned"])


if __name__ == "__main__":
    unittest.main()
