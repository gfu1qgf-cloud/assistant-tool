import ast
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from app_plugins.api import MAIN_MENU
from app_plugins.builtin.inventory import InventoryPlugin
from app_plugins.builtin.inventory_material_tools import InventoryMaterialTools
from app_plugins.builtin.material_organizer.store import MaterialCatalogStore
from app_plugins.host import PluginHost
from model.InventoryManager import InventoryStore


class Window(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.config = {
            "material_organizer_settings": {"sample_fps": 1.25, "analysis_width": 640},
            "image_classifier_settings": {"model": "base", "minimum_similarity": .31},
            "unrelated": {"keep": [1, 2, 3]},
        }
        self.appendLog = Mock()
        self.showDesktopNotification = Mock()
        self.saveCurrentConfig = Mock(return_value=True)

    def load_config(self):
        return deepcopy(self.config)


class InventoryMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.window = Window()
        self.host = PluginHost(self.window)
        self.plugin = self.host.install(InventoryPlugin())

    def tearDown(self):
        self.plugin.stop()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_host_has_one_owner_and_no_standalone_classification_commands(self):
        self.assertIsInstance(self.plugin.material_tools, InventoryMaterialTools)
        self.assertEqual([entry["plugin_id"] for entry in self.host.plugin_entries()], ["inventory"])
        commands = self.host._sorted_commands(MAIN_MENU)
        self.assertTrue(all(owner == "inventory" for _full, owner, _command in commands))
        self.assertEqual([command.title for _full, _owner, command in commands],
                         ["库存与素材管理器", "监听剪贴板中的 Google 链接"])
        self.assertIs(self.plugin.material_tools.organizer.context, self.plugin.context)
        self.assertIs(self.plugin.material_tools.classifier.context, self.plugin.context)
        self.assertIsNone(self.plugin.material_tools.organizer.store)
        self.assertIsNone(self.plugin.material_tools.classifier.dialog)

    def test_main_assembly_installs_inventory_only_and_keeps_legacy_aliases(self):
        root = Path(os.environ.get("LZX_TEST_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
        source = (root / "PYUI/main_pyui.py").read_text(encoding="utf-8-sig")
        installed = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "install":
                if node.args and isinstance(node.args[0], ast.Call) and isinstance(node.args[0].func, ast.Name):
                    installed.append(node.args[0].func.id)
        self.assertEqual(installed.count("InventoryPlugin"), 1)
        self.assertNotIn("MaterialOrganizerPlugin", installed)
        self.assertNotIn("ImageClassifierPlugin", installed)
        self.assertIn("DaVinciRemotePlugin", installed)
        self.assertIn("self.inventory_plugin.material_tools.classifier", source)
        self.assertIn("self.inventory_plugin.material_tools.organizer", source)

    def test_legacy_config_keys_and_runtime_changes_save_through_inventory(self):
        config = deepcopy(self.window.config)
        self.host.update_runtime_config(config)
        self.assertEqual(config["unrelated"], self.window.config["unrelated"])
        self.assertEqual(config["material_organizer_settings"]["sample_fps"], 1.25)
        self.assertEqual(config["image_classifier_settings"]["model"], "base")
        tools = self.plugin.material_tools
        updated = {**config, "material_organizer_settings": {"sample_fps": 3.5},
                   "image_classifier_settings": {"model": "large", "minimum_similarity": .42}}
        tools.apply_settings(updated)
        tools.classifier._runtime_settings_changed({"model": "base", "minimum_similarity": .41})
        tools.update_config(config)
        self.assertEqual(config["material_organizer_settings"]["sample_fps"], 3.5)
        self.assertEqual(config["image_classifier_settings"]["minimum_similarity"], .41)
        self.window.saveCurrentConfig.assert_called_once()

    def test_settings_pages_belong_to_inventory_and_disabled_inventory_hides_all(self):
        page_ids = {full_id for full_id in self.host._settings_pages}
        self.assertIn("inventory.material_organizer", page_ids)
        self.assertIn("inventory.image_classifier", page_ids)
        self.assertFalse(any(key.startswith(("material_organizer.", "image_classifier.")) for key in page_ids))
        self.host._enabled_states["inventory"] = False
        self.assertEqual(self.host._sorted_commands(MAIN_MENU), [])

    def test_inventory_window_mounts_tools_and_opens_original_views_without_models(self):
        tools = self.plugin.material_tools
        with tempfile.TemporaryDirectory() as folder:
            tools.organizer.store = MaterialCatalogStore(Path(folder) / "catalog.sqlite3", Path(folder) / "thumbs")
            self.plugin.store = InventoryStore(Path(folder) / "inventory.json", Path(folder) / "materials")
            with patch("app_plugins.builtin.image_classifier.classifier.ImageClassifierEngine",
                       side_effect=AssertionError("no model loading from menus")), patch.object(self.plugin, "_bring_to_front"):
                self.plugin.open_manager()
                dialog = self.plugin.dialog
                menu = dialog.layout().menuBar().actions()[0].menu()
                self.assertEqual(menu.title(), "素材工具")
                self.assertEqual([action.text() for action in menu.actions()], ["素材整理…", "图片/视频素材分类…"])
                for action in menu.actions():
                    action.trigger()
                self.app.processEvents()
                self.assertTrue(tools.organizer.dialog.isVisible())
                self.assertTrue(tools.classifier.dialog.isVisible())
                self.assertIsNone(tools.organizer.dialog.worker)
                self.assertIsNone(tools.classifier.dialog.worker)
                tools.organizer.dialog.hide()
                tools.classifier.dialog.hide()
                tools.organizer.dialog.deleteLater()
                tools.classifier.dialog.deleteLater()
                tools.organizer.dialog = tools.classifier.dialog = None
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_inventory_cannot_exit_or_destroy_views_while_child_job_is_busy(self):
        tools = self.plugin.material_tools
        with patch.object(tools.classifier, "can_close", return_value=(False, "still running")):
            self.assertEqual(self.plugin.can_close(), (False, "still running"))
        with patch.object(tools.organizer, "stop", return_value=False) as stop_a, \
                patch.object(tools.classifier, "stop", return_value=True) as stop_b:
            self.assertFalse(self.plugin.stop())
            stop_a.assert_called_once()
            stop_b.assert_called_once()

    def test_menu_errors_are_logged_not_unhandled_qt_callbacks(self):
        tools = self.plugin.material_tools
        with patch.object(tools.organizer, "open_manager", side_effect=RuntimeError("test error")):
            self.assertIsNone(tools.open_organizer())
        self.window.appendLog.assert_called()
        self.window.showDesktopNotification.assert_called_once()

    def test_previous_database_paths_are_unchanged(self):
        from app_plugins.builtin.material_organizer.store import DEFAULT_MATERIAL_ORGANIZER_DB
        self.assertEqual(DEFAULT_MATERIAL_ORGANIZER_DB.name, "catalog.sqlite3")
        self.assertEqual(DEFAULT_MATERIAL_ORGANIZER_DB.parent.name, "MaterialOrganizer")

    def test_catalog_closes_connections_and_rolls_back_failed_transactions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            store = MaterialCatalogStore(path, Path(folder) / "thumbs")
            with self.assertRaises(RuntimeError):
                with store._connect() as connection:
                    connection.execute("CREATE TABLE uncommitted_test (value TEXT)")
                    connection.execute("INSERT INTO uncommitted_test VALUES ('rollback')")
                    raise RuntimeError("rollback")
            with store._connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM uncommitted_test").fetchone()[0], 0)
            # Windows rejects renaming a SQLite file while a handle is open.
            moved = path.with_name("closed.sqlite3")
            path.rename(moved)
            moved.rename(path)
