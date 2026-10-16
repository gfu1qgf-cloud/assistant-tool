import ast
import inspect
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch
from PyQt6 import sip
from qt_compat import QtCore, QtWidgets
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active
from app_plugins.host import PluginHost
from app_plugins.builtin.task_delivery_controller import TaskDeliveryController
from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
from PYUI.main_pyui import MainDialog
from app_plugins.builtin.audio_splitter import AudioSplitterPlugin
from app_plugins.builtin.music_ducker import MusicDuckerPlugin
from app_plugins.builtin.codex_account_switcher.dialog import AccountSwitcherDialog
from app_plugins.builtin.material_organizer.ui import MaterialOrganizerDialog


def source_root():
    root = Path(__file__).resolve().parents[1]
    # An isolated test overlay may contain only the files being patched. Never
    # let a project-wide boundary check silently scan an empty partial tree.
    if not (root / 'app_plugins/host.py').is_file():
        root = Path(inspect.getfile(PluginHost)).resolve().parents[1]
    if not (root / 'PYUI/main_pyui.py').is_file():
        raise AssertionError('Full application source tree is required for boundary checks')
    return root


class WaitingThread(QtCore.QThread):
    def __init__(self, parent, early_finished=False):
        super().__init__(parent)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.early_finished = early_finished
    def run(self):
        self.entered.set()
        if self.early_finished:
            self.finished.emit()  # Exercises native-finish lag deterministically.
        self.release.wait(3)


class ThreadLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.window = QtWidgets.QWidget()
        self.threads = []
    def tearDown(self):
        for thread in self.threads:
            if not sip.isdeleted(thread):
                thread.release.set()
                self.assertTrue(thread.wait(1000))
        self.app.processEvents()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    def waiting(self, early=False):
        thread = WaitingThread(self.window, early)
        self.threads.append(thread)
        return thread
    def poll(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.app.processEvents()
        self.assertTrue(predicate())

    def test_100_restarts_do_not_delete_current_generation(self):
        owner = SimpleNamespace(worker=None)
        old_callback = Mock()
        for _ in range(100):
            old = self.waiting()
            owner.worker = old
            connect_thread_finished(old, owner, 'worker', old_callback)
            old.start()
            self.assertTrue(old.entered.wait(1))
            old.release.set()
            self.assertTrue(old.wait(1000))
            new = self.waiting()
            owner.worker = new
            new.start()
            self.assertTrue(new.entered.wait(1))
            self.app.processEvents()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            self.assertIs(owner.worker, new)
            self.assertTrue(thread_is_active(new))
            self.assertTrue(sip.isdeleted(old))
            new.release.set()
            self.assertTrue(new.wait(1000))
            new.deleteLater()
        old_callback.assert_not_called()

    def test_native_join_and_gui_callback_are_required(self):
        owner = SimpleNamespace(worker=self.waiting(early=True))
        observed = []
        thread = owner.worker
        connect_thread_finished(thread, owner, 'worker', lambda: observed.append(QtCore.QThread.currentThread()))
        thread.start()
        self.assertTrue(thread.entered.wait(1))
        for _ in range(10):
            self.app.processEvents()
            time.sleep(.002)
        self.assertEqual(observed, [])
        self.assertIs(owner.worker, thread)
        self.assertFalse(sip.isdeleted(thread))
        thread.release.set()
        self.poll(lambda: bool(observed))
        self.assertEqual(observed, [self.app.thread()])
        self.assertIsNone(owner.worker)

    def test_callback_exception_is_logged_not_raised_into_qt(self):
        owner = SimpleNamespace(worker=self.waiting())
        thread = owner.worker
        def fail():
            raise ValueError('synthetic finish failure')
        connect_thread_finished(thread, owner, 'worker', fail)
        with self.assertLogs('assistant_tool', level='ERROR') as logs:
            thread.start()
            thread.release.set()
            self.poll(lambda: owner.worker is None)
        self.assertIn('synthetic finish failure', '\n'.join(logs.output))

    def test_result_signal_does_not_release_material_worker(self):
        thread = self.waiting()
        owner = SimpleNamespace(worker=thread, _refresh_timer=Mock(), progress_bar=Mock(),
            refresh_categories=Mock(), refresh_assets=Mock(), status_label=Mock(), logger=Mock(),
            _set_controls_enabled=Mock())
        thread.start()
        self.assertTrue(thread.entered.wait(1))
        MaterialOrganizerDialog._analysis_completed(owner, {'completed': 1, 'skipped': 0, 'failed': 0})
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.assertIs(owner.worker, thread)
        self.assertFalse(sip.isdeleted(thread))
        self.assertTrue(thread_is_active(thread))
        connect_thread_finished(thread, owner, 'worker', lambda: MaterialOrganizerDialog._analysis_finished(owner))
        thread.release.set()
        self.poll(lambda: owner.worker is None)
        owner._set_controls_enabled.assert_called_once_with(True)

    def test_stop_timeout_keeps_audio_thread_and_window(self):
        plugin = AudioSplitterPlugin()
        plugin.worker = Mock()
        plugin.worker.isRunning.return_value = True
        plugin.worker.wait.return_value = False
        plugin.dialog = Mock()
        plugin.context = SimpleNamespace(log=Mock())
        old, dialog = plugin.worker, plugin.dialog
        self.assertFalse(plugin.stop())
        self.assertIs(plugin.worker, old)
        self.assertIs(plugin.dialog, dialog)
        dialog.deleteLater.assert_not_called()

    def test_stop_timeout_keeps_music_ducker_thread(self):
        plugin = MusicDuckerPlugin()
        plugin.thread = Mock()
        plugin.thread.isRunning.return_value = True
        plugin.thread.wait.return_value = False
        plugin.context = SimpleNamespace(log=Mock())
        with patch.object(plugin, '_sync_controls'):
            old = plugin.thread
            self.assertFalse(plugin.stop())
        self.assertIs(plugin.thread, old)

    def test_quota_timeout_retains_retired_threads(self):
        worker = Mock()
        worker.isRunning.return_value = True
        owner = SimpleNamespace(_stop_quota_worker=Mock(), _lingering_workers=[worker])
        self.assertFalse(AccountSwitcherDialog.shutdown(owner))
        self.assertEqual(owner._lingering_workers, [worker])

    def test_review_stop_joins_even_when_running_flag_is_already_false(self):
        worker = self.waiting()
        worker.stop = worker.release.set
        controller = TaskDeliveryController(SimpleNamespace(quick_actions=None),
            SimpleNamespace(parent_widget=self.window, log=Mock()))
        controller.review_status_thread = worker
        worker.start()
        self.assertTrue(worker.entered.wait(1))
        # Native TLS cleanup can outlast isRunning(). Reproduce that edge with
        # a real live thread rather than assuming the flag means safe to delete.
        with patch.object(worker, 'isRunning', return_value=False):
            self.assertTrue(controller.stop_review_status_monitor(wait_ms=1000))
        self.assertTrue(worker.release.is_set())
        self.assertIsNone(controller.review_status_thread)

    def test_delivery_stop_failure_propagates_and_retains_hotkey(self):
        owner = SimpleNamespace(can_close=Mock(return_value=(True, '')),
            stop_review_status_monitor=Mock(return_value=False), context=SimpleNamespace(log=Mock()),
            hotkey_manager=Mock(), _sync_compatibility_state=Mock())
        self.assertFalse(TaskDeliveryController.stop(owner))
        owner.hotkey_manager.close.assert_not_called()
        plugin = SimpleNamespace(controller=SimpleNamespace(stop=Mock(return_value=False)))
        self.assertFalse(TaskDeliveryPlugin.stop(plugin))

    def test_thread_registration_outside_gui_thread_is_rejected(self):
        errors = []
        owner = SimpleNamespace(worker=self.waiting())
        def connect():
            try:
                connect_thread_finished(owner.worker, owner, 'worker', lambda: None)
            except RuntimeError as error:
                errors.append(str(error))
        native = threading.Thread(target=connect)
        native.start()
        native.join(1)
        self.assertEqual(len(errors), 1)

    def test_host_retains_plugin_when_stop_returns_false(self):
        self.window.load_config = lambda: {}
        self.window.appendLog = Mock()
        host = PluginHost(self.window)
        plugin = SimpleNamespace(plugin_id='slow_test', register=lambda context: None,
                                 start=Mock(), stop=Mock(return_value=False))
        host.install(plugin)
        host.start_all()
        background = host._background_tasks = Mock()
        self.assertFalse(host.stop_all())
        self.assertIn('slow_test', host._started)
        background.shutdown.assert_not_called()
        host.start_all()
        plugin.start.assert_called_once()
        plugin.stop.return_value = True
        background.shutdown.return_value = True
        self.assertTrue(host.stop_all())
        self.assertNotIn('slow_test', host._started)

    def test_host_stop_exception_does_not_allow_exit(self):
        self.window.load_config = lambda: {}
        self.window.appendLog = Mock()
        host = PluginHost(self.window)
        plugin = SimpleNamespace(plugin_id='bad_stop_test', register=lambda context: None,
                                 stop=Mock(side_effect=RuntimeError('synthetic stop failure')))
        host.install(plugin)
        host.start_all()
        self.assertFalse(host.stop_all())
        self.assertIn('bad_stop_test', host._started)
        self.assertIn('synthetic stop failure', self.window.appendLog.call_args.args[0])

    def test_main_window_is_preserved_when_plugin_cannot_stop(self):
        owner = SimpleNamespace(
            background_tasks=Mock(), plugin_host=Mock(),
            oral_duration_check_thread=None, task_submission_audit_thread=None,
            task_reference_download_thread=None, task_result_thread=None,
            assign_video_thread=None, load_task_hotkey_manager=Mock(),
            stopGoogleSheetMonitor=Mock(return_value=True),
            stopFlowParameterGuard=Mock(return_value=True),
            stopReviewStatusMonitor=Mock(return_value=True),
            saveCurrentConfig=Mock(return_value=True),
            startGoogleSheetMonitor=Mock(), startFlowParameterGuard=Mock(),
            startReviewStatusMonitor=Mock(),
        )
        owner.background_tasks.is_busy.return_value = False
        owner.background_tasks.is_accepting.return_value = True
        owner.plugin_host.can_close_all.return_value = True, ''
        owner.plugin_host.stop_all.return_value = False
        event = Mock()
        with patch('PYUI.main_pyui.QMessageBox.warning') as warning:
            MainDialog.closeEvent(owner, event)
        event.ignore.assert_called_once()
        owner.plugin_host.start_all.assert_called_once()
        owner.background_tasks.resume_accepting.assert_called_once()
        owner.load_task_hotkey_manager.close.assert_not_called()
        warning.assert_called_once()

    def test_owned_threads_cannot_add_unqualified_finished_cleanup(self):
        # Reviewed exceptions are QProcess, host task handles, QRunnable signals,
        # or native owners that already join and bind the exact worker identity.
        approved = {
            # GUI-owned nonmodal QMessageBox; not a worker/QThread signal.
            'PYUI/background_tasks_pyui.py': {'box.finished.connect'},
            # Drive browsing submits through PluginContext; this is likewise a
            # manager-owned QObject handle, covered by drive queue/UI tests.
            'app_plugins/builtin/google_drive_browser/ui.py': {'handle.finished.connect'},
            # Host-owned BackgroundTaskHandle (QObject), not a dialog-owned QThread;
            # AI client tests verify GUI callbacks, cancellation and hidden-window completion.
            'app_plugins/builtin/ai_model_client/ui.py': {'handle.finished.connect'},
            # These migrated jobs expose manager-owned QObject handles; the host
            # joins their native QThread before emitting handle.finished.
            'app_plugins/builtin/task_audio_subtitle.py': {'handle.finished.connect'},
            'app_plugins/builtin/inventory.py': {'handle.finished.connect'},
            'app_plugins/builtin/audio_splitter.py': {'handle.finished.connect'},
            'app_plugins/builtin/batch_text_video/ui.py': {'handle.finished.connect'},
            'app_plugins/builtin/video_stitch/ui.py': {'worker.finished.connect'},
            # box is the GUI-owned, nonmodal material-error QMessageBox.
            'PYUI/utility_managers_pyui.py': {'handle.finished.connect','box.finished.connect'},
            # Reviewed: all three execution fields are returned by the host's
            # BackgroundTaskManager.submit (not plugin-owned QThreads). The
            # host joins native workers before emitting their handle.finished.
            # Preview dialogs are GUI-owned QDialogs, also not QThreads.
            'PYUI/node_workspace.py': {'self.handle.finished.connect','handle.finished.connect',
                                      'self.preview_handle.finished.connect','dialog.finished.connect'},
            # QDialog.finished, released by the GUI-thread details owner.
            'app_plugins/builtin/waste_reminder/plugin.py': {'dialog.finished.connect'},
            'app_plugins/builtin/davinci_remote/ui.py': {'process.finished.connect'},
            'app_plugins/builtin/facebook_contact_sheet/ui.py': {'self._worker.finished.connect'},
            'app_plugins/builtin/script_segmenter/ui.py': {'self.handle.finished.connect'},
            # Video indexing also submits through PluginContext to the host.
            'app_plugins/builtin/smart_search/video_ui.py': {'self.worker.finished.connect'},
            # Host-owned handle; QuickCopyController is a GUI-thread QObject.
            # Tests cover main-thread clipboard publication and late cancellation.
            'app_plugins/builtin/script_segmenter/quick_copy.py': {'self.handle.finished.connect'},
            'model/MediaPlayback.py': {'self.process.finished.connect'},
            'app_plugins/builtin/smart_video_editor/plugin.py': {'handle.finished.connect'},
            # Explicit isinstance(BackgroundTaskHandle) guard before connecting;
            # current-task refresh uses host-owned QObject completion, not a QThread.
            # Refresh tests cover cancellation, native dialog deletion and GUI delivery.
            'app_plugins/builtin/smart_video_editor/ui.py': {'handle.finished.connect'},
            'app_plugins/builtin/smart_video_editor/waveform.py': {'self.process.finished.connect'},
            'app_plugins/builtin/task_delivery.py': {'thread.finished.connect'},
            'app_plugins/builtin/task_delivery_board.py': {'worker.signals.finished.connect', 'dialog.finished.connect'},
            'app_plugins/builtin/task_delivery_controller.py': {'thread.finished.connect', 'handle.finished.connect'},
            'model/BackgroundTasks.py': {'worker.finished.connect'},
            'model/QtThreadLifecycle.py': {'thread.finished.connect'},
            # Native QProcess lifetimes, not QThread completion. Process tests
            # exercise timeout/cancel and asynchronous reaping before deletion.
            'model/MediaThumbnail.py': {'self.process.finished.connect', 'job.finished.connect'},
            'model/QtProcessLifecycle.py': {'process.finished.connect'},
        }
        root = source_root()
        for directory in ('PYUI', 'app_plugins', 'model'):
            for path in (root / directory).rglob('*.py'):
                relative = path.relative_to(root).as_posix()
                tree = ast.parse(path.read_text(encoding='utf-8-sig'))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    target = ast.unparse(node.func)
                    if target.endswith('.finished.connect'):
                        self.assertIn(target, approved.get(relative, set()),
                            f'{relative}:{node.lineno}: use connect_thread_finished for owned QThread; '
                            'review explicit non-QThread exceptions instead of bypassing this check')

    def test_workers_do_not_directly_create_gui_or_pump_gui_events(self):
        root = source_root()
        for directory in ('PYUI', 'app_plugins', 'model'):
            for path in (root / directory).rglob('*.py'):
                tree = ast.parse(path.read_text(encoding='utf-8-sig'))
                for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
                    if not any(ast.unparse(base).endswith(('QThread', 'QRunnable', 'Thread')) for base in cls.bases):
                        continue
                    for method in cls.body:
                        if not isinstance(method, ast.FunctionDef) or method.name not in ('run', '__init__'):
                            continue
                        for call in (node for node in ast.walk(method) if isinstance(node, ast.Call)):
                            target = ast.unparse(call.func)
                            self.assertFalse(any(word in target for word in
                                ('QMessageBox', 'QPixmap', 'QWidget', 'processEvents')),
                                f'{path.relative_to(root)}:{call.lineno}: GUI work belongs on the main thread')


if __name__ == '__main__':
    unittest.main()
