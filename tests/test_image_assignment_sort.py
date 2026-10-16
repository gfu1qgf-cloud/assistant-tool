import os
import unittest
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from PYUI.utility_managers_pyui import MaterialGroupAssignmentDialog
from app_plugins.builtin.smart_image_assignment.ui import GroupPicker
from model.InventoryManager import InventoryStore


class ImageAssignmentSortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def groups(self):
        return [{"source_kind": "material", "source_id": name, "name": name,
                 "updated_at": date, "images": [{"path": name + ".png"}]} for name, date in (
                     ("A", "2026-01-01T10:00:00+01:00"), ("B", "2026-10-01T10:00:00+02:00"))]

    def test_dates_default_newest_sort_preserves_checks_filter_and_manual_order(self):
        dialog = MaterialGroupAssignmentDialog(self.groups(), [{"label": "task", "target_dir": "target"}])
        try:
            self.assertEqual(dialog.group_table.horizontalHeaderItem(4).text(), "更新日期")
            self.assertEqual(dialog.group_table.item(0, 2).text(), "B")
            dialog.group_table.item(0, 0).setCheckState(QtCore.Qt.Checked)
            dialog.sort_groups(2)
            self.assertEqual(dialog.group_table.item(0, 2).text(), "A")
            self.assertEqual(dialog.selected_groups()[0]["name"], "B")
            dialog.filter_edit.setText("B")
            self.assertTrue(dialog.group_table.isRowHidden(0))
            self.assertFalse(dialog.group_table.isRowHidden(1))
            dialog.filter_edit.clear()
            dialog.group_table.selectRow(1)
            dialog.move_current_group(-1)
            self.assertEqual(dialog.group_table.item(0, 2).text(), "B")
            self.assertEqual(dialog.selected_images()[0]["path"], "B.png")
        finally:
            dialog.close()

    def test_smart_picker_inherits_date_sort_and_model_exposes_both_sources_dates(self):
        dialog = GroupPicker(self.groups(), [])
        try:
            self.assertEqual(dialog.group_table.item(0, 2).text(), "B")
        finally:
            dialog.close()
        state = {"materials": [{"id": "m", "name": "m", "updated_at": "2026-01-02"}],
                 "people": [{"id": "p", "name": "p", "created_at": "2026-01-01"}]}
        groups = InventoryStore._image_groups_from_state(state)
        self.assertEqual([group["updated_at"] for group in groups], ["2026-01-02", "2026-01-01"])


if __name__ == "__main__": unittest.main()
