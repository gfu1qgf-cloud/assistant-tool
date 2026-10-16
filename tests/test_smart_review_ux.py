"""Layout discoverability and serialized, main-thread result presentation."""
import os
import time
import unittest
from unittest.mock import patch
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.smart_video_editor.ui import SmartVideoReviewDialog
from app_plugins.builtin.smart_video_editor.foreground_results import ForegroundResults


class SmartReviewUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.patches = [patch("app_plugins.builtin.smart_video_editor.timeline_review.FfplayReviewSurface.prepare"),
                        patch("app_plugins.builtin.smart_video_editor.timeline_review.WaveformLoader.load_task")]
        for item in self.patches: item.start(); self.addCleanup(item.stop)

    def pump(self, seconds=.04):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents(); time.sleep(.002)

    def dialog(self):
        dialog = SmartVideoReviewDialog({"tasks": [], "settings": {}, "summary": {}})
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.timeline_review.close_player)
        return dialog

    def test_navigation_settings_and_primary_actions_are_separated(self):
        dialog = self.dialog()
        dialog.resize(1280, 820); dialog.show(); self.pump()
        timeline = dialog.timeline_review
        self.assertEqual([timeline.inspector_tabs.tabText(i) for i in range(3)], ["问题处理", "气口裁切", "字幕设置"])
        self.assertFalse(timeline.breath_panel.isVisible())
        self.assertFalse(timeline.subtitle_settings_panel.isVisible())
        self.assertTrue(timeline.task_combo.isVisible())
        self.assertTrue(timeline.task_filter_combo.isVisible())
        self.assertGreaterEqual(timeline.task_script_text.height(), 110)
        self.assertTrue(timeline.task_script_text.isVisible())
        self.assertEqual(timeline.script_review_tabs.currentIndex(), 0)
        timeline.script_review_tabs.setCurrentIndex(1); self.pump()
        self.assertTrue(timeline.expected_text.isVisible())
        timeline.script_review_tabs.setCurrentIndex(0)
        self.assertGreaterEqual(timeline.timeline_scroll.viewport().height(), timeline.timeline.height())
        self.assertLessEqual(dialog.minimumSizeHint().width(), 1280)
        self.assertLessEqual(dialog.minimumSizeHint().height(), 820)
        self.assertEqual([a.text() for a in dialog.review_menu_bar.actions()], ["文件", "视频文件", "问题报告", "使用帮助"])
        self.assertFalse(dialog.rename_videos_button.isVisible())
        timeline.inspector_tabs.setCurrentIndex(1); self.pump()
        self.assertTrue(timeline.apply_breath_button.isVisible())
        self.assertTrue(timeline.pause_keep_before_spinbox.isVisible())
        timeline.inspector_tabs.setCurrentIndex(2); self.pump()
        self.assertTrue(timeline.subtitle_max_words_spinbox.isVisible())
        dialog.close()

    def test_menu_actions_keep_existing_slots_and_enabled_state(self):
        dialog = self.dialog()
        timeline = dialog.timeline_review
        action = next(a for a in timeline.clip_actions_menu.actions() if a.text() == "前移当前片段")
        timeline.move_clip_left_button.setEnabled(False)
        self.assertFalse(action.isEnabled())
        timeline.move_clip_left_button.setEnabled(True)
        self.assertTrue(action.isEnabled())
        clicked = []
        timeline.move_clip_left_button.clicked.connect(lambda: clicked.append(True))
        action.trigger(); self.assertEqual(clicked, [True])

    def test_export_labels_remain_readable_after_font_and_mode_changes(self):
        dialog = self.dialog()
        dialog.resize(1000, 820); dialog.show(); self.pump()
        button = dialog.export_button
        font = button.font(); font.setPointSize(13); button.setFont(font)
        for text in ('确认并生成视频与 SRT', '确认并导出分段视频', '导出其余任务'):
            for enabled in (False, True):
                button.setText(text); button.setEnabled(enabled); self.pump()
                self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(text) + 32)
                self.assertGreaterEqual(button.height(), button.fontMetrics().height() + 12)
                self.assertTrue(dialog.rect().contains(button.mapTo(dialog,button.rect().bottomRight())))

    def test_blocked_review_with_chinese_font_preserves_readable_full_text(self):
        import test_smart_video_export_blockers_ui as fixtures
        case = fixtures.ExportBlockersUiTests()
        case.setUpClass(); case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        font_path = Path(r"C:\Windows\Fonts\msyh.ttc")
        if font_path.is_file():
            old_font = self.app.font()
            font_id = QtGui.QFontDatabase.addApplicationFont(str(font_path))
            families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families:
                self.app.setFont(QtGui.QFont(families[0], 9))
                self.addCleanup(self.app.setFont, old_font)
        dialog = case.dialog()
        dialog.resize(1280, 820); dialog.show(); self.pump()
        self.assertTrue(dialog.blocker_panel.isVisible())
        self.assertTrue(dialog.timeline_review.task_script_text.isVisible())
        self.assertGreaterEqual(dialog.timeline_review.task_script_text.height(), 110)
        self.assertLessEqual(dialog.minimumSizeHint().height(), 820)

    def test_automatic_results_wait_for_modal_then_open_once_in_order(self):
        parent = QtWidgets.QWidget(); self.addCleanup(parent.deleteLater)
        allowed, busy, seen = [True], [False], []
        queue = ForegroundResults(parent, lambda: allowed[0], lambda: busy[0])
        modal = QtWidgets.QDialog(parent); modal.setModal(True); modal.show()
        self.addCleanup(modal.deleteLater)
        def show(key):
            self.assertEqual(QtCore.QThread.currentThread(), self.app.thread())
            return queue.show(key, lambda: seen.append(key))
        queue.enqueue("first", lambda: show("first")); queue.enqueue("second", lambda: show("second"))
        self.pump(); self.assertEqual(seen, [])
        modal.close(); self.pump(.25)
        self.assertEqual(seen, ["first", "second"])
        self.assertEqual(queue.queue, [])

    def test_result_arriving_during_review_is_not_nested_or_dropped(self):
        parent = QtWidgets.QWidget(); self.addCleanup(parent.deleteLater)
        queue = ForegroundResults(parent, lambda: True, lambda: False)
        seen = []
        def first():
            seen.append("first-enter")
            queue.enqueue("second", lambda: queue.show("second", lambda: seen.append("second")))
            self.pump()
            self.assertEqual(seen, ["first-enter"])
            seen.append("first-exit")
        queue.enqueue("first", lambda: queue.show("first", first))
        self.pump(.1)
        self.assertEqual(seen, ["first-enter", "first-exit", "second"])

    def test_manual_open_consumes_queued_entry_and_exit_keeps_windows_closed(self):
        parent = QtWidgets.QWidget(); self.addCleanup(parent.deleteLater)
        allowed, seen = [True], []
        queue = ForegroundResults(parent, lambda: allowed[0], lambda: False)
        queue.enqueue("one", lambda: queue.show("one", lambda: seen.append("auto")))
        queue.show("one", lambda: seen.append("manual")); self.pump()
        self.assertEqual(seen, ["manual"])
        queue.enqueue("two", lambda: seen.append("must-not-open"))
        allowed[0] = False; self.pump()
        self.assertEqual(seen, ["manual"])
        self.assertFalse(queue.timer.isActive())

    def test_stopped_plugin_ignores_late_results_until_reenabled(self):
        parent = QtWidgets.QWidget(); self.addCleanup(parent.deleteLater)
        seen = []
        queue = ForegroundResults(parent, lambda: True, lambda: False)
        queue.stop()
        queue.enqueue("late", lambda: seen.append("late"))
        self.assertFalse(queue.show("late", lambda: seen.append("late-manual")))
        self.pump(); self.assertEqual(seen, [])
        queue.resume()
        queue.enqueue("next", lambda: queue.show("next", lambda: seen.append("next")))
        self.pump(); self.assertEqual(seen, ["next"])


if __name__ == "__main__": unittest.main()
