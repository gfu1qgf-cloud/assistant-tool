import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from model.BackgroundTasks import BackgroundTaskManager, directory_resource
from PYUI.background_tasks_pyui import BackgroundTasksWidget


class BackgroundTasksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.logs = []
        self.manager = BackgroundTaskManager(logger=lambda *args: self.logs.append(args))

    def tearDown(self):
        self.manager.cancel_all()
        self.wait_until(lambda: not self.manager.is_busy())
        self.manager.shutdown()
        self.manager.deleteLater()
        self.app.processEvents()

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()
        self.assertTrue(predicate(), "background task timed out")

    def submit(self, runner, **kwargs):
        return self.manager.submit(owner="test", title="test task", runner=runner, **kwargs)

    def test_item_phase_is_separate_from_batch_percent_and_delivered_on_gui(self):
        release = threading.Event()
        def run(_payload, context):
            context.report('文案视频 · 已结束 0/2', 0)
            context.report_item_stage('one', '等待字幕模型')
            release.wait(2)
            return {}
        handle = self.submit(run, payload={})
        phases = []
        handle.itemStage.connect(lambda identifier, phase: phases.append((identifier, phase, threading.get_ident())))
        self.wait_until(lambda: bool(phases))
        try:
            self.assertEqual(phases, [('one', '等待字幕模型', threading.get_ident())])
            self.assertEqual(handle.item_stages['one'], '等待字幕模型')
            row = self.manager.get(handle.task_id)
            self.assertEqual(row['percent'], 0)
            self.assertEqual(row['stage'], '文案视频 · 已结束 0/2')
        finally: release.set()

    def test_parallel_snapshot_main_thread_results_and_completion_100(self):
        release = threading.Event()
        started = []
        payload = {"list": [1]}
        def runner(data, context):
            started.append(threading.get_ident())
            context.report("working", 100)
            release.wait(1)
            return data
        handles = [self.submit(runner, payload=payload, kind="media") for _ in range(2)]
        result = []
        handles[0].completed.connect(lambda value: result.append((threading.get_ident(), value)))
        payload["list"].append(2)
        self.wait_until(lambda: len(started) == 2)
        self.assertTrue(all(ident != threading.get_ident() for ident in started))
        self.assertEqual(self.manager.get(handles[0].task_id)["percent"], 99)
        release.set()
        self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(result, [(threading.get_ident(), {"list": [1]})])
        self.assertTrue(all(self.manager.get(handle.task_id)["percent"] == 100 for handle in handles))

    def test_resources_serialize_writers_but_unrelated_work_proceeds(self):
        release = threading.Event()
        entered = []
        def runner(data, _context):
            entered.append(data)
            if data == "first":
                release.wait(1)
        first = self.submit(runner, payload="first", kind="media", resources=["directory:a"])
        second = self.submit(runner, payload="second", kind="media", resources=["directory:a"])
        self.submit(runner, payload="other", kind="media", resources=["directory:b"])
        self.wait_until(lambda: "other" in entered)
        self.assertNotIn("second", entered)
        self.assertIn("共用资源", self.manager.waiting_reason(second.task_id))
        release.set()
        self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(self.manager.get(first.task_id)["status"], "succeeded")
        self.assertIn("second", entered)

    def test_waiting_reason_distinguishes_concurrency_limit_from_resource_lock(self):
        release = threading.Event()
        def runner(*_): release.wait(2)
        first = self.submit(runner,kind='media',resources=['directory:a'])
        second = self.submit(runner,kind='media',resources=['directory:b'])
        third = self.submit(runner,kind='media',resources=['directory:c'])
        same = self.submit(runner,kind='media',resources=['directory:a'])
        try:
            self.wait_until(lambda: self.manager.get(second.task_id)['status'] == 'running')
            reason = self.manager.waiting_reason(third.task_id)
            self.assertIn('媒体处理并发名额（2/2）',reason)
            self.assertIn('不是资源互斥',reason)
            self.assertIn('同一输出目录',self.manager.waiting_reason(same.task_id))
        finally:
            release.set(); self.wait_until(lambda: not self.manager.is_busy())

    def test_exclusive_job_is_a_fair_barrier(self):
        first_release, exclusive_release = threading.Event(), threading.Event()
        entered = []
        def runner(data, _context):
            entered.append(data)
            if data == "first": first_release.wait(1)
            if data == "exclusive": exclusive_release.wait(1)
        self.submit(runner, payload="first", kind="network")
        self.submit(runner, payload="exclusive", execution="exclusive")
        self.submit(runner, payload="last", kind="network")
        self.wait_until(lambda: entered == ["first"])
        first_release.set()
        self.wait_until(lambda: "exclusive" in entered)
        self.assertNotIn("last", entered)
        exclusive_release.set()
        self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(entered, ["first", "exclusive", "last"])

    def test_cancel_all_does_not_start_queued_work(self):
        def runner(_data, context):
            while not context.is_canceled(): time.sleep(.005)
            context.check_canceled()
        first = self.submit(runner)
        queued_called = []
        second = self.submit(lambda *_args: queued_called.append(True))
        self.wait_until(lambda: self.manager.get(first.task_id)["status"] == "running")
        self.manager.cancel_all()
        self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(queued_called, [])
        self.assertEqual(self.manager.get(second.task_id)["status"], "canceled")

    def test_cancel_after_native_worker_finishes_before_gui_callback_is_respected(self):
        handle = self.submit(lambda *_args: {'value': 'already calculated'})
        self.manager._dispatch()
        worker = self.manager._workers[handle.task_id]
        self.assertTrue(worker.wait(2000))
        # Do not deliver the queued outcome/finished signals until after cancel.
        self.assertEqual(self.manager.get(handle.task_id)['status'], 'running')
        self.manager.cancel(handle.task_id)
        self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(self.manager.get(handle.task_id)['status'], 'canceled')

    def test_failure_logs_full_trace_and_retry_is_opt_in(self):
        attempts = []
        def runner(_payload, _context):
            attempts.append(True)
            if len(attempts) == 1: raise ValueError("test failure")
            return {"paths": []}
        handle = self.submit(runner, retryable=True)
        self.wait_until(lambda: not self.manager.is_busy())
        record = self.manager.get(handle.task_id)
        self.assertEqual(record["status"], "failed")
        self.assertIn("Traceback", record["traceback"])
        self.assertTrue(any("test failure" in message for message, _ in self.logs))
        retried = self.manager.retry(handle.task_id)
        self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(self.manager.get(retried.task_id)["status"], "succeeded")
        denied = self.submit(lambda *_args: (_ for _ in ()).throw(ValueError("no retry")))
        self.wait_until(lambda: not self.manager.is_busy())
        with self.assertRaises(ValueError): self.manager.retry(denied.task_id)

    def test_bad_plugin_progress_or_result_is_a_task_failure_not_a_gui_exception(self):
        def progress(_data, context): context.report("bad value", "not a number")
        handles = [self.submit(progress), self.submit(lambda *_args: {"paths": None})]
        self.wait_until(lambda: not self.manager.is_busy())
        self.assertTrue(all(self.manager.get(handle.task_id)["status"] == "failed" for handle in handles))

    def test_dedup_reject_qobjects_and_shutdown_gate(self):
        runner = lambda *_args: None
        first = self.submit(runner, dedup_key="same")
        self.assertIs(first, self.submit(runner, dedup_key="same"))
        with self.assertRaises(TypeError): self.submit(runner, payload={"widget": QtWidgets.QWidget()})
        self.manager.stop_accepting()
        with self.assertRaises(RuntimeError): self.submit(runner)
        self.manager.resume_accepting()
        self.manager.cancel_all()
        self.assertEqual(directory_resource("."), directory_resource(Path.cwd()))

    def test_history_no_payload_secrets_and_interrupted_jobs_are_not_resumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            manager = BackgroundTaskManager(history_path=path)
            handle = manager.submit(owner="demo", title="safe title", runner=lambda *_args: None,
                                    payload={"key": "secret-not-persisted"})
            manager.flush_history()
            self.assertNotIn("secret-not-persisted", path.read_text(encoding="utf-8"))
            restored = BackgroundTaskManager(history_path=path)
            self.assertEqual(restored.get(handle.task_id)["status"], "interrupted")
            self.assertFalse(restored.is_busy())
            restored.shutdown()
            manager.cancel_all()
            manager.shutdown()
            restored.deleteLater()
            manager.deleteLater()

    def test_error_secrets_are_redacted_without_corrupting_history_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.manager._history_path = Path(temporary) / "history.json"
            def runner(*_args): raise ValueError('api_key="secret-value"')
            handle = self.submit(runner)
            self.wait_until(lambda: not self.manager.is_busy())
            text = self.manager._history_path.read_text(encoding="utf-8")
            rows = json.loads(text)
            self.assertNotIn("secret-value", text)
            self.assertIn("[REDACTED]", rows[0]["error"])
            self.assertIn("[REDACTED]", self.manager.get(handle.task_id)["traceback"])
            self.manager._history_path = None

    def test_corrupt_history_is_preserved_before_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            path.write_text("broken json", encoding="utf-8")
            manager = BackgroundTaskManager(history_path=path)
            manager.flush_history()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])
            backup = list(path.parent.glob("history.json.invalid-*"))
            self.assertEqual(len(backup), 1)
            self.assertEqual(backup[0].read_text(encoding="utf-8"), "broken json")
            manager.shutdown()
            manager.deleteLater()

    def test_widget_completion_auto_hides_and_failure_remains_visible(self):
        view = BackgroundTasksWidget(self.manager)
        success = self.submit(lambda *_args: None)
        failure = self.submit(lambda *_args: (_ for _ in ()).throw(ValueError("failed")))
        self.wait_until(lambda: not self.manager.is_busy())
        self.manager._records[success.task_id]["finished_at"] = time.time() - 10
        view.refresh()
        self.assertTrue(view._cards[success.task_id].isHidden())
        self.assertFalse(view._cards[failure.task_id].isHidden())
        view.show_completed.setChecked(True)
        view.refresh()
        self.assertFalse(view._cards[success.task_id].isHidden())
        view.deleteLater()

    def test_pending_review_stays_visible_until_opened_and_ordinary_results_still_hide(self):
        review = self.submit(lambda *_args: None)
        ordinary = self.submit(lambda *_args: None)
        self.wait_until(lambda: not self.manager.is_busy())
        opened = []
        review.set_result_handler(lambda: opened.append('review'), pending=True)
        ordinary.set_result_handler(lambda: None)
        for handle in (review, ordinary):
            self.manager._records[handle.task_id]['finished_at'] = time.time() - 10
        view = BackgroundTasksWidget(self.manager)
        view.refresh()
        card = view._cards[review.task_id]
        self.assertFalse(card.isHidden())
        self.assertEqual(card.state.text(), '待核对')
        self.assertEqual(card.result_button.text(), '核对')
        self.assertTrue(view._cards[ordinary.task_id].isHidden())
        card.result_button.click()
        view.refresh()
        self.assertEqual(opened, ['review'])
        self.assertFalse(review.has_pending_result())
        self.assertTrue(card.isHidden())
        view.deleteLater()

    def test_deferred_result_and_failed_callback_keep_review_pending(self):
        handle = self.submit(lambda *_args: None)
        self.wait_until(lambda: not self.manager.is_busy())
        handle.set_result_handler(lambda: False, pending=True)
        self.assertTrue(handle.show_result())
        self.assertTrue(handle.has_pending_result())
        def fail():
            raise ValueError('review not ready')
        handle.set_result_handler(fail, pending=True)
        with self.assertRaisesRegex(ValueError, 'review not ready'):
            handle.show_result()
        self.assertTrue(handle.has_pending_result())
        self.manager.remove(handle.task_id)
        self.assertFalse(handle.has_pending_result())

    def test_delete_failed_record_removes_card_and_history_but_keeps_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.manager._history_path = Path(temporary) / "history.json"
            result = Path(temporary) / "partial-result.mp4"
            result.write_bytes(b"keep this output")
            from model.BackgroundTasks import BackgroundTaskResultError
            def runner(*_args):
                raise BackgroundTaskResultError("partial failure", {"paths": [result]})
            handle = self.submit(runner, retryable=True, payload={"large": "retry payload"})
            self.wait_until(lambda: not self.manager.is_busy())
            handle.set_result_handler(lambda: None)
            view = BackgroundTasksWidget(self.manager)
            # Hidden panels intentionally skip timer refresh; show the panel
            # before testing an interaction a user can actually perform.
            self.addCleanup(view.close)
            self.addCleanup(view.deleteLater)
            view.show()
            self.app.processEvents()
            card = view._cards[handle.task_id]
            self.assertFalse(card.delete_button.isHidden())
            self.assertTrue(card.delete_button.isEnabled())
            card.delete_button.click()
            self.wait_until(lambda: handle.task_id not in view._cards)
            self.assertEqual(self.manager.records(), [])
            self.assertFalse(handle.isRunning())
            self.assertEqual(self.manager.get(handle.task_id)["status"], "failed")
            self.assertNotIn(handle.task_id, self.manager._specs)
            self.assertNotIn(handle.task_id, self.manager._handles)
            self.assertFalse(handle.has_result_handler())
            self.assertFalse(self.manager.can_retry(handle.task_id))
            self.assertEqual(result.read_bytes(), b"keep this output")
            self.assertEqual(json.loads(self.manager._history_path.read_text(encoding="utf-8")), [])
            restored = BackgroundTaskManager(history_path=self.manager._history_path)
            self.assertEqual(restored.records(), [])
            restored.shutdown()
            restored.deleteLater()
            view.deleteLater()
            self.manager._history_path = None

    def test_running_queued_and_canceling_tasks_cannot_be_deleted(self):
        entered, release = threading.Event(), threading.Event()
        def runner(_data, context):
            entered.set()
            release.wait(3)
            context.check_canceled()
        try:
            running = self.submit(runner)
            queued = self.submit(lambda *_args: None)
            self.wait_until(entered.is_set)
            for handle in (running, queued):
                self.assertFalse(self.manager.can_remove(handle.task_id))
                self.assertFalse(self.manager.remove(handle.task_id))
            self.manager.cancel(running.task_id)
            self.assertFalse(self.manager.remove(running.task_id))
            self.manager.cancel(queued.task_id)
            self.assertTrue(self.manager.remove(queued.task_id))
            self.assertEqual(len(self.manager.records()), 1)
        finally:
            release.set()
            self.wait_until(lambda: not self.manager.is_busy())

    def test_delete_is_blocked_during_finish_signal_callbacks(self):
        handle = self.submit(lambda *_args: None)
        attempts = []
        handle.completed.connect(lambda *_args: attempts.append(self.manager.remove(handle.task_id)))
        handle.finished.connect(lambda: attempts.append(self.manager.remove(handle.task_id)))
        self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(attempts, [False, False])
        self.assertTrue(self.manager.remove(handle.task_id))
        self.assertFalse(self.manager.remove(handle.task_id))
        self.assertFalse(self.manager.remove("missing-id"))

    def test_delete_is_blocked_during_queued_cancel_callbacks(self):
        handle = self.submit(lambda *_args: None)
        attempts = []
        handle.failed.connect(lambda *_args: attempts.append(self.manager.remove(handle.task_id)))
        handle.finished.connect(lambda: attempts.append(self.manager.remove(handle.task_id)))
        self.manager.cancel(handle.task_id)
        self.assertEqual(attempts, [False, False])
        self.assertTrue(self.manager.remove(handle.task_id))

    def test_delete_history_save_failure_rolls_back_and_keeps_retry(self):
        handle = self.submit(lambda *_args: (_ for _ in ()).throw(ValueError("fail")), retryable=True)
        self.wait_until(lambda: not self.manager.is_busy())
        with patch.object(self.manager, "flush_history", return_value=False):
            with self.assertRaisesRegex(OSError, "未删除记录"):
                self.manager.remove(handle.task_id)
        self.assertEqual(len(self.manager.records()), 1)
        self.assertTrue(self.manager.can_remove(handle.task_id))
        self.assertTrue(self.manager.can_retry(handle.task_id))
        self.assertIn(handle.task_id, self.manager._handles)

    def test_bulk_cleanup_failure_only_keeps_success_and_active_tasks(self):
        view = BackgroundTasksWidget(self.manager)
        success = self.submit(lambda *_args: None)
        failed = self.submit(lambda *_args: (_ for _ in ()).throw(ValueError("fail")))
        self.wait_until(lambda: not self.manager.is_busy())
        queued = self.submit(lambda *_args: None)
        view.refresh()
        self.assertTrue(view.clear_failed_action.isEnabled())
        self.assertTrue(view._cards[queued.task_id].delete_button.isHidden())
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            view.clear_failed_action.trigger()
        self.assertNotIn(failed.task_id, view._cards)
        self.assertEqual({row["id"] for row in self.manager.records()}, {success.task_id, queued.task_id})
        view.deleteLater()

    def test_bulk_cleanup_can_be_canceled_and_saves_once(self):
        view = BackgroundTasksWidget(self.manager)
        handles = [self.submit(lambda *_args: None) for _ in range(3)]
        self.wait_until(lambda: not self.manager.is_busy())
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.No):
            view.clear_records({"succeeded"})
        self.assertEqual(len(self.manager.records()), 3)
        with patch.object(self.manager, "flush_history", wraps=self.manager.flush_history) as saved:
            self.assertEqual(self.manager.remove_many([h.task_id for h in handles] + [handles[0].task_id]), 3)
            self.assertEqual(saved.call_count, 1)
        view.refresh()
        self.assertEqual(view._cards, {})
        self.assertFalse(view.clear_finished_action.isEnabled())
        view.deleteLater()

    def test_restored_interrupted_record_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            handle = self.submit(lambda *_args: None)
            self.manager._history_path = path
            self.manager.flush_history()
            restored = BackgroundTaskManager(history_path=path)
            self.assertEqual(restored.get(handle.task_id)["status"], "interrupted")
            self.assertTrue(restored.remove(handle.task_id))
            self.assertEqual(restored.records(), [])
            self.manager._history_path = None
            restored.shutdown()
            restored.deleteLater()


if __name__ == "__main__":
    unittest.main()
