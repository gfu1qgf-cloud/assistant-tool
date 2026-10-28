"""Real asynchronous lifetimes and cache invalidation, not only happy paths."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from PyQt6 import sip
from model.OdsHelper import TaskData, ReadTaskOds2
from model.TaskLoading import TaskLoadEngine, TaskLoadRequest, TaskLoadCancelled, TaskLoadError, TaskLoadResult
from PYUI.task_loading import TaskLoadController


def task(body='hello', identifier='1'):
    return TaskData({'task_id': identifier, 'task_type': 'reels', 'task_audio_text': body,
                     '_source_row': 2, 'task_date': '1005'})


def report(count=1):
    return {'sheet_name': 'test', 'header_row': 1, 'task_count': count,
            'matched_headers': {}, 'unmatched_fields': [], 'defaulted_counts': {},
            'extra_headers': [], 'labels': {}}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / '1005'
        self.project.mkdir()
        self.table = self.project / 'tasks.ods'
        self.table.write_bytes(b'one')
        self.config = {'task_table_file_name': 'tasks.ods', 'task_table_schema': {'revision': 1}}
        self.schema = {'revision': 1}
        self.reader = Mock(return_value=([task()], report()))
        self.detector = Mock(return_value='sk')
        self.engine = TaskLoadEngine(reader=self.reader, detector=self.detector,
            config_reader=lambda path: copy.deepcopy(self.config), schema_loader=lambda **kwargs: dict(self.schema))
        self.request = TaskLoadRequest(str(self.root), '1005', 'synthetic-config')

    def test_repeat_uses_content_cache_and_language_cache(self):
        first, second = self.engine.load(self.request), self.engine.load(self.request)
        self.assertFalse(first.table_cached)
        self.assertTrue(second.table_cached)
        self.assertEqual(second.language_hits, 1)
        self.reader.assert_called_once()
        self.detector.assert_called_once_with('hello')
        self.assertEqual(second.languages, ['sk'])

    def test_same_size_and_same_mtime_edit_cannot_hit_stale_cache(self):
        self.engine.load(self.request)
        stamp = self.table.stat()
        self.table.write_bytes(b'two')
        os.utime(self.table, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.reader.return_value = ([task('changed')], report())
        second = self.engine.load(self.request)
        self.assertFalse(second.table_cached)
        self.assertEqual(second.tasks[0].task_audio_text, 'changed')
        self.assertEqual(self.reader.call_count, 2)

    def test_changed_field_mapping_invalidates_parse_cache(self):
        self.engine.load(self.request)
        self.schema['revision'] = 2
        self.assertFalse(self.engine.load(self.request).table_cached)
        self.assertEqual(self.reader.call_count, 2)

    def test_mutating_live_task_does_not_poison_cached_task_objects(self):
        first = self.engine.load(self.request)
        first.tasks[0].task_audio_text = 'plugin temporary change'
        first.report['task_count'] = 999
        second = self.engine.load(self.request)
        self.assertEqual(second.tasks[0].task_audio_text, 'hello')
        self.assertEqual(second.report['task_count'], 1)

    def test_same_text_is_shared_but_changed_text_is_detected(self):
        self.reader.return_value = ([task('same', '1'), task('same', '2')], report(2))
        first = self.engine.load(self.request)
        self.assertEqual(first.language_hits, 1)
        self.detector.assert_called_once_with('same')
        self.table.write_bytes(b'changed')
        self.reader.return_value = ([task('same', '1'), task('new', '2')], report(2))
        second = self.engine.load(self.request)
        self.assertEqual(second.language_hits, 1)
        self.assertEqual(self.detector.call_count, 2)

    def test_caches_are_bounded(self):
        engine = TaskLoadEngine(language_limit=2, table_limit=1, reader=self.reader,
            detector=self.detector, config_reader=lambda path: self.config, schema_loader=lambda **kwargs: self.schema)
        for value in ('first', 'second', 'third'):
            engine.language(value)
        self.assertEqual(len(engine._languages), 2)
        self.assertIsNone(engine.cached_language('first'))
        for value in (b'one', b'two'):
            self.table.write_bytes(value)
            engine.load(self.request)
        self.assertEqual(len(engine._tables), 1)

    def test_language_error_does_not_discard_task(self):
        self.detector.side_effect = ValueError('synthetic language failure')
        result = self.engine.load(self.request)
        self.assertEqual(len(result.tasks), 1)
        self.assertEqual(result.languages, ['unknown'])

    def test_config_change_during_parse_rejects_result(self):
        def read(*args, **kwargs):
            self.config['task_table_schema'] = {'revision': 2}
            return [task()], report()
        self.reader.side_effect = read
        with self.assertRaisesRegex(TaskLoadError, '配置已改变'):
            self.engine.load(self.request)

    def test_cancel_before_io_does_not_read_config_or_table(self):
        self.engine.config_reader = Mock(side_effect=AssertionError('should not read'))
        with self.assertRaises(TaskLoadCancelled):
            self.engine.load(self.request, cancelled=lambda: True)
        self.reader.assert_not_called()

    def test_cancel_after_parse_does_not_cache_partial_results(self):
        event = threading.Event()
        def read(*args, **kwargs):
            event.set()
            return [task()], report()
        self.reader.side_effect = read
        with self.assertRaises(TaskLoadCancelled):
            self.engine.load(self.request, cancelled=event.is_set)
        self.assertFalse(self.engine._tables)

    def test_reader_consumes_the_exact_hashed_snapshot(self):
        self.engine.load(self.request)
        self.assertEqual(self.reader.call_args.kwargs['source_bytes'], b'one')
        self.assertTrue(callable(self.reader.call_args.kwargs['cancelled']))

    def test_new_snapshot_reader_preserves_normal_ods_behavior_and_can_cancel(self):
        from odf.opendocument import OpenDocumentSpreadsheet
        from odf.table import Table, TableRow, TableCell
        from odf.text import P
        from model.TaskTableSchema import normalize_task_table_schema
        document = OpenDocumentSpreadsheet()
        sheet = Table(name='Tasks')
        document.spreadsheet.addElement(sheet)
        for values in (('ID', 'Type', 'Body'), ('1', 'reels', 'alpha'), ('2', 'reels', 'beta')):
            row = TableRow()
            for value in values:
                cell = TableCell()
                cell.addElement(P(text=value))
                row.addElement(cell)
            sheet.addElement(row)
        document.save(str(self.table))
        schema = normalize_task_table_schema({'fields': {
            'task_id': {'aliases': ['ID']}, 'task_type': {'aliases': ['Type']},
            'task_audio_text': {'aliases': ['Body']}}})
        normal, original_report = ReadTaskOds2(self.table, schema=schema, return_report=True)
        snapshot, snapshot_report = ReadTaskOds2('not-read.ods', schema=schema, return_report=True,
                                                source_bytes=self.table.read_bytes())
        self.assertEqual([vars(value) for value in normal], [vars(value) for value in snapshot])
        self.assertEqual(original_report, snapshot_report)
        calls = []
        def cancelled():
            calls.append(1)
            return len(calls) >= 3
        with self.assertRaises(InterruptedError):
            ReadTaskOds2(self.table, schema=schema, cancelled=cancelled)


class LoadHost(QtWidgets.QDialog):
    def __init__(self, root):
        super().__init__()
        self.load_btn = QtWidgets.QPushButton('加载', self)
        self.task_status_label = QtWidgets.QLabel('previous', self)
        self.task_path_edit = QtWidgets.QLineEdit(str(root), self)
        self.dateEdit = QtWidgets.QDateEdit(QtCore.QDate(2026, 10, 5), self)
        self.task_table_widget = QtWidgets.QTableWidget(1, 5, self)
        self.task_list = [task('old', 'old')]
        self.loaded_project_dir = Path(root) / 'old'
        self.logs, self.errors, self.rendered = [], [], []
        self._task_language_by_row = {0: 'sk'}
        self.file_explorer_tree_view = Mock()
        self.startTaskReferenceDownloads = Mock()
        self.updateGoogleSheetDownloadTarget = Mock()

    def refreshTaskWidget(self, languages=None):
        self.rendered.append([value.task_id for value in self.task_list])
        self._task_language_by_row = dict(enumerate(languages))
        self.task_table_widget.clearSelection()
        self.task_table_widget.setRowCount(len(self.task_list))
        for row, value in enumerate(self.task_list):
            self.task_table_widget.setItem(row, 0, QtWidgets.QTableWidgetItem(value.task_id))

    def appendLog(self, value, **kwargs):
        self.logs.append(value)

    def Critical(self, value):
        self.errors.append(value)


class AsyncLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.host = LoadHost(self.root)
        self.controllers, self.releases = [], []

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
        for event in self.releases:
            event.set()
        end = time.monotonic() + 2
        while any(worker.is_alive() for controller in self.controllers for worker, _ in controller.workers.values()):
            if time.monotonic() > end:
                break
            self.app.processEvents()
            time.sleep(.002)
        if not sip.isdeleted(self.host):
            self.host.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def wait(self, condition, timeout=3):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(condition(), 'asynchronous state did not complete')

    def outcome(self, request, identifier='new'):
        return TaskLoadResult(Path(request.root) / request.day, [task(identifier=identifier)], report(), ['sk'],
                              {'table': .1, 'language': .1, 'total': .2}, False, 0)

    def controller(self, runner, **kwargs):
        engine = Mock()
        engine.load.side_effect = runner
        controller = TaskLoadController(self.host, engine=engine, **kwargs)
        self.controllers.append(controller)
        return controller

    def blocked(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def test_slow_io_does_not_block_gui_heartbeat(self):
        release = self.blocked()
        ids = []
        def load(request, **kwargs):
            ids.append(threading.get_ident())
            release.wait(2)
            return self.outcome(request)
        controller = self.controller(load)
        beats = []
        timer = QtCore.QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: beats.append(1))
        timer.start()
        begin = time.monotonic()
        controller.request(str(self.root), '1005', 'config')
        self.assertLess(time.monotonic() - begin, .1)
        self.wait(lambda: len(beats) >= 12)
        self.assertNotEqual(ids[0], threading.get_ident())
        self.assertEqual(self.host.task_list[0].task_id, 'old')
        release.set()
        self.wait(lambda: not controller.busy)
        timer.stop()
        self.assertEqual(self.host.task_list[0].task_id, 'new')

    def test_late_old_project_cannot_overwrite_new_project(self):
        release = self.blocked()
        def load(request, **kwargs):
            if request.day == '1005':
                release.wait(2)  # Simulate uninterruptible native IO.
            return self.outcome(request, request.day)
        controller = self.controller(load)
        controller.request(str(self.root), '1005', 'config')
        self.host.dateEdit.setDate(QtCore.QDate(2026, 10, 6))
        controller.request(str(self.root), '1006', 'config')
        self.wait(lambda: not controller.busy)
        self.assertEqual(self.host.task_list[0].task_id, '1006')
        release.set()
        self.wait(lambda: not any(worker.is_alive() for worker, _ in controller.workers.values()))
        controller.poll()
        self.assertEqual(self.host.rendered, [['1006']])
        self.host.startTaskReferenceDownloads.assert_called_once_with(self.root / '1006')

    def test_path_changed_without_new_load_discards_result(self):
        release = self.blocked()
        def load(request, **kwargs):
            release.wait(2)
            return self.outcome(request)
        controller = self.controller(load)
        controller.request(str(self.root), '1005', 'config')
        self.host.task_path_edit.setText(str(self.root / 'other'))
        release.set()
        self.wait(lambda: not controller.busy)
        self.assertEqual(self.host.task_list[0].task_id, 'old')

    def test_timeout_is_nonblocking_and_late_success_is_ignored(self):
        release = self.blocked()
        def load(request, **kwargs):
            release.wait(2)
            return self.outcome(request)
        controller = self.controller(load, timeout=.08)
        controller.request(str(self.root), '1005', 'config')
        self.wait(lambda: not controller.busy)
        self.assertIn('超时', self.host.task_status_label.text())
        release.set()
        self.wait(lambda: not any(worker.is_alive() for worker, _ in controller.workers.values()))
        controller.poll()
        self.assertEqual(self.host.task_list[0].task_id, 'old')
        self.assertEqual(self.host.load_btn.text(), '加载')

    def test_repeated_click_cancels_instead_of_starting_duplicates(self):
        release = self.blocked()
        def load(request, **kwargs):
            release.wait(2)
            return self.outcome(request)
        controller = self.controller(load)
        self.assertTrue(controller.request(str(self.root), '1005', 'config'))
        self.assertFalse(controller.request(str(self.root), '1005', 'config'))
        self.assertFalse(controller.busy)
        self.assertEqual(controller.engine.load.call_count, 1)

    def test_hung_reads_are_bounded_and_recover_after_return(self):
        release = self.blocked()
        def load(request, **kwargs):
            release.wait(2)
            return self.outcome(request)
        controller = self.controller(load, max_workers=2)
        controller.request(str(self.root), '1005', 'config')
        controller.cancel()
        controller.request(str(self.root), '1005', 'config')
        controller.cancel()
        self.assertFalse(controller.request(str(self.root), '1005', 'config'))
        self.assertEqual(len(controller.workers), 2)
        release.set()
        self.wait(lambda: not any(worker.is_alive() for worker, _ in controller.workers.values()))
        self.assertTrue(controller.request(str(self.root), '1005', 'config'))
        self.wait(lambda: not controller.busy)

    def test_failure_preserves_previous_tasks_and_project(self):
        controller = self.controller(lambda request, **kwargs: (_ for _ in ()).throw(ValueError('bad table')))
        old_tasks, old_project = self.host.task_list, self.host.loaded_project_dir
        controller.request(str(self.root), '1005', 'config')
        self.wait(lambda: not controller.busy)
        self.assertIs(self.host.task_list, old_tasks)
        self.assertEqual(self.host.loaded_project_dir, old_project)
        self.assertEqual(self.host.errors, ['bad table'])
        self.host.startTaskReferenceDownloads.assert_not_called()

    def test_dependency_system_exit_is_reported_instead_of_waiting_for_timeout(self):
        controller = self.controller(lambda request, **kwargs: (_ for _ in ()).throw(SystemExit('missing dependency')))
        controller.request(str(self.root), '1005', 'config')
        self.wait(lambda: not controller.busy)
        self.assertEqual(self.host.errors, ['missing dependency'])

    def test_shutdown_does_not_wait_for_hung_io_or_call_deleted_window(self):
        release = self.blocked()
        def load(request, **kwargs):
            release.wait(2)
            return self.outcome(request)
        controller = self.controller(load)
        controller.request(str(self.root), '1005', 'config')
        started = time.monotonic()
        controller.shutdown()
        self.assertLess(time.monotonic() - started, .1)
        self.host.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        release.set()
        self.wait(lambda: not any(worker.is_alive() for worker, _ in controller.workers.values()))
        controller.poll()  # Must not dereference the destroyed host.

    def test_reference_side_failure_cannot_turn_successful_load_into_failure(self):
        self.host.startTaskReferenceDownloads.side_effect = OSError('external disk unavailable')
        controller = self.controller(lambda request, **kwargs: self.outcome(request))
        controller.request(str(self.root), '1005', 'config')
        self.wait(lambda: not controller.busy)
        self.assertEqual(self.host.task_list[0].task_id, 'new')
        self.assertIn('已加载', self.host.task_status_label.text())
        self.assertFalse(self.host.errors)

    def test_hidden_directory_panel_is_not_enumerated_on_load(self):
        self.host.file_explorer_tree_view.isVisible.return_value = False
        controller = self.controller(lambda request, **kwargs: self.outcome(request))
        controller.request(str(self.root), '1005', 'config')
        self.wait(lambda: not controller.busy)
        self.host.file_explorer_tree_view.load_directory.assert_not_called()

    def test_same_project_reload_preserves_selection_by_identity(self):
        self.host.task_list = [task(identifier='a'), task(identifier='b')]
        self.host.refreshTaskWidget(languages=['sk', 'sk'])
        self.host.loaded_project_dir = self.root / '1005'
        self.host.task_table_widget.selectRow(1)
        def load(request, **kwargs):
            outcome = self.outcome(request)
            outcome.tasks = [task(identifier='b'), task(identifier='a')]
            outcome.languages = ['sk', 'sk']
            return outcome
        controller = self.controller(load)
        controller.request(str(self.root), '1005', 'config')
        self.wait(lambda: not controller.busy)
        self.assertEqual([index.row() for index in self.host.task_table_widget.selectionModel().selectedRows()], [0])


class IntegrationDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_real_table_fill_uses_prepared_languages_without_gui_detection(self):
        from PYUI.main_pyui import MainDialog
        with tempfile.TemporaryDirectory() as folder:
            host = LoadHost(folder)
            try:
                for name in ('refreshTaskWidget', '_fillTaskWidget', 'markTaskLanguages',
                             'getTaskLanguageColor', 'getTaskLanguageName'):
                    setattr(host, name, types.MethodType(getattr(MainDialog, name), host))
                host.task_language_colors = MainDialog.task_language_colors
                host.task_language_names = MainDialog.task_language_names
                host._task_language_color_by_code = {}
                host.detectTaskLanguage = Mock(side_effect=AssertionError('GUI language detection forbidden'))
                host.task_list = [task(identifier=str(index)) for index in range(1517)]
                host.refreshTaskWidget(languages=['sk'] * 1517)
                host.detectTaskLanguage.assert_not_called()
                self.assertEqual(host.task_table_widget.rowCount(), 1517)
                self.assertEqual(host.task_table_widget.item(1516, 0).text(), '1516')
                self.assertTrue(host.task_table_widget.updatesEnabled())
                self.assertFalse(host.task_table_widget.signalsBlocked())
                with self.assertRaisesRegex(ValueError, '不一致'):
                    host.refreshTaskWidget(languages=[])
                self.assertTrue(host.task_table_widget.updatesEnabled())
                self.assertFalse(host.task_table_widget.signalsBlocked())
            finally:
                host.deleteLater()
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_public_load_entry_does_not_read_config_or_stat_directories_on_gui(self):
        from PYUI.main_pyui import MainDialog
        with tempfile.TemporaryDirectory() as folder:
            host = LoadHost(folder)
            try:
                host.task_loader = Mock()
                host.config_name = 'config.json'
                host.load_config = Mock(side_effect=AssertionError('GUI config read forbidden'))
                host.getTodayDir = Mock(side_effect=AssertionError('GUI stat forbidden'))
                MainDialog.loadTask(host)
                host.task_loader.request.assert_called_once_with(str(folder), '1005', 'config.json')
                host.load_config.assert_not_called()
                host.getTodayDir.assert_not_called()
            finally:
                host.deleteLater()
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_reference_preflight_runs_off_gui_and_cancel_prevents_download(self):
        from model import TaskReferenceDownloader as references
        with tempfile.TemporaryDirectory() as folder:
            release = threading.Event()
            ids, results, beats = [], [], []
            def build(*args):
                ids.append(threading.get_ident())
                release.wait(2)
                return [], []
            worker = references.TaskReferenceDownloadThread([], tasks=[task()], root_dir=folder,
                                                            cache_path=Path(folder) / 'cache.json')
            worker.completed.connect(results.append)
            timer = QtCore.QTimer()
            timer.setInterval(10)
            timer.timeout.connect(lambda: beats.append(1))
            try:
                with patch.object(references, 'build_task_reference_jobs', side_effect=build), \
                     patch.object(references, 'download_one', side_effect=AssertionError('unexpected download')):
                    timer.start()
                    worker.start()
                    end = time.monotonic() + 2
                    while len(beats) < 10 and time.monotonic() < end:
                        self.app.processEvents()
                        time.sleep(.002)
                    self.assertGreaterEqual(len(beats), 10)
                    self.assertNotEqual(ids[0], threading.get_ident())
                    worker.requestInterruption()
                    release.set()
                    while not worker.wait(0) and time.monotonic() < end:
                        self.app.processEvents()
                        time.sleep(.002)
                    self.assertTrue(worker.wait(0))
                    self.app.processEvents()
                    self.assertEqual(results[0]['downloaded'], 0)
            finally:
                timer.stop()
                release.set()
                worker.wait(2000)
                worker.deleteLater()
                self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_reference_preflight_failure_is_a_result_not_an_escaping_exception(self):
        from model import TaskReferenceDownloader as references
        worker = references.TaskReferenceDownloadThread([], tasks=[task()], root_dir='synthetic')
        results, logs = [], []
        worker.completed.connect(results.append)
        worker.log.connect(logs.append)
        with patch.object(references, 'build_task_reference_jobs', side_effect=OSError('slow disk unavailable')):
            worker.run()
        self.assertEqual(results[0]['failed'], 1)
        self.assertTrue(any('不受影响' in message for message in logs))
        worker.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_old_reference_completion_cannot_refresh_new_project_tree(self):
        from PYUI.main_pyui import MainDialog
        from types import SimpleNamespace
        tree = Mock()
        host = SimpleNamespace(appendLog=Mock(), task_reference_download_root=Path('old'),
                               loaded_project_dir=Path('new'), file_explorer_tree_view=tree)
        MainDialog.onTaskReferenceDownloadCompleted(host,
            {'downloaded': 0, 'repaired': 0, 'cached': 1, 'skipped': 0, 'failed': 0})
        tree.load_directory.assert_not_called()

    def test_pending_reference_project_starts_only_if_still_current(self):
        from PYUI.main_pyui import MainDialog
        from types import SimpleNamespace
        for current, calls in ((Path('new'), 1), (Path('other'), 0)):
            host = SimpleNamespace(task_reference_download_thread=Mock(), task_reference_download_root=Path('old'),
                _pending_task_reference_project=Path('new'), loaded_project_dir=current,
                startTaskReferenceDownloads=Mock())
            MainDialog.onTaskReferenceDownloadFinished(host)
            self.assertEqual(host.startTaskReferenceDownloads.call_count, calls)
            self.assertIsNone(host._pending_task_reference_project)

    def test_validated_monitor_target_does_not_resolve_disk_on_gui(self):
        from model.GoogleSheetMonitor import GoogleSheetMonitorThread
        worker = GoogleSheetMonitorThread({})
        absolute = Path.cwd() / 'already-validated-project'
        with patch.object(Path, 'resolve', side_effect=AssertionError('GUI filesystem lookup forbidden')):
            worker.set_project_dir(absolute, validated=True)
            self.assertEqual(worker._project_snapshot(), absolute)
        worker.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
