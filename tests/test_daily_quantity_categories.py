import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtWidgets
from model.DailyQuantityCategories import CategoryStore, DEFAULT_CATEGORIES, validate_categories
from app_plugins.builtin.task_delivery_category_editor import CategoryEditorDialog
from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog
from app_plugins.builtin.task_delivery import TaskDeliveryPlugin


class CategoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {"daily_quantity_categories_file": str(Path(self.temp.name) / "categories.json")}
        self.store = CategoryStore(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_has_all_template_categories_and_is_independent_copy(self):
        categories, revision = self.store.load()
        self.assertIsNone(revision)
        self.assertEqual(sum(map(len, categories.values())), 29)
        categories.clear()
        self.assertEqual(len(self.store.load()[0]), 4)
        self.assertFalse(self.store.path.exists())

    def test_first_initialize_is_persistent_then_explicit_deletion_survives(self):
        self.store.initialize()
        _, revision = self.store.load()
        self.store.save({}, revision)
        self.assertEqual(CategoryStore(self.config).initialize(), {})
        self.assertTrue(self.store.path.with_name(self.store.path.name + ".bak").exists())

    def test_editing_does_not_merge_seed_back_and_preserves_multiline(self):
        options = {"测试": ["第一行\n第二行"]}
        self.store.save(options, None)
        self.assertEqual(self.store.initialize(), options)
        self.store.save({"另一个分页": ["新类别"]}, self.store.load()[1])
        self.assertEqual(self.store.load()[0], {"另一个分页": ["新类别"]})
        self.assertEqual(json.loads(self.store.path.with_name(self.store.path.name + ".bak").read_text("utf-8"))["categories"], options)

    def test_stale_editor_cannot_overwrite_newer_edit(self):
        self.store.initialize()
        revision = self.store.load()[1]
        self.store.save({"新": ["更新"]}, revision)
        with self.assertRaisesRegex(ValueError, "其他窗口"):
            self.store.save({"旧": ["旧值"]}, revision)
        self.assertEqual(self.store.load()[0], {"新": ["更新"]})

    def test_corrupt_catalog_never_silently_resets(self):
        # Test fixture only: malformed input, not an application write path.
        with self.store.path.open("w", encoding="utf-8") as handle:
            handle.write("{broken")
        with self.assertRaisesRegex(ValueError, "未清空或覆盖"):
            self.store.initialize()
        self.assertEqual(self.store.path.read_text("utf-8"), "{broken")

    def test_duplicate_blank_or_nonlist_names_are_rejected(self):
        for invalid in ({"x": ["a", " A "]}, {"": []}, {"x": [""]}, {"x": "wrong"}, []):
            with self.assertRaises(ValueError):
                validate_categories(invalid)

    def test_editor_add_rename_remove_and_save_cancel(self):
        self.store.save({"分页": ["原类别"]}, None)
        dialog = CategoryEditorDialog(self.store)
        try:
            parent = dialog.tree.topLevelItem(0)
            self.assertTrue(dialog._insert("新类别", parent))
            self.assertFalse(dialog._insert("新类别", parent))
            dialog.tree.setCurrentItem(parent.child(1))
            with patch.object(QtWidgets.QInputDialog, "getMultiLineText", return_value=("修改\n类别", True)):
                dialog.rename()
            self.assertEqual(dialog.categories(), {"分页": ["原类别", "修改\n类别"]})
            with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                dialog.remove()
            dialog.save()
            self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
            self.assertEqual(self.store.load()[0], {"分页": ["原类别"]})
            cancelled = CategoryEditorDialog(self.store)
            cancelled._insert("不保存")
            cancelled.reject()
            self.assertEqual(self.store.load()[0], {"分页": ["原类别"]})
            cancelled.deleteLater()
        finally:
            dialog.deleteLater()

    def test_plugin_loads_catalog_without_sheet_credentials(self):
        plugin = TaskDeliveryPlugin()
        plugin.context = Mock()
        plugin.context.load_config.return_value = self.config
        plugin.daily_quantity_dialog = DailyQuantityDialog()
        try:
            with patch("model.DailyQuantityStats.load_sheets_service", side_effect=AssertionError("must stay offline")):
                self.assertTrue(plugin.load_daily_quantity_categories())
            self.assertEqual(plugin.daily_quantity_dialog._category_options, DEFAULT_CATEGORIES)
            self.assertTrue(self.store.path.exists())
        finally:
            plugin.daily_quantity_dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
