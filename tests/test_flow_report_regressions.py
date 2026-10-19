"""Verify the review report against real isolated queue, UI and file boundaries."""
import copy
from concurrent.futures import Future, ThreadPoolExecutor
import math
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from qt_compat import QtCore, QtWidgets
import test_flow_assistant as fixtures
import test_flow_production as production
from app_plugins.builtin.flow_assistant.store import QueueStore
from app_plugins.builtin.flow_assistant.archive import archive_download
from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
from app_plugins.builtin.flow_assistant.page_recovery import BINDING_MISSING, watch


class Surface(QtWidgets.QWidget):
    positionChanged = QtCore.pyqtSignal(float)
    errorOccurred = QtCore.pyqtSignal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(args[1] if len(args) > 1 else None)
        self.prepare, self.play, self.pause, self.release = (MagicMock() for _ in range(4))


class ReportTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    client = fixtures.FlowTests.client
    jobs = fixtures.FlowTests.jobs
    ui = production.ProductionTests.ui
    archive_row = production.ProductionTests.archive_row

    def test_preferences_are_persisted_and_survive_new_store(self):
        service = self.service()
        value = {'concurrency': 3, 'downloads_root': str(self.root / 'other-downloads')}
        service.execute({'action': 'preferences', 'value': value})
        restored = QueueStore(self.namespace).settings()
        self.assertEqual(restored['concurrency'], 3)
        self.assertEqual(restored['downloads_root'], value['downloads_root'])

    def test_failed_preferences_save_keeps_live_state_and_download_root(self):
        service = self.service()
        previous, root = copy.deepcopy(service.preferences), service.downloads_root
        errors = []; service.failed.connect(errors.append)
        with patch.object(service.store, 'save_settings', side_effect=OSError('volume unavailable')):
            service.execute({'action': 'preferences', 'value': {'downloads_root': 'other'}})
        self.assertEqual(service.preferences, previous)
        self.assertEqual(service.downloads_root, root)
        self.assertEqual(errors, ['volume unavailable'])

    def test_quarantine_and_resume_failures_do_not_publish_unsaved_preferences(self):
        service = self.service(); service.store.enqueue(self.jobs())
        row = service.store.rows()[0]; client = self.client(service)
        for operation in (lambda: service.quarantine(client, row, 'bad model'),
                          lambda: service.resume_browser({'keys': ['profile:'+client['profile']]})):
            previous = copy.deepcopy(service.preferences)
            with patch.object(service.store, 'save_settings', side_effect=OSError('full')):
                with self.assertRaises(OSError): operation()
            self.assertEqual(service.preferences, previous)
        self.assertTrue(service.paused)

    def test_observed_cost_is_published_only_after_successful_save(self):
        service = self.service(); before = copy.deepcopy(service.preferences)
        with patch.object(service.store, 'save_settings', side_effect=OSError('full')):
            with self.assertRaises(OSError): service.remember_cost(self.jobs()[0], 0)
        self.assertEqual(service.observed_costs, {})
        self.assertEqual(service.preferences, before)

    def test_preview_missing_payload_output_and_remote_are_controlled(self):
        from app_plugins.builtin.flow_assistant.result_preview import ResultPreview
        with patch('model.MediaPlayback.FfplayReviewSurface', Surface):
            for row in ({'payload': None}, {'payload': {}}, {'payload': {'output': None}}):
                with self.subTest(row=row), self.assertRaisesRegex(ValueError, '成品不存在'):
                    ResultPreview(row)
            output = self.target / 'preview.mp4'; output.write_bytes(b'fixture')
            for bad in ('0', 'not-a-number', 0, -1, math.nan, math.inf, None, True):
                row = {'payload': {'output': str(output), 'duration': bad}, 'remote': None}
                view = ResultPreview(row); self.addCleanup(view.close)
                self.assertGreater(view.duration, 0)
                self.assertTrue(math.isfinite(view.duration))
                view.position(0); view.seek()
            view = ResultPreview({'payload': {'output': str(output), 'duration': '8.5'},
                                  'remote': {'media': {'duration': '0'}}})
            self.addCleanup(view.close)
            self.assertEqual(view.duration, 8.5)
            view.position(4.25); self.assertEqual(view.slider.value(), 5000)

    def test_refresh_before_open_and_after_stopping_does_not_touch_catalog(self):
        plugin = FlowAssistantPlugin(); plugin.context = MagicMock()
        plugin.refresh_tasks()
        plugin.context.current_task_targets.assert_not_called()
        plugin.dialog = MagicMock(); plugin.stopping = True
        plugin.refresh_tasks(); plugin.dialog.set_targets.assert_not_called()

    def test_ui_contract_survives_minor_release_and_rejects_incompatible_api(self):
        from app_plugins.builtin.flow_assistant.ui import FlowDialog
        plugin = SimpleNamespace(version='0.4.999', ui_api_version=1, command=MagicMock(),
            context=SimpleNamespace(parent_widget=SimpleNamespace()))
        view = FlowDialog(plugin); self.addCleanup(view.close)
        plugin.ui_api_version = 2
        with self.assertRaises(RuntimeError): FlowDialog(plugin)

    def test_variant_restores_group_count_and_new_records_remember_requested_count(self):
        dialog, plugin = self.ui(); service = self.service()
        service.store.enqueue(self.jobs(count=3, duration=8))
        rows = service.store.rows(); panel = dialog.manual
        panel.recent.update_rows(rows); panel.recent.list.setCurrentRow(0)
        panel.recent.variant()
        self.assertEqual(panel.options.count.value(), 3)
        panel.restore_job(rows[0]); self.assertEqual(panel.options.count.value(), 3)

    def test_unknown_batches_do_not_merge_and_different_parameters_stay_separate(self):
        dialog, plugin = self.ui(); service = self.service()
        service.store.enqueue(self.jobs(count=2)); rows = service.store.rows()
        for row in rows: row['payload']['batch_id'] = None
        dialog.manual.recent.update_rows(rows)
        self.assertEqual(dialog.manual.recent.list.count(), 2)
        for row in rows: row['payload']['batch_id'] = 'same'
        rows[1]['payload']['resolution'] = '360p'
        dialog.manual.recent.update_rows(rows)
        self.assertEqual(dialog.manual.recent.list.count(), 2)

    def test_zero_cost_and_zero_balance_can_pass_verified_prepare(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=None, budget=0))
        client = self.client(service, credits=0); row = service.store.rows()[0]
        client['account'] = client['account'].casefold()
        service.store.update(row['id'], 'preparing', browser=client['id'], attempt='c'*32,
            remote={'account': client['account']})
        service.request(client, 'prepare', {}, row['id'])
        key = next(k for k, r in service.pending.items() if r['action'] == 'prepare')
        with patch.object(service, 'start_upload') as upload:
            service._response(client, {'request_id': key, 'success': True,
                'data': {'verified': True, 'model': 'omni_1_1_flash', 'cost': 0, 'email': client['account']}})
            upload.assert_called_once()
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'uploading')
        self.assertEqual(current['remote']['cost'], 0)

    def test_cancelled_cleanup_releases_busy_flag_and_reports_error(self):
        service = self.service(); service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'completed')
        future = Future(); future.cancel(); pool = MagicMock(); pool.submit.return_value = future
        with patch.object(service, 'pool', pool):
            service.extra_action({'action': 'cleanup', 'ids': [row['id']]})
        self.app.processEvents()
        self.assertNotIn(row['id'], service.archiving)
        self.assertTrue(service.store.get(row['id'])['error'])

    def test_recovery_uses_code_even_when_user_facing_words_change(self):
        service = self.service(); service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        client = self.client(service)
        service.store.update(row['id'], 'uncertain', browser=client['id'], attempt='a'*32,
            remote={'recovery_code': BINDING_MISSING}, error='arbitrary translated message')
        self.assertTrue(watch(service))
        self.assertEqual(service.store.get(row['id'])['remote']['page_recovery']['mode'], 'snapshot_only')

    @unittest.skipUnless(os.name == 'nt', 'Windows no-overwrite publish race')
    def test_archive_race_accepts_identical_file_but_preserves_conflicting_file(self):
        for same in (True, False):
            row, downloads, source = self.archive_row()
            destination = Path(row['payload']['output'])
            def race(temporary, output):
                Path(output).write_bytes(source.read_bytes() if same else b'other existing video')
                raise FileExistsError('race')
            with patch('app_plugins.builtin.flow_assistant.archive.data_io.rename', side_effect=race):
                if same:
                    self.assertEqual(archive_download(row, downloads, verify=lambda _: None), str(destination))
                else:
                    with self.assertRaisesRegex(FileExistsError, '未覆盖'):
                        archive_download(row, downloads, verify=lambda _: None)
                    self.assertEqual(destination.read_bytes(), b'other existing video')
            self.assertTrue(source.exists())
            self.assertFalse(list(destination.parent.glob('*.part')))
            # Each synthetic case uses a distinct attempt directory.
            source.unlink(); source.parent.rmdir()
            destination.unlink()

    def test_enqueue_and_tick_reads_do_not_deadlock_and_ack_errors(self):
        store = QueueStore(self.namespace)
        def submit():
            for _ in range(12): store.enqueue(self.jobs())
        def read():
            for _ in range(40): store.rows()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit), pool.submit(read)]
            for future in futures: future.result(timeout=15)
        self.assertEqual(len(store.rows()), 12)
        service = self.service(); errors = []; service.submission_failed.connect(errors.append)
        with patch.object(service.store, 'enqueue', side_effect=OSError('database is locked')):
            service.execute({'action': 'enqueue', 'items': [{**self.args, 'count': 1}], 'request_id': 'locked'})
        self.assertEqual(errors[0]['request_id'], 'locked')


if __name__ == '__main__': unittest.main()
