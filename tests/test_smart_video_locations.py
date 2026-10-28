"""Read-only file reveal from the current player and exact right-click target."""
import copy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtGui, QtWidgets
from model import FileLocations as locations
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog
from app_plugins.builtin.smart_video_editor.timeline_widget import SmartTimelineWidget


class FileLocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "源视频 & (test), 20%.mp4"
        self.source.write_bytes(b"fixture")
        for name in ("warning", "information"):
            p = patch.object(QtWidgets.QMessageBox, name)
            setattr(self, name, p.start()); self.addCleanup(p.stop)
        p = patch.object(QtCore.QProcess, "startDetached", return_value=(True, 123))
        self.start = p.start(); self.addCleanup(p.stop)
        p = patch.object(QtGui.QDesktopServices, "openUrl", return_value=True)
        self.desktop = p.start(); self.addCleanup(p.stop)
        p = patch.object(locations, "os", SimpleNamespace(name="nt"))
        p.start(); self.addCleanup(p.stop)

    def test_windows_reveals_exact_file_with_separate_arguments(self):
        before = self.source.read_bytes()
        self.assertTrue(locations.open_file_location(self.source))
        self.start.assert_called_once_with("explorer.exe", ["/select,", str(self.source.resolve())])
        self.desktop.assert_not_called(); self.warning.assert_not_called()
        self.assertEqual(self.source.read_bytes(), before)

    def test_missing_file_reports_actual_path_without_launching_or_creating(self):
        missing = self.root / "missing.mp4"
        self.assertFalse(locations.open_file_location(missing))
        self.assertIn(str(missing), self.warning.call_args.args[2])
        self.assertIn("刷新", self.warning.call_args.args[2])
        self.start.assert_not_called(); self.desktop.assert_not_called()
        self.assertFalse(missing.exists())

    def test_empty_path_and_directory_never_accidentally_open_cwd(self):
        for source in (None, "", self.root):
            self.assertFalse(locations.open_file_location(source))
        self.start.assert_not_called(); self.desktop.assert_not_called()
        self.assertEqual(self.information.call_count, 2)

    def test_launch_failure_and_invalid_path_are_normal_warnings(self):
        self.start.return_value = (False, 0)
        self.assertFalse(locations.open_file_location(self.source))
        self.assertIn("无法打开", self.warning.call_args.args[2])
        self.start.side_effect = OSError("launch failed")
        self.assertFalse(locations.open_file_location(self.source))
        self.assertIn("launch failed", self.warning.call_args.args[2])
        self.assertFalse(locations.open_file_location(object()))

    def test_other_platform_opens_local_parent_url(self):
        with patch.object(locations, "os", SimpleNamespace(name="posix")):
            self.assertTrue(locations.open_file_location(self.source))
        self.start.assert_not_called()
        url = self.desktop.call_args.args[0]
        self.assertTrue(url.isLocalFile())
        self.assertEqual(Path(url.toLocalFile()), self.source.resolve().parent)


class SmartVideoLocationGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        # An empty offscreen font database understates the dialog's real height.
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dialogs = []
        for target in (
            "app_plugins.builtin.smart_video_editor.timeline_review.FfplayReviewSurface.prepare",
            "app_plugins.builtin.smart_video_editor.timeline_review.WaveformLoader.load_task",
        ):
            p = patch(target); p.start(); self.addCleanup(p.stop)
        p = patch("app_plugins.builtin.smart_video_editor.timeline_review._media_shape", return_value=(1080, 1920, 30))
        p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close(); dialog.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents(); self.temporary.cleanup()

    def dialog(self, empty=False):
        clips = []
        for index in range(2):
            path = self.root / f"source-{index}.mp4"; path.write_bytes(b"fixture")
            clips.append({"source": str(path), "file_name": path.name, "included": True,
                          "source_index": index, "export_order": index+1, "status": "green",
                          "original_duration": 4.0, "trim_start": 0.0, "trim_end": 4.0,
                          "kept_ranges": [[0.0, 4.0]], "issues": [], "similarity": 1.0,
                          "expected_text": "alpha", "recognized_text": "alpha"})
        tasks = [] if empty else [{"task_id": "current", "label": "当前任务", "task_dir": str(self.root), "clips": clips}]
        dialog = SmartVideoReviewDialog({"tasks": tasks, "settings": {}, "summary": {}})
        self.dialogs.append(dialog)
        return dialog

    def test_player_button_follows_current_source_and_leaves_review_unchanged(self):
        dialog = self.dialog(); review = dialog.timeline_review
        dialog.resize(1280, 820); dialog.show(); self.app.processEvents()
        self.assertTrue(review.open_video_location_button.isVisible())
        self.assertLessEqual(dialog.minimumSizeHint().width(), 1280)
        self.assertLessEqual(dialog.minimumSizeHint().height(), 820)
        before = copy.deepcopy(dialog.bundle)
        with patch("app_plugins.builtin.smart_video_editor.timeline_review.open_file_location", return_value=True) as reveal:
            review.select_clip(0, 1)
            review.open_video_location_button.click()
            reveal.assert_called_once_with(str(self.root / "source-1.mp4"), review)
            self.assertEqual(review.open_video_location_button.toolTip(), str(self.root / "source-1.mp4"))
        self.assertEqual(dialog.bundle, before)

    def test_no_current_task_disables_button_and_invalid_index_is_safe(self):
        dialog = self.dialog(empty=True); review = dialog.timeline_review
        self.assertFalse(review.open_video_location_button.isEnabled())
        with patch("app_plugins.builtin.smart_video_editor.timeline_review.open_file_location", return_value=False) as reveal:
            self.assertFalse(review.open_clip_location(9))
            reveal.assert_called_once_with(None, review)

    def test_clip_location_signal_targets_right_clicked_clip_not_current_player(self):
        dialog = self.dialog(); review = dialog.timeline_review
        review.select_clip(0, 0)
        source_before = review.current_segment["source"]
        before = copy.deepcopy(dialog.bundle)
        with patch("app_plugins.builtin.smart_video_editor.timeline_review.open_file_location", return_value=True) as reveal:
            review.timeline.clipLocationRequested.emit(1)
            reveal.assert_called_once_with(str(self.root / "source-1.mp4"), review)
        self.assertEqual(review.current_segment["source"], source_before)
        self.assertEqual(dialog.bundle, before)

    def test_table_right_click_and_file_menu_use_selected_original_source(self):
        dialog = self.dialog(); before = copy.deepcopy(dialog.bundle)
        dialog._open_advanced_review()
        dialog.resize(1280, 820); dialog.show(); self.app.processEvents()
        self.assertEqual(dialog.table.contextMenuPolicy(), QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        file_menu = next(action.menu() for action in dialog.review_menu_bar.actions() if action.text() == "视频文件")
        location_action = next(action for action in file_menu.actions() if action.text() == "打开所选视频文件位置")
        dialog.table.setCurrentCell(1, dialog.COL_FILE)
        with patch("app_plugins.builtin.smart_video_editor.ui.open_file_location", return_value=True) as reveal:
            location_action.trigger()
            reveal.assert_called_once_with(str(self.root / "source-1.mp4"), dialog)
            reveal.reset_mock()
            def execute(menu, _position):
                next(action for action in menu.actions() if action.text() == "打开文件位置").trigger()
            with patch.object(QtWidgets.QMenu, "exec", execute):
                point = dialog.table.visualItemRect(dialog.table.item(0, dialog.COL_FILE)).center()
                dialog._clip_context_menu(point)
            reveal.assert_called_once_with(str(self.root / "source-0.mp4"), dialog)
        self.assertEqual(dialog.bundle, before)

    def test_timeline_context_location_and_existing_include_action_are_distinct(self):
        widget = SmartTimelineWidget(); self.addCleanup(widget.deleteLater)
        widget._segment_rects = [(QtCore.QRectF(0, 0, 100, 100), {"clip_index": 7, "included": True,
            'source_start':0.0,'source_end':5.0,'timeline_start':0.0,'timeline_end':5.0})]
        event = SimpleNamespace(pos=lambda: QtCore.QPoint(20, 20), globalPos=lambda: QtCore.QPoint(20, 20), accept=Mock())
        reveal, include = Mock(), Mock()
        widget.clipLocationRequested.connect(reveal); widget.clipInclusionRequested.connect(include)
        def choose_location(menu, _position):
            return next(action for action in menu.actions() if action.text() == "打开文件位置")
        with patch.object(QtWidgets.QMenu, "exec", choose_location): widget.contextMenuEvent(event)
        reveal.assert_called_once_with(7); include.assert_not_called(); event.accept.assert_called_once()
        with patch.object(QtWidgets.QMenu, "exec", lambda menu, _position: next(
                action for action in menu.actions() if action.text()=='删除整个原始片段（保留原文件）')): widget.contextMenuEvent(event)
        include.assert_called_once_with(7, False)


if __name__ == "__main__":
    unittest.main()
