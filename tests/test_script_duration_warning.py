import unittest

from qt_compat import QtCore, QtTest, QtWidgets
from app_plugins.builtin.script_segmenter.engine import normalize_settings, split_copy
from app_plugins.builtin.script_segmenter.duration_warning import duration_check, warning_text
from test_script_segmenter import FakeContext
from app_plugins.builtin.script_segmenter.ui import ScriptSegmenterDialog
from app_plugins.builtin.script_segmenter.quick_copy import QuickCopyController, run_quick_copy
from model.BackgroundTasks import BackgroundTaskManager, TaskRunContext
import threading
import time
from types import SimpleNamespace


class DurationWarningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.manager = BackgroundTaskManager()
        self.context = FakeContext(self.manager)
        self.dialog = ScriptSegmenterDialog(self.context)
        self.controller = QuickCopyController(self.context)
        self.app.clipboard().setText('unchanged')

    def tearDown(self):
        self.dialog.copy_guard.cancel()
        self.controller.stop()
        self.dialog.close()
        self.dialog.deleteLater()
        self.controller.deleteLater()
        self.manager.shutdown()
        self.manager.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        QtTest.QTest.qWait(10)

    def result(self, count=451, **settings):
        return split_copy('amen ' * count, settings)

    def load(self, result):
        self.dialog.current_entry()['result'] = result
        self.dialog.render_result()

    def click_proceed(self, guard):
        box = guard.pending
        self.assertIsNotNone(box)
        button = next(b for b in box.buttons() if b.text() == '仍然复制')
        button.click()
        QtTest.QTest.qWait(5)

    def test_default_limit_and_normalization(self):
        self.assertEqual(normalize_settings({})['total_limit_seconds'], 180)
        self.assertEqual(normalize_settings({'total_limit_seconds': 'bad'})['total_limit_seconds'], 180)
        self.assertEqual(normalize_settings({'total_limit_seconds': 240})['total_limit_seconds'], 240)

    def test_exact_boundary_and_no_repeated_padding(self):
        result = self.result(450, padding_seconds=2)
        check = duration_check(result)
        self.assertEqual(check['seconds'], 180)
        self.assertGreater(result['video_seconds'], 180)
        self.load(result)
        self.dialog.copy_all()
        self.assertIsNone(self.dialog.copy_guard.pending)
        self.assertNotEqual(self.app.clipboard().text(), 'unchanged')

    def test_banner_and_cancel_preserve_clipboard_and_no_nested_wait(self):
        self.load(self.result())
        self.assertFalse(self.dialog.duration_note.isHidden())
        self.assertIn('超过', self.dialog.duration_note.text())
        self.dialog.copy_all()
        self.assertIsNotNone(self.dialog.copy_guard.pending)
        self.assertEqual(self.app.clipboard().text(), 'unchanged')
        box = self.dialog.copy_guard.pending
        self.assertIn('取消', box.defaultButton().text())
        box.reject()
        self.assertIsNone(self.dialog.copy_guard.pending)
        self.assertEqual(self.app.clipboard().text(), 'unchanged')

    def test_confirm_once_same_content_and_changed_content_requires_confirmation(self):
        self.load(self.result())
        self.dialog.copy_all()
        self.click_proceed(self.dialog.copy_guard)
        self.assertIn('第1段', self.app.clipboard().text())
        self.dialog.copy_all()
        self.assertIsNone(self.dialog.copy_guard.pending)
        self.dialog.table.selectAll()
        self.dialog.copy_selected()
        self.assertIsNone(self.dialog.copy_guard.pending)
        self.load(self.result(452))
        self.dialog.copy_all()
        self.assertIsNotNone(self.dialog.copy_guard.pending)

    def test_selected_short_segment_does_not_warn_for_long_document(self):
        self.load(self.result())
        self.dialog.table.selectRow(0)
        self.dialog.copy_selected(False)
        self.assertIsNone(self.dialog.copy_guard.pending)
        self.assertEqual(self.app.clipboard().text(), self.dialog.current_entry()['result']['segments'][0]['text'])

    def test_select_all_uses_total_duration_and_both_copy_modes_guarded(self):
        self.load(self.result())
        self.dialog.table.selectAll()
        self.dialog.copy_selected(False)
        self.assertIsNotNone(self.dialog.copy_guard.pending)
        self.dialog.copy_guard.cancel()
        self.dialog.copy_selected(True)
        self.assertIsNotNone(self.dialog.copy_guard.pending)
        self.assertEqual(self.app.clipboard().text(), 'unchanged')

    def test_pending_confirmation_does_not_duplicate_or_survive_stop(self):
        result = self.result()
        check = duration_check(result)
        output = {'text': 'should not copy', 'count': 1, 'warnings': 0, 'duration_checks': [check]}
        self.controller.completed(output)
        box = self.controller.copy_guard.pending
        self.controller.completed(output)
        self.assertIs(self.controller.copy_guard.pending, box)
        self.controller.stop()
        self.assertIsNone(self.controller.copy_guard.pending)
        self.controller.completed(output)
        self.assertEqual(self.app.clipboard().text(), 'unchanged')

    def test_quick_copy_checks_each_task_not_sum_of_batch(self):
        run = TaskRunContext(threading.Event(), lambda *a: None, lambda *a: None)
        entries = [{'key': str(i), 'label': str(i), 'text': 'amen ' * 250} for i in range(2)]
        output = run_quick_copy({'entries': entries, 'settings': {}}, run)
        self.controller.completed(output)
        self.assertIsNone(self.controller.copy_guard.pending)
        self.assertIn('任务：0', self.app.clipboard().text())

    def test_quick_copy_overlimit_confirmation_names_task_and_cancels(self):
        run = TaskRunContext(threading.Event(), lambda *a: None, lambda *a: None)
        entries = [{'key': 'a', 'label': '任务甲', 'text': 'amen ' * 451}]
        output = run_quick_copy({'entries': entries, 'settings': {}}, run)
        self.controller.completed(output)
        self.assertIn('任务甲', self.controller.copy_guard.pending.text())
        self.controller.copy_guard.cancel()
        self.assertEqual(self.app.clipboard().text(), 'unchanged')
        self.controller.completed(output)
        self.click_proceed(self.controller.copy_guard)
        self.assertEqual(self.app.clipboard().text(), output['text'])

    def test_real_background_handle_finishes_while_confirmation_is_pending(self):
        self.context.targets = [{'task': SimpleNamespace(task_audio_text='amen ' * 451),
                                 'label': '任务甲', 'target_dir': ''}]
        self.controller.copy_tasks([0])
        deadline = time.monotonic() + 3
        while self.controller.handle is not None and time.monotonic() < deadline:
            QtTest.QTest.qWait(5)
        self.assertIsNone(self.controller.handle)
        self.assertIsNotNone(self.controller.copy_guard.pending)
        self.assertFalse(self.manager.is_busy())
        self.assertEqual(self.app.clipboard().text(), 'unchanged')
        self.controller.copy_guard.cancel()
        self.context.targets[0]['task'].task_audio_text = 'short'
        self.controller.copy_tasks([0])
        deadline = time.monotonic() + 3
        while self.controller.handle is not None and time.monotonic() < deadline:
            QtTest.QTest.qWait(5)
        self.assertIsNone(self.controller.handle)
        self.assertIn('short', self.app.clipboard().text())

    def test_near_limit_and_edit_clear_warning(self):
        self.load(self.result(430))
        self.assertIn('接近', self.dialog.duration_note.text())
        self.dialog.input.setPlainText('short')
        self.assertTrue(self.dialog.duration_note.isHidden())

    def test_settings_can_be_saved_and_reopened(self):
        self.dialog.total_limit.setValue(240)
        self.dialog.save_settings()
        other = ScriptSegmenterDialog(self.context)
        try:
            self.assertEqual(other.total_limit.value(), 240)
        finally:
            other.deleteLater()

    def test_numbers_and_punctuation_reuse_existing_estimate(self):
        result = split_copy('2026, amen. ' * 110)
        check = duration_check(result)
        self.assertEqual(check['seconds'], result['estimated_seconds'])
        self.assertGreater(check['seconds'], 180)
        self.assertIn('建议精简', warning_text(check))


if __name__ == '__main__':
    unittest.main()
