import os
import unittest
from unittest.mock import patch

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

    def test_menu_entry_persists_keys_and_rolls_back_failed_save(self):
        context = FakeContext()
        plugin = TaskDeliveryPlugin()
        context.plugin = plugin
        plugin.register(context)
        plugin.controller.gemini_api_keys = ["existing"]

        with patch("app_plugins.builtin.task_delivery.GeminiKeysDialog") as dialog:
            dialog.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
            dialog.return_value.keys.return_value = ["existing", "new"]
            self.assertTrue(plugin.open_gemini_keys())
        self.assertEqual(context.saved["gemini_api_keys"], ["existing", "new"])

        context.save_success = False
        with patch("app_plugins.builtin.task_delivery.GeminiKeysDialog") as dialog:
            dialog.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Accepted
            dialog.return_value.keys.return_value = ["failed"]
            self.assertFalse(plugin.open_gemini_keys())
        self.assertEqual(plugin.controller.gemini_api_keys, ["existing", "new"])


if __name__ == "__main__":
    unittest.main()
