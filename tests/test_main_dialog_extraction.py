import ast
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from PYUI import task_submission_audit_pyui as audit
from PYUI import oral_duration_check_pyui as oral


class DialogExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        self.app.processEvents()

    def test_modules_do_not_import_main_window(self):
        for module in (audit, oral):
            tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
            imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            self.assertNotIn('PYUI.main_pyui', imports)

    def test_legacy_main_exports_all_four_classes(self):
        main = Path(audit.__file__).with_name('main_pyui.py')
        tree = ast.parse(main.read_text(encoding='utf-8'))
        aliases = {a.asname or a.name for n in tree.body if isinstance(n, ast.ImportFrom)
                   and n.module in ('PYUI.task_submission_audit_pyui', 'PYUI.oral_duration_check_pyui')
                   for a in n.names}
        self.assertEqual(aliases, {'TaskSubmissionAuditDialog', 'TaskSubmissionAuditThread',
                                  'OralDurationCheckResultDialog', 'OralDurationCheckThread'})

    def test_oral_result_populates_and_preserves_parent(self):
        parent = QtWidgets.QWidget()
        dialog = oral.OralDurationCheckResultDialog({
            'updated_count': 1, 'updated_rows': [{'row': 5, 'file_name': 'example.mp4', 'duration': 60}],
            'skipped_rows': [{'row': 8, 'reason': 'not found'}]}, parent)
        self.assertIs(dialog.parent(), parent)
        self.assertEqual(dialog.updated_tree.topLevelItemCount(), 1)
        self.assertEqual(dialog.updated_tree.topLevelItem(0).text(1), 'example.mp4')
        self.assertEqual(dialog.skipped_tree.topLevelItem(0).text(2), 'not found')
        dialog.accept()
        parent.deleteLater()

    def test_audit_filter_and_repair_selection(self):
        rows = [{'file_name': name, 'filter_type': kind} for name, kind in [('a', 'reels'), ('b', 'oral')]]
        dialog = audit.TaskSubmissionAuditDialog({'missing': rows, 'type_values': ['reels', 'oral']}, {})
        self.assertEqual(len(dialog.filteredItems(dialog.missing_tree)), 2)
        dialog.type_filter.setCurrentText('reels')
        self.assertEqual([r['file_name'] for r in dialog.filteredItems(dialog.missing_tree)], ['a'])
        self.assertTrue(dialog.repair_button.isEnabled())
        dialog.accept()
        dialog.deleteLater()

    def test_empty_audit_disables_repair(self):
        dialog = audit.TaskSubmissionAuditDialog({}, {})
        self.assertFalse(dialog.repair_button.isEnabled())
        dialog.deleteLater()

    def test_worker_operation_order_and_config_snapshot(self):
        config = {'value': 1}
        worker = audit.TaskSubmissionAuditThread(config, 'repair', [{'id': 1}])
        config['value'] = 2
        results = []
        worker.succeeded.connect(results.append)
        calls = []
        with patch.object(audit, 'repair_submission_items', side_effect=lambda *a, **k: calls.append('repair') or {'write_count': 1}), \
             patch.object(audit, 'scan_task_submission_sheet', side_effect=lambda *a, **k: calls.append('scan') or {'missing': []}):
            worker.run()
        self.assertEqual(worker.config['value'], 1)
        self.assertEqual(calls, ['repair', 'scan'])
        self.assertEqual(results[0]['repair_result']['write_count'], 1)

    def test_both_workers_report_failure_without_success(self):
        for module, cls, function in (
            (audit, audit.TaskSubmissionAuditThread, 'scan_task_submission_sheet'),
            (oral, oral.OralDurationCheckThread, 'check_oral_video_durations')):
            worker = cls({})
            failures, successes = [], []
            worker.failed.connect(lambda *args: failures.append(args))
            worker.succeeded.connect(successes.append)
            with patch.object(module, function, side_effect=RuntimeError('test failure')):
                worker.run()
            self.assertFalse(successes)
            self.assertIn('test failure', failures[0][0])
            self.assertIn('Traceback', failures[0][1])

    def test_active_worker_prevents_dialog_close(self):
        dialog = audit.TaskSubmissionAuditDialog({}, {})
        dialog.worker = object()
        with patch.object(audit, 'thread_is_active', return_value=True), \
             patch.object(audit.QMessageBox, 'information') as message:
            dialog.accept()
            dialog.reject()
        self.assertEqual(message.call_count, 2)
        self.assertEqual(QtWidgets.QDialog.result(dialog), 0)
        dialog.worker = None
        dialog.deleteLater()

    def test_async_audit_completes_and_releases_worker(self):
        dialog = audit.TaskSubmissionAuditDialog({}, {})
        with patch.object(audit, 'scan_task_submission_sheet', return_value={'missing': [{'file_name': 'done'}]}):
            dialog.refresh()
            thread = dialog.worker
            self.assertIsNotNone(thread)
            # A duplicate refresh cannot create a second worker.
            dialog.refresh()
            self.assertIs(dialog.worker, thread)
            self.assertFalse(dialog.refresh_button.isEnabled())
            deadline = time.monotonic() + 5
            while dialog.worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            self.assertIsNone(dialog.worker)
            self.assertEqual(dialog.missing_tree.topLevelItem(0).text(0), 'done')
            self.assertTrue(dialog.refresh_button.isEnabled())
        dialog.deleteLater()

    def test_old_worker_finished_signal_cannot_release_new_worker(self):
        dialog = audit.TaskSubmissionAuditDialog({}, {})
        current = audit.TaskSubmissionAuditThread({})
        stale = audit.TaskSubmissionAuditThread({})
        dialog.worker = current
        dialog.setBusy(True)
        dialog.onWorkerFinished(stale)
        self.assertIs(dialog.worker, current)
        self.assertFalse(dialog.refresh_button.isEnabled())
        dialog.worker = None
        dialog.deleteLater()

    def test_oral_worker_success_and_progress(self):
        worker = oral.OralDurationCheckThread({})
        progress, successes = [], []
        worker.log.connect(progress.append)
        worker.succeeded.connect(successes.append)
        def check(config, progress_callback):
            progress_callback('checking')
            return {'checked_count': 1}
        with patch.object(oral, 'check_oral_video_durations', side_effect=check):
            worker.run()
        self.assertEqual(progress, ['checking'])
        self.assertEqual(successes, [{'checked_count': 1}])


if __name__ == '__main__':
    unittest.main()
