"""Never shut down the machine: the OS boundary is mocked in every OS test."""
import ast
import copy
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from model.CompletionShutdown import CompletionShutdown, request_windows_shutdown
from model.BackgroundTasks import ACTIVE, BackgroundTaskManager
from PYUI.background_tasks_pyui import BackgroundTasksWidget


class FakeManager(QtCore.QObject):
    changed = QtCore.pyqtSignal(str)
    idle = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.rows, self.notifying, self.accepting = {}, False, True
        self.flush_history = Mock(return_value=True)

    def records(self): return copy.deepcopy(list(self.rows.values()))
    def completion_states(self): return {key: row['status'] for key, row in self.rows.items()}
    def is_busy(self): return any(row['status'] in ACTIVE for row in self.rows.values())
    def is_settled(self): return not self.is_busy() and not self.notifying
    def is_accepting(self): return self.accepting
    def stop_accepting(self): self.accepting = False
    def resume_accepting(self): self.accepting = True

    def set(self, key, status):
        self.rows[key] = {'id': key, 'status': status}
        self.changed.emit(key)


class CompletionShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.manager = FakeManager()
        self.now = [100.0]
        self.close = Mock(return_value=True)
        self.ready = Mock(return_value='')
        self.controller = CompletionShutdown(self.manager, self.close, ready_check=self.ready,
                                             clock=lambda: self.now[0])
        self.addCleanup(self.dispose)

    def dispose(self):
        self.controller.timer.stop()
        self.controller.deleteLater(); self.manager.deleteLater(); self.app.processEvents()

    def finish_job(self):
        self.manager.set('one', 'queued'); self.controller.arm()
        self.manager.set('one', 'succeeded')
        self.assertEqual(self.controller.remaining, 60)

    def test_opt_in_is_session_only_and_empty_queue_does_not_shut_down(self):
        self.assertFalse(self.controller.armed)
        self.manager.set('old', 'failed')
        self.controller.arm(); self.now[0] += 600; self.controller.refresh()
        self.assertTrue(self.controller.armed); self.assertIsNone(self.controller.deadline)
        self.close.assert_not_called()
        fresh = CompletionShutdown(self.manager, self.close)
        self.assertFalse(fresh.armed); fresh.deleteLater()

    def test_historical_failure_does_not_block_current_success_and_request_is_deferred(self):
        self.manager.set('old', 'failed'); self.finish_job()
        with patch('model.CompletionShutdown.request_windows_shutdown') as shutdown:
            self.now[0] += 60; self.controller.refresh(); self.controller.refresh()
        self.close.assert_called_once(); self.manager.flush_history.assert_called_once()
        self.assertTrue(self.controller.requested); self.assertFalse(self.controller.armed)
        shutdown.assert_not_called()  # main() owns it, after all cleanup.

    def test_follow_up_result_callbacks_and_new_jobs_reset_countdown(self):
        self.manager.set('one', 'running'); self.controller.arm()
        self.manager.notifying = True; self.manager.set('one', 'succeeded')
        self.assertIsNone(self.controller.deadline)
        self.manager.set('two', 'queued'); self.manager.notifying = False
        self.controller.refresh(); self.assertIsNone(self.controller.deadline)
        self.manager.set('two', 'succeeded'); self.assertEqual(self.controller.remaining, 60)
        self.now[0] += 55
        self.manager.set('three', 'queued'); self.assertIsNone(self.controller.deadline)
        self.manager.set('three', 'succeeded'); self.assertEqual(self.controller.remaining, 60)
        self.now[0] += 59; self.controller.refresh(); self.close.assert_not_called()
        self.now[0] += 1; self.controller.refresh(); self.close.assert_called_once()

    def test_default_failure_or_cancellation_disarms(self):
        for status in ('failed', 'canceled', 'interrupted'):
            with self.subTest(status=status):
                self.manager.set(status, 'queued'); self.controller.arm()
                self.manager.set(status, status)
                self.assertFalse(self.controller.armed); self.assertFalse(self.controller.requested)
        self.close.assert_not_called()

    def test_explicit_all_ended_mode_allows_failure_and_cancel(self):
        self.manager.set('one','queued'); self.manager.set('two','queued')
        self.controller.arm(allow_failed=True)
        self.manager.set('one','failed'); self.assertIsNone(self.controller.deadline)
        self.manager.set('two','canceled'); self.now[0] += 60; self.controller.refresh()
        self.assertTrue(self.controller.requested)

    def test_manual_cancel_only_disables_shutdown_not_jobs(self):
        self.manager.set('one','running'); self.controller.arm(); self.controller.cancel()
        self.assertEqual(self.manager.rows['one']['status'],'running')
        self.manager.set('one','succeeded'); self.now[0] += 120; self.controller.refresh()
        self.close.assert_not_called(); self.assertFalse(self.controller.requested)

    def test_cancel_during_zero_second_notification_still_prevents_close(self):
        self.finish_job()
        def cancel_at_zero():
            if self.controller.deadline is not None and self.controller.remaining == 0:
                self.controller.cancel()
        self.controller.changed.connect(cancel_at_zero)
        self.now[0] += 60; self.controller.refresh()
        self.close.assert_not_called(); self.assertFalse(self.controller.requested)

    def test_modal_dialog_waits_and_gets_fresh_countdown(self):
        self.finish_job(); self.ready.return_value = '模态对话框'
        self.now[0] += 60; self.controller.refresh()
        self.close.assert_not_called(); self.assertIsNone(self.controller.deadline)
        self.ready.return_value = ''; self.controller.refresh()
        self.assertEqual(self.controller.remaining,60)

    def test_record_save_or_safe_close_failure_cancels_and_restores_queue(self):
        self.finish_job(); self.manager.flush_history.return_value = False
        self.now[0] += 60; self.controller.refresh()
        self.close.assert_not_called(); self.assertFalse(self.controller.requested)
        self.assertTrue(self.manager.accepting)
        self.manager.flush_history.return_value = True; self.close.return_value = False
        self.manager.set('two','queued'); self.controller.arm(); self.manager.set('two','succeeded')
        self.now[0] += 60; self.controller.refresh()
        self.assertFalse(self.controller.requested); self.assertTrue(self.manager.accepting)

    def test_removed_completed_cards_do_not_remove_completion_evidence(self):
        self.finish_job(); del self.manager.rows['one']; self.manager.changed.emit('one')
        self.now[0] += 60; self.controller.refresh(); self.assertTrue(self.controller.requested)

    def test_real_manager_waits_for_chained_work_and_worker_cleanup(self):
        manager = BackgroundTaskManager()
        controller = CompletionShutdown(manager, self.close, clock=lambda: self.now[0])
        try:
            controller.arm()
            first = manager.submit(owner='test', title='first', runner=lambda *_: {'paths':[]})
            first.completed.connect(lambda _: manager.submit(owner='test',title='followup',runner=lambda *_:None))
            until=time.monotonic()+5
            while (len(manager.records()) < 2 or not manager.is_settled()) and time.monotonic()<until:
                self.app.processEvents(); time.sleep(.002)
            self.app.processEvents()
            self.assertEqual(len(manager.records()),2); self.assertTrue(manager.is_settled())
            self.assertEqual(controller.remaining,60)
            self.now[0] += 60; controller.refresh(); self.assertTrue(controller.requested)
        finally:
            controller.timer.stop(); manager.shutdown(); controller.deleteLater(); manager.deleteLater()
            self.app.processEvents()

    def test_ui_confirmation_cancel_dialog_and_new_task_do_not_disarm_each_other(self):
        manager = BackgroundTaskManager()
        release = threading.Event()
        owner = QtWidgets.QWidget()
        controller = CompletionShutdown(manager, self.close, owner, clock=lambda: self.now[0])
        view = BackgroundTasksWidget(manager, owner, shutdown_controller=controller)
        owner.show()
        try:
            with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.No):
                view.shutdown_enabled.setChecked(True)
            self.assertFalse(controller.armed); self.assertFalse(view.shutdown_enabled.isChecked())
            with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                view.shutdown_enabled.setChecked(True)
            self.assertTrue(controller.armed)
            manager.submit(owner='test',title='one',runner=lambda *_:None)
            until=time.monotonic()+5
            while manager.is_busy() and time.monotonic()<until:
                self.app.processEvents(); time.sleep(.002)
            self.app.processEvents()
            self.assertIsNotNone(controller.deadline); self.assertTrue(view.shutdown_dialog.isVisible())
            manager.submit(owner='test',title='two',runner=lambda *_:release.wait(2))
            self.assertTrue(controller.armed); self.assertFalse(view.shutdown_dialog.isVisible())
            release.set()
            until=time.monotonic()+5
            while manager.is_busy() and time.monotonic()<until:
                self.app.processEvents(); time.sleep(.002)
            self.app.processEvents(); self.assertTrue(view.shutdown_dialog.isVisible())
            view.shutdown_dialog.reject()
            self.assertFalse(controller.armed); self.assertFalse(view.shutdown_enabled.isChecked())
        finally:
            release.set(); controller.cancel(); manager.cancel_all()
            until=time.monotonic()+5
            while manager.is_busy() and time.monotonic()<until:
                self.app.processEvents(); time.sleep(.002)
            manager.shutdown(); owner.close(); owner.deleteLater(); manager.deleteLater(); self.app.processEvents()

    def test_os_boundary_never_forces_or_schedules_uncancelable_os_timeout(self):
        def system_directory(buffer, _length):
            buffer.value = r'C:\Windows\System32'; return len(buffer.value)
        with patch('model.CompletionShutdown.ctypes.windll.kernel32.GetSystemDirectoryW',side_effect=system_directory), \
             patch('model.CompletionShutdown.subprocess.run',return_value=Mock(returncode=0)) as run:
            request_windows_shutdown()
        self.assertEqual(run.call_args.args[0],[r'C:\Windows\System32\shutdown.exe','/s','/t','0'])
        self.assertFalse(run.call_args.kwargs['shell'])
        with patch('model.CompletionShutdown.subprocess.run',return_value=Mock(returncode=5)), \
             self.assertRaisesRegex(OSError,'Windows未接受关机请求'):
            request_windows_shutdown()

    def test_os_action_occurs_after_qt_logs_and_storage_release_in_entrypoint(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root/'main.py').read_text(encoding='utf8'))
        function = next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='main')
        calls = [node for node in ast.walk(function) if isinstance(node,ast.Call)]
        request = next(n.lineno for n in calls if isinstance(n.func,ast.Name) and n.func.id=='request_windows_shutdown')
        close = next(n.lineno for n in calls if isinstance(n.func,ast.Attribute) and isinstance(n.func.value,ast.Name)
                     and n.func.value.id=='storage' and n.func.attr=='close')
        logs = next(n.lineno for n in calls if isinstance(n.func,ast.Name) and n.func.id=='shutdown_application_logging')
        qt = next(n.lineno for n in calls if isinstance(n.func,ast.Attribute) and isinstance(n.func.value,ast.Name)
                  and n.func.value.id=='sip' and n.func.attr=='delete')
        self.assertGreater(request, close); self.assertGreater(request,logs); self.assertGreater(request,qt)

    def test_main_exit_quits_only_if_normal_close_was_accepted(self):
        from types import SimpleNamespace
        from PYUI.main_pyui import MainDialog
        window = SimpleNamespace(close=Mock(return_value=False))
        app = Mock()
        with patch.object(QtWidgets.QApplication,'instance',return_value=app):
            self.assertFalse(MainDialog.requestCompletionShutdown(window)); app.quit.assert_not_called()
            window.close.return_value = True
            self.assertTrue(MainDialog.requestCompletionShutdown(window)); app.quit.assert_called_once()
