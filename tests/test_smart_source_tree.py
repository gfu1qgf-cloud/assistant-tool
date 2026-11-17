"""Directory selection must not lose nested clips or change analysis inputs."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.smart_video_editor.ui import SmartVideoSourceDialog


class SourceDirectoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)/"访谈节目"
        self.paths = [self.root/"采访"/"第二机位"/"001.mp4", self.root/"开场.mp4",
                      self.root/"补充画面"/"001.mp4", self.root/"采访"/"第一机位"/"002.mp4",
                      self.root/"采访"/"第一机位"/"001.mp4"]
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"selection-only video fixture")
        self.jobs = [{"task_id": "1", "label": "1 | 访谈节目", "task_dir": str(self.root),
                      "script": "interview script", "sources": [str(p) for p in self.paths]}]
        self.dialog = self.make_dialog(self.jobs)

    def make_dialog(self, jobs):
        dialog = SmartVideoSourceDialog(jobs)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        return dialog

    def folder(self, relative, dialog=None, job=0):
        return (dialog or self.dialog)._directory_items[(job, (self.root/relative).absolute())]

    def leaves(self, item):
        return [child for child in self.dialog._descendants(item)
                if child.data(0, QtCore.Qt.UserRole)[0] == "source"]

    def test_nested_folders_and_same_named_videos_remain_distinct(self):
        task = self.dialog.tree.topLevelItem(0)
        first, second = self.folder("采访/第一机位"), self.folder("采访/第二机位")
        self.assertIs(first.parent(), self.folder("采访"))
        self.assertIs(second.parent(), self.folder("采访"))
        self.assertIs(self.folder("采访").parent(), task)
        self.assertEqual(first.childCount(), 2)
        self.assertEqual(second.childCount(), 1)
        self.assertIs(self.dialog._source_items[0][1].parent(), task, "root video must not gain a fake folder")
        self.assertNotEqual(first.child(1).data(0, QtCore.Qt.UserRole)[3], second.child(0).data(0, QtCore.Qt.UserRole)[3])
        self.assertEqual(len(self.dialog._source_items[0]), 5)

    def test_folder_checkbox_recursively_selects_only_its_own_subtree(self):
        interview = self.folder("采访")
        interview.setCheckState(0, QtCore.Qt.Unchecked)
        self.assertTrue(all(item.checkState(0) == QtCore.Qt.Unchecked for item in self.leaves(interview)))
        self.assertEqual(self.folder("补充画面").checkState(0), QtCore.Qt.Checked)
        self.assertEqual(self.dialog._source_items[0][1].checkState(0), QtCore.Qt.Checked)
        self.assertEqual(self.dialog.tree.topLevelItem(0).checkState(0), QtCore.Qt.PartiallyChecked)
        interview.setCheckState(0, QtCore.Qt.Checked)
        self.assertEqual(self.dialog.tree.topLevelItem(0).checkState(0), QtCore.Qt.Checked)

    def test_individual_file_updates_every_ancestor_to_partial_or_empty(self):
        folder = self.folder("采访/第一机位")
        folder.child(0).setCheckState(0, QtCore.Qt.Unchecked)
        for node in (folder, folder.parent(), self.dialog.tree.topLevelItem(0)):
            self.assertEqual(node.checkState(0), QtCore.Qt.PartiallyChecked)
        folder.child(1).setCheckState(0, QtCore.Qt.Unchecked)
        self.assertEqual(folder.checkState(0), QtCore.Qt.Unchecked)
        self.assertEqual(folder.parent().checkState(0), QtCore.Qt.PartiallyChecked)
        self.folder("采访/第二机位").setCheckState(0, QtCore.Qt.Unchecked)
        self.assertEqual(folder.parent().checkState(0), QtCore.Qt.Unchecked)

    def test_task_checkbox_reaches_every_nested_video_even_when_collapsed(self):
        self.dialog._collapse_directories()
        task = self.dialog.tree.topLevelItem(0)
        task.setCheckState(0, QtCore.Qt.Unchecked)
        self.assertTrue(all(item.checkState(0) == QtCore.Qt.Unchecked for item in self.dialog._source_items[0]))
        task.setCheckState(0, QtCore.Qt.Checked)
        self.assertTrue(all(item.checkState(0) == QtCore.Qt.Checked for item in self.dialog._source_items[0]))

    def test_accept_preserves_original_input_order_after_grouping_and_sorting(self):
        original = copy.deepcopy(self.jobs)
        self.folder("补充画面").setCheckState(0, QtCore.Qt.Unchecked)
        self.dialog.tree.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
        self.dialog.accept()
        self.assertEqual(self.dialog.selected_jobs[0]["sources"], [str(p) for index, p in enumerate(self.paths) if index != 2])
        self.assertEqual(self.jobs, original, "selection must not modify caller jobs")

    def test_identical_directory_paths_in_two_tasks_have_independent_selection(self):
        jobs = [self.jobs[0], dict(self.jobs[0], task_id="2", label="2 | another interview")]
        dialog = self.make_dialog(jobs)
        self.folder("采访", dialog, 0).setCheckState(0, QtCore.Qt.Unchecked)
        self.assertEqual(self.folder("采访", dialog, 1).checkState(0), QtCore.Qt.Checked)
        dialog.accept()
        self.assertEqual(len(dialog.selected_jobs[0]["sources"]), 2)
        self.assertEqual(len(dialog.selected_jobs[1]["sources"]), 5)

    def test_external_sources_keep_their_ancestry_and_exact_original_paths(self):
        external = self.root.parent/"其他节目"/"第一机位"/"001.mp4"
        external.parent.mkdir(parents=True)
        external.write_bytes(b"external fixture")
        job = dict(self.jobs[0], sources=[str(external), str(self.paths[-1])])
        dialog = self.make_dialog([job])
        leaf = dialog._source_items[0][0]
        self.assertEqual(leaf.parent().text(0), "第一机位")
        self.assertEqual(leaf.parent().parent().text(0), "其他节目")
        self.assertEqual(leaf.toolTip(0), str(external))
        dialog.accept()
        self.assertEqual(dialog.selected_jobs[0]["sources"], job["sources"])

    def test_directory_doubleclick_does_not_launch_player_but_leaf_still_opens_video(self):
        with patch("qt_compat.QtGui.QDesktopServices.openUrl") as opened:
            self.dialog._open_item(self.folder("采访"), 0)
            opened.assert_not_called()
            self.dialog._open_item(self.dialog._source_items[0][0], 0)
        self.assertEqual(Path(opened.call_args.args[0].toLocalFile()), self.paths[0])

    def test_empty_selection_and_missing_script_keep_existing_validation(self):
        self.dialog.tree.topLevelItem(0).setCheckState(0, QtCore.Qt.Unchecked)
        with patch.object(QtWidgets.QMessageBox, "information") as info:
            self.dialog.accept()
        info.assert_called_once()
        self.assertEqual(self.dialog.selected_jobs, [])
        dialog = self.make_dialog([dict(self.jobs[0], script="")])
        with patch.object(QtWidgets.QMessageBox, "warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertEqual(dialog.selected_jobs, [])

    def test_expand_collapse_preserves_choices_and_empty_tasks_are_not_exported(self):
        leaf = self.dialog._source_items[0][0]
        leaf.setCheckState(0, QtCore.Qt.Unchecked)
        self.dialog._collapse_directories()
        self.assertTrue(self.dialog.tree.topLevelItem(0).isExpanded())
        self.assertFalse(self.folder("采访").isExpanded())
        self.dialog.tree.expandAll()
        self.assertEqual(leaf.checkState(0), QtCore.Qt.Unchecked)
        dialog = self.make_dialog([dict(self.jobs[0], sources=[]), self.jobs[0]])
        dialog.accept()
        self.assertEqual(len(dialog.selected_jobs), 1)
        self.assertEqual(len(dialog.selected_jobs[0]["sources"]), 5)

    def test_directory_layout_fits_and_can_be_reviewed_visually(self):
        self.dialog.resize(1100, 600)
        self.dialog.show()
        self.app.processEvents()
        self.assertTrue(self.dialog.tree.visualItemRect(self.folder("采访/第一机位")).isValid())
        if os.environ.get("SMART_SOURCE_TREE_SCREENSHOT"):
            self.dialog.grab().save(os.environ["SMART_SOURCE_TREE_SCREENSHOT"])
