import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qt_compat import QtCore, QtWidgets

from model.InventoryManager import InventoryStore
from PYUI.utility_managers_pyui import InventoryManagerDialog


class InventoryUpdateSortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_stock_edit_time_is_not_changed_by_other_items_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InventoryStore(Path(directory) / "inventory.json")
            base = time.time()
            with patch("model.InventoryManager.time.time", return_value=base - 120):
                old = store.add_item("旧库存", 10, 1)
            with patch("model.InventoryManager.time.time", return_value=base - 60):
                newer = store.add_item("新库存", 10, 1)
            with patch("model.InventoryManager.time.time", return_value=base):
                store.add_stock(newer["id"], 2)
            items = {item["id"]: item for item in store.list_items()}
            self.assertEqual(items[old["id"]]["modified_at"], base - 120)
            self.assertEqual(items[old["id"]]["updated_at"], base)
            self.assertEqual(items[newer["id"]]["modified_at"], base)

    def test_inventory_material_and_people_tables_sort_and_keep_user_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = InventoryStore(root / "inventory.json", root / "library")
            base = time.time()
            with patch("model.InventoryManager.time.time", return_value=base - 120):
                store.add_item("A 库存", 10, 1)
            with patch("model.InventoryManager.time.time", return_value=base):
                store.add_item("B 库存", 10, 1)
            source = root / "sample.txt"
            source.write_text("sample", encoding="utf-8")
            first_material = store.add_material("A 素材", [source])
            second_material = store.add_material("B 素材", [source])
            first_person = store.add_person("A 人物")
            second_person = store.add_person("B 人物")
            state = store.load()
            dates = {
                first_material["id"]: "2026-01-01T10:00:00+01:00",
                second_material["id"]: "2026-02-01T10:00:00+01:00",
                first_person["id"]: "2026-01-01T10:00:00+01:00",
                second_person["id"]: "2026-02-01T10:00:00+01:00",
            }
            for entry in state["materials"] + state["people"]:
                entry["updated_at"] = dates[entry["id"]]
            store._write(state)

            dialog = InventoryManagerDialog(store)
            try:
                tables = (
                    (dialog.table, dialog.refresh, 6, "B 库存", "A 库存"),
                    (dialog.material_table, dialog.refresh_materials, 6, "B 素材", "A 素材"),
                    (dialog.people_table, dialog.refresh_people, 5, "B 人物", "A 人物"),
                )
                for table, refresh, updated_column, newest, oldest in tables:
                    self.assertEqual(
                        table.horizontalHeaderItem(updated_column).text(), "更新时间"
                    )
                    self.assertEqual(table.item(0, 0).text(), newest)
                    table.sortItems(0, QtCore.Qt.SortOrder.AscendingOrder)
                    refresh()
                    self.assertEqual(table.item(0, 0).text(), oldest)
            finally:
                dialog.close()

    def test_monitored_material_download_updates_existing_record_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.txt"
            source.write_text("sample", encoding="utf-8")
            store = InventoryStore(root / "inventory.json", root / "library")
            material = store.add_material("监视素材", [source])
            with patch("model.InventoryManager.datetime") as clock:
                clock.now.return_value.astimezone.return_value.isoformat.return_value = (
                    "2026-09-24T12:34:56+02:00"
                )
                store.touch_material(material["id"])
            updated = store.list_materials()[0]
            self.assertEqual(updated["updated_at"], "2026-09-24T12:34:56+02:00")
            self.assertEqual(updated["created_at"], material["created_at"])


if __name__ == "__main__":
    unittest.main()
