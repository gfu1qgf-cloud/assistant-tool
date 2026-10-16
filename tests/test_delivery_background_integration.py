import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qt_compat import QtCore, QtWidgets
from model.BackgroundTasks import BackgroundTaskManager, TaskRunContext, BackgroundTaskResultError
from model.TaskDeliveryBackground import (UPLOAD_RESOURCE, run_delivery_upload, run_quick_upload, snapshot_upload_plan,
                                         version_of, complete_pending_versions)
from model.TaskResultOrganizer import (file_identity, run_task_result_organizer,
    load_pending_changed_file_batches, save_pending_changed_file_batches)
from app_plugins.builtin.task_delivery_controller import TaskDeliveryController
from model.ThreadOutput import capture_thread_output


DAY = date(2026, 10, 8)


class DeliveryBackgroundIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / '1008' / 'result'
        self.output.mkdir(parents=True)
        self.config = {'run_export': True, 'video_detection_mode': 'skip', 'compress_enabled': False,
            'task_result_pending_file': str(self.root / 'pending.json'), 'drive_parent_folder_id': 'fake-parent',
            'video_upload_history_enabled': False, 'open_result_dir': False,
            'upload_date_override': '2026-10-08', 'upload_slot_override': '02'}
        self.manager = BackgroundTaskManager()
        self.release = threading.Event()
        self.stack = ExitStack()
        self.stack.enter_context(patch('model.TaskResultOrganizer.load_drive_service', return_value=object()))
        for name in ('write_task_submission_links', 'write_review_video_links', 'record_video_uploads'):
            self.stack.enter_context(patch('model.TaskResultOrganizer.' + name, return_value=0 if name.startswith('write') else {}))
        self.stack.enter_context(patch('model.TaskResultOrganizer.pending_review_recovery_records', return_value=[]))
        self.stack.enter_context(patch('model.TaskResultOrganizer.print_person_folder_links', return_value={}))
        self.stack.enter_context(patch('model.TaskResultOrganizer.preferred_drive_file_ids_by_identity', return_value={}))

    def tearDown(self):
        self.release.set()
        self.manager.cancel_all()
        self.wait_until(lambda: not self.manager.is_busy())
        self.manager.shutdown()
        self.stack.close()
        self.manager.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.app.processEvents()
        self.assertTrue(predicate())

    def video(self, name, data=b'completed-video'):
        path = self.output / name
        path.write_bytes(data)
        old = time.time() - 10
        os.utime(path, (old, old))
        return path

    def prepare(self, files, claimed=None):
        export = SimpleNamespace(output_dir=self.output, updated_files=files)
        def route(batches, *_args, **_kwargs):
            return [(root, paths, Path('Alice')) for root, paths in batches]
        with patch('model.TaskResultOrganizer.export_one_date', return_value=export), \
             patch('model.TaskResultOrganizer.build_routed_batches', side_effect=route):
            return run_task_result_organizer([DAY], self.root, self.config, prepare_only=True,
                upload_work_dir=self.manager.work_root / ('batch-' + str(time.monotonic_ns())),
                claimed_versions=claimed)

    def records(self, batches):
        return [{'local_file': str(path), 'name': Path(path).name, 'id': Path(path).stem,
                 'webViewLink': 'https://drive.google.com/file/d/fake/view', 'remote_prefix': 'Alice'}
                for _root, paths, _prefix in batches for path in paths]

    def submit(self, plan, runner=run_delivery_upload):
        return self.manager.submit(owner='task_delivery', title='test upload', runner=runner,
            payload=plan, kind='network', resources=(UPLOAD_RESOURCE,))

    def test_preparation_does_not_contact_drive_and_has_immutable_input(self):
        source = self.video('one.mp4', b'first')
        with patch('model.TaskResultOrganizer.load_drive_service') as drive:
            result = self.prepare([source])
        drive.assert_not_called()
        plan = result['upload_plan']
        copied = Path(plan['batches'][0][1][0])
        source.write_bytes(b'changed')
        self.assertEqual(copied.read_bytes(), b'first')
        self.assertEqual(plan['batch_date'], '2026-10-08')
        self.assertEqual(plan['batch_slot'], '02')

    def streaming_upload(self, *, fail_second=False):
        sources = [self.video('one.mp4'), self.video('two.mp4')]
        plan = self.prepare(sources)['upload_plan']; plan['postprocess'] = {'synthetic': True}
        events = []
        def process(source, root, _prefix):
            events.append(('render', source.name))
            if fail_second and source.name == 'two.mp4':
                raise ValueError('synthetic second render failure')
            target = root.with_name(root.name+'-processed') / ('[节点处理]'+source.name)
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b'processed')
            return target
        def sync(_service, path, _parent, **_kwargs):
            events.append(('upload', path.name))
            return {'local_file': str(path), 'name': path.name, 'id': path.stem,
                    'webViewLink': 'https://drive.google.com/file/d/fake/view'}
        ctx = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        with ExitStack() as stack:
            stack.enter_context(patch('app_plugins.builtin.task_delivery_workflow.processor', return_value=process))
            stack.enter_context(patch('model.GoogleDriveHelper.get_or_create_remote_folder', return_value='folder'))
            folders = stack.enter_context(patch('model.GoogleDriveHelper.get_or_create_remote_folder_path_with_root',
                                               return_value=('person', 'person-root')))
            stack.enter_context(patch('model.GoogleDriveHelper.get_or_create_remote_folder_path', return_value='target'))
            stack.enter_context(patch('model.GoogleDriveHelper.sync_file', side_effect=sync))
            try:
                result = run_delivery_upload(plan, ctx, registry=object())
            except BackgroundTaskResultError as error:
                if not fail_second: raise
                result = error.result
        self.assertEqual(folders.call_count, 1)
        return events, result, sources

    def test_rendered_file_is_uploaded_before_next_render(self):
        events, result, _sources = self.streaming_upload()
        self.assertEqual(events, [('render','one.mp4'), ('upload','[节点处理]one.mp4'),
                                  ('render','two.mp4'), ('upload','[节点处理]two.mp4')])
        self.assertEqual(result['uploaded_file_count'], 2)
        self.assertEqual(load_pending_changed_file_batches(self.config, self.root, [DAY]), [])

    def test_later_render_error_does_not_lose_first_upload_or_pending_retry(self):
        events, result, sources = self.streaming_upload(fail_second=True)
        self.assertEqual(events, [('render','one.mp4'), ('upload','[节点处理]one.mp4'), ('render','two.mp4')])
        self.assertEqual(result['uploaded_file_count'], 1)
        self.assertEqual([row['file_name'] for row in result['upload_failed_files']], ['two.mp4'])
        pending = load_pending_changed_file_batches(self.config, self.root, [DAY])
        self.assertEqual([path for _root, paths in pending for path in paths], [sources[1]])

    def test_quick_upload_shares_queue_and_reports_each_snapshot_file(self):
        first = self.video('quick-one.mp4', b'one')
        second = self.video('quick-two.mp4', b'two')
        blocker = self.manager.submit(owner='task_delivery', title='previous batch',
            runner=lambda *_: self.release.wait(6), kind='network', resources=(UPLOAD_RESOURCE,))
        self.wait_until(lambda: self.manager.get(blocker.task_id)['status'] == 'running')
        folder_calls, copied, progress = [], [], []
        def folder(_service, _parent, name):
            folder_calls.append(name)
            return 'folder-' + name
        def sync(_service, local, _parent):
            copied.append((local.read_bytes(), local))
            self.assertNotIn(self.output, local.parents)
            return {'action': 'uploaded'}
        module = 'app_plugins.builtin.task_delivery_quick_upload.'
        with patch(module + 'load_drive_service', return_value=object()), \
             patch(module + 'get_or_create_remote_folder', side_effect=folder), \
             patch(module + 'sync_file', side_effect=sync):
            handle = self.manager.submit(owner='task_delivery', title='quick batch', runner=run_quick_upload,
                payload={'sources': [str(first), str(second)], 'name': 'extras', 'parent_folder': 'fake-parent',
                         'batch_date': '2026-10-08', 'batch_slot': '02'},
                kind='network', resources=(UPLOAD_RESOURCE,))
            handle.progressChanged.connect(lambda message, percent: progress.append((message, percent)))
            self.app.processEvents()
            self.assertEqual(self.manager.get(handle.task_id)['status'], 'queued')
            self.assertEqual(copied, [])
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(folder_calls, ['1008', '02', 'extras'])
        self.assertEqual([data for data, _path in copied], [b'one', b'two'])
        self.assertTrue(any('2/2' in message and percent == 90 for message, percent in progress))
        self.assertTrue(all(not path.exists() for _data, path in copied))
        self.assertEqual(self.manager.get(handle.task_id)['status'], 'succeeded')

    def test_network_metadata_error_after_commit_keeps_success_and_remaining_pending(self):
        first, second = self.video('one.mp4'), self.video('two.mp4')
        plan = self.prepare([first, second])['upload_plan']
        saved = []
        def upload(_service, batches, *_args, **_kwargs):
            from model.GoogleDriveHelper import _upload_observer
            _upload_observer.get()['records'].append(self.records(batches)[0])
            raise OSError('synthetic folder request failure after first commit')
        with patch('model.TaskResultOrganizer.upload_routed_changed_files_to_drive_batch', side_effect=upload):
            handle = self.submit(plan)
            handle.completed.connect(saved.append)
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(self.manager.get(handle.task_id)['status'], 'failed')
        self.assertEqual(saved[0]['uploaded_file_count'], 1)
        remaining = load_pending_changed_file_batches(self.config, self.root, [DAY])
        self.assertEqual([path.name for _root, files in remaining for path in files], ['two.mp4'])

    def test_ten_then_ten_uploads_serialize_without_readding_first_ten(self):
        first_files = [self.video(f'a{index}.mp4') for index in range(10)]
        first = self.prepare(first_files)['upload_plan']
        started, seen = threading.Event(), []
        def upload(_service, batches, *_args, **_kwargs):
            batches = list(batches)  # The production uploader consumes lazily.
            names = [Path(p).name for _r, files, _p in batches for p in files]
            seen.append(names)
            if len(seen) == 1:
                started.set()
                self.release.wait(6)
            return self.records(batches)
        with patch('model.TaskResultOrganizer.upload_routed_changed_files_to_drive_batch', side_effect=upload):
            self.submit(first)
            self.wait_until(started.is_set)
            second_files = [self.video(f'b{index}.mp4') for index in range(10)]
            claimed = {file_identity(item['original']): [item['version']] for item in first['files'].values()}
            second = self.prepare(second_files, claimed)['upload_plan']
            self.assertEqual(len(second['files']), 10)
            last = self.submit(second)
            self.app.processEvents()
            self.assertEqual(self.manager.get(last.task_id)['status'], 'queued')
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual([len(names) for names in seen], [10, 10])
        self.assertTrue(set(seen[0]).isdisjoint(seen[1]))
        self.assertEqual(load_pending_changed_file_batches(self.config, self.root, [DAY]), [])

    def test_revised_same_name_uses_its_own_bytes_and_latest_drive_id(self):
        source = self.video('one.mp4', b'old')
        first = self.prepare([source])['upload_plan']
        self.video('one.mp4', b'new-version')
        claimed = {file_identity(source): [next(iter(first['files'].values()))['version']]}
        second = self.prepare([source], claimed)['upload_plan']
        entered = threading.Event()
        seen, identifiers = [], {}
        def upload(_service, batches, *_args, preferred_file_ids=None, **_kwargs):
            batches = list(batches)
            seen.append((Path(batches[0][1][0]).read_bytes(), dict(preferred_file_ids)))
            if len(seen) == 1:
                entered.set()
                self.release.wait(6)
                identifiers['one.mp4'] = 'id-created-by-first-batch'
            return self.records(batches)
        with patch('model.TaskResultOrganizer.preferred_drive_file_ids_by_identity', side_effect=lambda _: dict(identifiers)), \
             patch('model.TaskResultOrganizer.upload_routed_changed_files_to_drive_batch', side_effect=upload):
            self.submit(first)
            self.wait_until(entered.is_set)
            self.submit(second)
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual([row[0] for row in seen], [b'old', b'new-version'])
        self.assertEqual(seen[1][1], {'one.mp4': 'id-created-by-first-batch'})

    def test_partial_failure_preserves_only_failed_pending_and_delivers_success_result(self):
        sources = [self.video('one.mp4'), self.video('two.mp4')]
        plan = self.prepare(sources)['upload_plan']
        def upload(_service, batches, *_args, **_kwargs):
            return self.records([next(iter(batches))])
        results = []
        with patch('model.TaskResultOrganizer.upload_routed_changed_files_to_drive_batch', side_effect=upload):
            handle = self.submit(plan)
            handle.completed.connect(results.append)
            self.wait_until(lambda: not self.manager.is_busy())
        self.assertEqual(self.manager.get(handle.task_id)['status'], 'failed')
        self.assertEqual(results[0]['uploaded_file_count'], 1)
        pending = load_pending_changed_file_batches(self.config, self.root, [DAY])
        self.assertEqual(pending[0][1], sources[1:])

    def test_finishing_old_version_does_not_clear_pending_revision(self):
        source = self.video('one.mp4', b'old')
        plan = self.prepare([source])['upload_plan']
        self.video('one.mp4', b'new')
        complete_pending_versions(plan, [{'upload_source_file': str(source)}])
        self.assertEqual(load_pending_changed_file_batches(self.config, self.root, [DAY])[0][1], [source])

    def test_queued_cancel_leaves_pending_file_and_does_not_start_upload(self):
        plan = self.prepare([self.video('one.mp4')])['upload_plan']
        calls = []
        handle = self.submit(plan, runner=lambda *_: calls.append(True))
        self.manager.cancel(handle.task_id)
        self.app.processEvents()
        self.assertEqual(calls, [])
        self.assertTrue(load_pending_changed_file_batches(self.config, self.root, [DAY]))

    def test_cancel_after_remote_success_still_records_successful_file(self):
        from model.GoogleDriveHelper import _report_upload, _upload_observer
        sources = [self.video('one.mp4'), self.video('two.mp4')]
        plan = self.prepare(sources)['upload_plan']
        canceled = threading.Event()
        context = TaskRunContext(canceled, lambda *_: None, lambda *_: None)
        def upload(_service, batches, *_args, **_kwargs):
            record = self.records([next(iter(batches))])[0]
            _upload_observer.get()['records'].append(record)
            canceled.set()
            _report_upload('two.mp4')
        with patch('model.TaskResultOrganizer.upload_routed_changed_files_to_drive_batch', side_effect=upload):
            result = run_delivery_upload(plan, context)
        self.assertTrue(result['cancelled_upload'])
        self.assertEqual(result['uploaded_file_count'], 1)
        self.assertEqual(load_pending_changed_file_batches(self.config, self.root, [DAY])[0][1], sources[1:])

    def test_controller_reenables_preparation_while_upload_runs(self):
        window = QtWidgets.QWidget()
        window.task_path_edit = QtWidgets.QLineEdit(str(self.root))
        window.dateEdit = QtWidgets.QDateEdit(QtCore.QDate(2026, 10, 8))
        button = QtWidgets.QPushButton('整理任务结果')
        context = SimpleNamespace(parent_widget=window, load_config=lambda: dict(self.config),
            background_tasks=self.manager, gemini_keys=None, log=Mock(), notify=Mock())
        context.submit_background_task = lambda **kw: self.manager.submit(owner='task_delivery', **kw)
        controller = TaskDeliveryController(SimpleNamespace(quick_actions=SimpleNamespace(organize_button=button)), context)
        plan = self.prepare([self.video('one.mp4')])['upload_plan']
        gate = threading.Event()
        def run(_plan, _ctx):
            gate.set()
            self.release.wait(6)
            return {'uploaded_file_count': 0, 'upload_failed_files': [{'local_file': str(self.output / 'one.mp4')}]}
        with patch('app_plugins.builtin.task_delivery_controller.run_delivery_upload', side_effect=run), \
             patch.object(controller, '_prepare_delivery_config', return_value=self.config), \
             patch('app_plugins.builtin.task_delivery_controller.TaskResultOrganizerThread') as factory, \
             patch.object(controller, 'record_task_result_history'):
            controller._queue_upload(plan)
            self.wait_until(gate.is_set)
            self.assertTrue(button.isEnabled())
            new_preparation = controller.organize_results()
            factory.assert_called_once()
            new_preparation.start.assert_called_once()
            self.assertTrue(factory.call_args.kwargs['prepare_only'])
            work = factory.call_args.kwargs['upload_work_dir']
            self.assertEqual(work.parent, self.root / '.整理上传临时')
            self.assertFalse(work.is_relative_to(self.manager.work_root))
            self.release.set()
            self.wait_until(lambda: not self.manager.is_busy())
            controller.on_task_result_finished()
        window.deleteLater()
        button.deleteLater()

    def test_concurrent_stdout_is_not_stolen_by_another_job(self):
        outputs = [io.StringIO(), io.StringIO()]
        barrier = threading.Barrier(2)
        def run(index):
            with capture_thread_output(outputs[index]):
                barrier.wait(2)
                for _ in range(20):
                    print('worker-' + str(index))
        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        for i in range(2):
            self.assertEqual(outputs[i].getvalue().splitlines(), ['worker-' + str(i)] * 20)


if __name__ == '__main__':
    unittest.main()
