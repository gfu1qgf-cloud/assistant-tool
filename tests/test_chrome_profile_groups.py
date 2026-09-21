import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QListWidgetItem

from PYUI.chrome_runner_pyui import ChromeRunnerDialog


class ChromeProfileGroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def create_dialog(self, config_path):
        with patch.object(ChromeRunnerDialog, "load_profiles", lambda _self: None):
            dialog = ChromeRunnerDialog(config_path=str(config_path))
        dialog.chrome_path = "chrome.exe"
        dialog.profiles = [
            {"name": "Alpha", "directory": "Profile 1", "path": "A"},
            {"name": "Beta", "directory": "Profile 2", "path": "B"},
            {"name": "Gamma", "directory": "Profile 3", "path": "C"},
        ]
        dialog.chrome_list_widget.clear()
        for profile in dialog.profiles:
            item = QListWidgetItem(profile["name"])
            item.setData(Qt.UserRole, profile["directory"])
            dialog.chrome_list_widget.addItem(item)
        dialog.restore_iterator_position()
        dialog.update_iterator_status()
        return dialog

    def test_group_iterator_uses_visible_order_and_restores_next_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            dialog.profile_groups = [{
                "id": "group-a",
                "name": "工作组",
                # Deliberately different from the visible profile order.
                "profile_directories": ["Profile 3", "Profile 1"],
            }]
            dialog.selected_group_id = "group-a"
            self.assertTrue(dialog.save_profile_groups())
            dialog.populate_profile_group_combo()
            dialog.launch_profile = MagicMock(return_value=(True, None))

            first = dialog.start_group_iterator()

            self.assertEqual(first["directory"], "Profile 1")
            self.assertEqual(
                dialog.iterator_profile_directories,
                ["Profile 1", "Profile 3"],
            )
            dialog.launch_profile.assert_called_once()

            restored = self.create_dialog(config_path)
            restored.launch_profile = MagicMock(return_value=(True, None))
            second = restored.launch_next_profile()

            self.assertEqual(second["directory"], "Profile 3")
            self.assertEqual(restored.active_iterator_group_id, "group-a")
            self.assertEqual(restored.selected_group_id, "group-a")
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["chrome_profile_groups"]["groups"][0]["name"],
                "工作组",
            )

    def test_manual_iterator_clears_group_source_without_deleting_group(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            dialog.profile_groups = [{
                "id": "group-a",
                "name": "工作组",
                "profile_directories": ["Profile 1", "Profile 3"],
                "future_metadata": {"color": "blue"},
            }]
            dialog.selected_group_id = "group-a"
            dialog.active_iterator_group_id = "group-a"
            dialog.iterator_profile_directories = ["Profile 1", "Profile 3"]
            dialog.populate_profile_group_combo()
            dialog.chrome_list_widget.clearSelection()
            dialog.chrome_list_widget.item(1).setSelected(True)
            dialog.launch_profile = MagicMock(return_value=(True, None))

            launched = dialog.start_selected_iterator()

            self.assertEqual(launched["directory"], "Profile 2")
            self.assertIsNone(dialog.active_iterator_group_id)
            self.assertEqual(dialog.profile_groups[0]["future_metadata"], {"color": "blue"})

    def test_each_group_keeps_its_own_next_profile_across_switch_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            dialog.profile_groups = [
                {
                    "id": "group-a",
                    "name": "A 组",
                    "profile_directories": ["Profile 1", "Profile 3"],
                    "next_profile_directory": "Profile 1",
                },
                {
                    "id": "group-b",
                    "name": "B 组",
                    "profile_directories": ["Profile 2", "Profile 3"],
                    "next_profile_directory": "Profile 2",
                },
            ]
            dialog.launch_profile = MagicMock(return_value=(True, None))

            dialog.selected_group_id = "group-a"
            self.assertEqual(
                dialog.start_group_iterator()["directory"], "Profile 1"
            )
            dialog.selected_group_id = "group-b"
            self.assertEqual(
                dialog.start_group_iterator()["directory"], "Profile 2"
            )
            dialog.selected_group_id = "group-a"
            self.assertEqual(
                dialog.start_group_iterator()["directory"], "Profile 3"
            )

            restored = self.create_dialog(config_path)
            restored.launch_profile = MagicMock(return_value=(True, None))
            restored.selected_group_id = "group-b"
            self.assertEqual(
                restored.start_group_iterator()["directory"], "Profile 3"
            )
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            groups = {
                group["id"]: group
                for group in saved["chrome_profile_groups"]["groups"]
            }
            self.assertEqual(
                groups["group-a"]["next_profile_directory"], "Profile 1"
            )
            self.assertEqual(
                groups["group-b"]["next_profile_directory"], "Profile 2"
            )
            self.assertEqual(saved["chrome_profile_groups"]["version"], 3)

    def test_member_update_preserves_or_repairs_saved_group_position(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            group = {
                "id": "group-a",
                "name": "可编辑组",
                "profile_directories": ["Profile 1", "Profile 3"],
                "next_profile_directory": "Profile 3",
            }
            dialog.profile_groups = [group]
            dialog.selected_group_id = "group-a"

            self.assertTrue(dialog.update_profile_group_members(
                group, ["Profile 2", "Profile 3"]
            ))
            self.assertEqual(
                group["profile_directories"], ["Profile 2", "Profile 3"]
            )
            self.assertEqual(group["next_profile_directory"], "Profile 3")

            self.assertTrue(dialog.update_profile_group_members(
                group, ["Profile 2"]
            ))
            self.assertEqual(group["next_profile_directory"], "Profile 2")
            self.assertEqual(
                dialog.save_profile_group_members_btn.text(), "管理成员 / 子分组…"
            )

    def test_nested_groups_expand_in_group_order_and_deduplicate_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            dialog.profile_groups = [
                {
                    "id": "group-a",
                    "name": "A 组",
                    "profile_directories": ["Profile 2", "Profile 3"],
                    "child_group_ids": [],
                },
                {
                    "id": "group-b",
                    "name": "B 组",
                    "profile_directories": ["Profile 1", "Profile 2"],
                    "child_group_ids": [],
                },
                {
                    "id": "combo",
                    "name": "组合组",
                    "profile_directories": [],
                    "child_group_ids": ["group-a", "group-b"],
                    "next_profile_directory": "Profile 2",
                },
            ]
            dialog.selected_group_id = "combo"
            self.assertEqual(
                dialog.get_group_profile_directories("combo"),
                ["Profile 2", "Profile 3", "Profile 1"],
            )
            dialog.launch_profile = MagicMock(return_value=(True, None))

            first = dialog.start_group_iterator()
            second = dialog.launch_next_profile()

            self.assertEqual(first["directory"], "Profile 2")
            self.assertEqual(second["directory"], "Profile 3")
            self.assertEqual(
                dialog.iterator_profile_directories,
                ["Profile 2", "Profile 3", "Profile 1"],
            )

            restored = self.create_dialog(config_path)
            restored.launch_profile = MagicMock(return_value=(True, None))
            third = restored.launch_next_profile()
            self.assertEqual(third["directory"], "Profile 1")
            self.assertEqual(restored.active_iterator_group_id, "combo")

    def test_nested_group_can_contain_another_nested_group(self):
        with tempfile.TemporaryDirectory() as directory:
            dialog = self.create_dialog(Path(directory) / "config.json")
            dialog.profile_groups = [
                {
                    "id": "leaf",
                    "name": "叶子组",
                    "profile_directories": ["Profile 1", "Profile 2"],
                    "child_group_ids": [],
                },
                {
                    "id": "middle",
                    "name": "中间组",
                    "profile_directories": ["Profile 3"],
                    "child_group_ids": ["leaf"],
                },
                {
                    "id": "top",
                    "name": "总组",
                    "profile_directories": [],
                    "child_group_ids": ["middle"],
                },
            ]

            self.assertEqual(
                dialog.get_group_profile_directories("top"),
                ["Profile 3", "Profile 1", "Profile 2"],
            )
            self.assertTrue(dialog.group_contains_group("top", "leaf"))

    def test_cycle_is_rejected_and_loaded_cycle_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            dialog = self.create_dialog(config_path)
            group_a = {
                "id": "group-a",
                "name": "A 组",
                "profile_directories": ["Profile 1"],
                "child_group_ids": ["group-b"],
            }
            group_b = {
                "id": "group-b",
                "name": "B 组",
                "profile_directories": ["Profile 2"],
                "child_group_ids": [],
            }
            dialog.profile_groups = [group_a, group_b]

            self.assertFalse(dialog.update_profile_group_members(
                group_b, ["Profile 2"], ["group-a"]
            ))
            self.assertEqual(group_b["child_group_ids"], [])

            config_path.write_text(json.dumps({
                "chrome_profile_groups": {
                    "version": 3,
                    "groups": [
                        dict(group_a, child_group_ids=["group-b"]),
                        dict(group_b, child_group_ids=["group-a"]),
                    ],
                },
            }, ensure_ascii=False), encoding="utf-8")
            restored = self.create_dialog(config_path)
            groups = {group["id"]: group for group in restored.profile_groups}
            self.assertEqual(groups["group-a"]["child_group_ids"], ["group-b"])
            self.assertEqual(groups["group-b"]["child_group_ids"], [])


if __name__ == "__main__":
    unittest.main()
