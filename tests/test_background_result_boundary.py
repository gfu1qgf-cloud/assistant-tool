"""Bad partial results must release the job, not escape through a Qt slot."""
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from qt_compat import QtCore, QtWidgets
from model.BackgroundTasks import BackgroundTaskManager, BackgroundTaskResultError


class BackgroundResultBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.manager = BackgroundTaskManager(logger=lambda *_: None, max_parallel=1)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()
        self.assertTrue(predicate(), 'following job did not settle')

    def tearDown(self):
        self.manager.cancel_all()
        self.wait_until(lambda: self.manager.is_settled())
        self.manager.shutdown()
        self.manager.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def submit(self, runner):
        return self.manager.submit(owner='synthetic', title='result boundary',
            runner=runner, resources=('synthetic:shared',))

    def legacy_finished(self, result, *, canceled=False):
        """An old/already-running worker can still send an unvalidated result.

        Call the receiving slot directly so the pre-fix exception is a normal
        unittest error, not an unhandled Qt callback that aborts the process.
        """
        handle = self.submit(lambda *_: None)
        task_id = handle.task_id
        self.manager._queue.remove(task_id)
        self.manager._records[task_id]['status'] = 'running'
        event = threading.Event()
        if canceled:
            event.set()
        worker = SimpleNamespace(task_id=task_id, canceled=event,
            wait=lambda *_: True, deleteLater=lambda: None)
        self.manager._workers[task_id] = worker
        self.manager._on_outcome(task_id, {'status': 'failed',
            'error': 'original partial failure', 'result': result})
        delivered = []
        handle.completed.connect(delivered.append)
        following_ran = []
        following = self.submit(lambda *_: following_ran.append(True))
        try:
            self.manager._on_finished(worker)
            self.wait_until(lambda: self.manager.is_settled())
            self.assertEqual(following_ran, [True])
            self.assertEqual(self.manager.get(following.task_id)['status'], 'succeeded')
            self.assertNotIn(task_id, self.manager._workers)
            self.assertNotIn(task_id, self.manager._notifying)
            self.assertNotIn(task_id, self.manager._outcomes)
            return self.manager.get(task_id), delivered
        finally:
            # Pre-fix slot errors leave the fake worker behind. Do not let
            # that deliberate reproduction obstruct test teardown.
            self.manager._workers.pop(task_id, None)
            self.manager._notifying.discard(task_id)

    def test_malformed_partial_paths_do_not_block_the_same_resource(self):
        for invalid in (None, 7, 'not-a-path-list', {'wrong': 'shape'}):
            with self.subTest(value=invalid):
                row, delivered = self.legacy_finished({'paths': invalid})
                self.assertEqual(row['status'], 'failed')
                self.assertIn('paths', row['error'])
                self.assertIn('Traceback', row['traceback'])
                self.assertEqual(delivered, [])
                self.assertEqual(row['paths'], [])

    def test_path_string_conversion_error_is_contained(self):
        class BrokenPath:
            def __str__(self):
                raise ValueError('synthetic broken path')
        row, delivered = self.legacy_finished({'paths': [BrokenPath()]})
        self.assertIn('synthetic broken path', row['error'])
        self.assertEqual(delivered, [])

    def test_valid_partial_paths_are_strings_for_owner_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'partial.mp4'
            path.write_bytes(b'kept synthetic output')
            def runner(*_):
                raise BackgroundTaskResultError('partial failure',
                    {'paths': (path,), 'completed': 1, 'failed': 1})
            handle = self.submit(runner)
            delivered = []
            handle.completed.connect(delivered.append)
            self.wait_until(lambda: self.manager.is_settled())
            self.assertEqual(delivered, [{'paths': [str(path)], 'completed': 1, 'failed': 1}])
            self.assertEqual(self.manager.get(handle.task_id)['paths'], [str(path)])
            self.assertEqual(path.read_bytes(), b'kept synthetic output')

    def test_late_cancel_remains_canceled_even_when_partial_result_is_bad(self):
        row, delivered = self.legacy_finished({'paths': None}, canceled=True)
        self.assertEqual(row['status'], 'canceled')
        self.assertEqual(delivered, [])


if __name__ == '__main__':
    unittest.main()
