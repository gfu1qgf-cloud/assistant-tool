"""Durable transfer retries: no accounts, cloud generation or real downloads."""
import copy
import json
import time
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets
import test_flow_assistant as fixtures
import test_flow_production as production
import test_flow_setup as setup_fixtures
from app_plugins.builtin.flow_assistant.store import QueueStore
from app_plugins.builtin.flow_assistant.transfer_policy import retry_info, retry_detail, retry_limit


class TransferTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    bound_row = production.ProductionTests.bound_row
    ui = setup_fixtures.SetupTests.ui

    def client(self, service, *args, **kwargs):
        value = fixtures.FlowTests.client(self, service, *args, **kwargs)
        value['account'] = value['account'].casefold()
        return value

    def due(self, service, job, stage):
        row = service.store.get(job)
        info = {**retry_info(row, stage), 'next_at': time.time() - 1, 'waiting': ''}
        service._save_transfer(row, stage, info)

    def request(self, service, action):
        return next(key for key, value in service.pending.items() if value['action'] == action)

    def actions(self, client):
        return [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list]

    def upload(self, service):
        service.store.enqueue(self.jobs(duration=10, budget=0)); row = service.store.rows()[-1]
        client = self.client(service); service.paused = False
        service.store.update(row['id'], 'uploading', attempt='b' * 32, browser=client['id'], remote={
            'account': client['account'], 'cost': 15, 'cost_source': 'duration_rule', 'profile': client['profile']})
        self.assertTrue(service.start_upload(service.store.get(row['id'])))
        return service.store.get(row['id']), client

    def test_current_page_missing_task_uses_durable_restore_immediately(self):
        service = self.service(); row, client = self.bound_row(service)
        service.store.update(row['id'], 'download_failed', error='当前项目中没有可重试下载的绑定结果；未重新生成。')
        service.execute({'action': 'retry_download_selected', 'ids': [row['id']]})
        request = self.actions(client)[-1]
        self.assertEqual(request['action'], 'retry_download')
        self.assertEqual(request['params']['restore']['bindings'], row['remote']['bindings'])
        self.assertEqual(request['params']['restore']['task_id'], 'task')
        self.assertEqual(request['params']['restore']['download_path'], service.download_name(row))
        self.assertEqual(service.store.get(row['id'])['status'], 'downloading')
        self.assertNotIn('run_tasks', [value['action'] for value in self.actions(client)])

    def test_download_automatic_backoff_deduplication_and_limit(self):
        service = self.service(); service.preferences['download_retry_limit'] = 2
        row, client = self.bound_row(service)
        service.watch_transfer_retries(); row = service.store.get(row['id'])
        self.assertEqual(retry_info(row, 'download')['count'], 0)
        self.assertAlmostEqual(retry_info(row, 'download')['next_at'] - time.time(), 5, delta=1)
        for index in (1, 2):
            self.due(service, row['id'], 'download'); service.watch_transfer_retries()
            request = self.request(service, 'retry_download')
            before = len(self.actions(client)); service.watch_transfer_retries()
            self.assertEqual(before, len(self.actions(client)))
            service._response(client, {'request_id': request, 'success': False, 'error': 'NETWORK_FAILED'})
            current = service.store.get(row['id']); info = retry_info(current, 'download')
            self.assertEqual(info['count'], index)
            if index == 1: self.assertAlmostEqual(info['next_at'] - time.time(), 15, delta=1)
        self.assertTrue(info['stopped']); self.assertEqual(info['next_at'], 0)
        before = len(self.actions(client)); service.watch_transfer_retries()
        self.assertEqual(before, len(self.actions(client))); self.assertNotIn('run_tasks', [v['action'] for v in self.actions(client)])

    def test_upload_failure_is_retried_and_success_submits_generation_once(self):
        service = self.service(); row, client = self.upload(service)
        old = self.request(service, 'upload_images')
        service._response(client, {'request_id': old, 'success': False, 'data': {'errors': {'image': 'NETWORK_FAILED'}}})
        current = service.store.get(row['id']); self.assertEqual(current['status'], 'uploading')
        self.assertIn('NETWORK_FAILED', current['error']); self.assertEqual(retry_info(current, 'upload')['count'], 0)
        self.due(service, row['id'], 'upload'); service.watch_transfer_retries()
        request = self.request(service, 'upload_images')
        service._response(client, {'request_id': old, 'success': True})
        self.assertNotIn('run_tasks', [v['action'] for v in self.actions(client)])
        service._response(client, {'request_id': request, 'success': True})
        service._response(client, {'request_id': request, 'success': True})
        runs = [v for v in self.actions(client) if v['action'] == 'run_tasks']
        self.assertEqual(len(runs), 1); self.assertEqual(runs[0]['params']['batch_id'], row['attempt'])
        batch = service.store.batches()[row['payload']['batch_id']]
        self.assertEqual(batch['reserved'], 15)

    def test_upload_exhaustion_and_changed_source_are_not_silent(self):
        service = self.service(); service.preferences['upload_retry_limit'] = 1; row, client = self.upload(service)
        service._response(client, {'request_id': self.request(service, 'upload_images'), 'success': False, 'error': 'network'})
        self.due(service, row['id'], 'upload'); service.watch_transfer_retries()
        service._response(client, {'request_id': self.request(service, 'upload_images'), 'success': False, 'error': 'network'})
        self.assertEqual(service.store.get(row['id'])['status'], 'failed')
        self.assertTrue(retry_info(service.store.get(row['id']), 'upload')['stopped'])
        self.assertIn('1/1', retry_detail(service.store.get(row['id']), service.preferences))
        self.assertFalse(any(v['action'] == 'run_tasks' for v in self.actions(client)))

    def test_image_changed_during_backoff_stops_without_upload_or_charge(self):
        service = self.service(); row, client = self.upload(service)
        service._response(client, {'request_id': self.request(service, 'upload_images'), 'success': False, 'error': 'network'})
        self.image.write_bytes(b'changed after submission'); self.due(service, row['id'], 'upload')
        before = len(self.actions(client)); service.watch_transfer_retries()
        self.assertEqual(before, len(self.actions(client))); self.assertEqual(service.store.get(row['id'])['status'], 'failed')
        self.assertIn('变化', service.store.get(row['id'])['error'])
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 0)

    def test_paused_queue_holds_upload_but_not_free_download_recovery(self):
        service = self.service(); row, client = self.upload(service)
        service._response(client, {'request_id': self.request(service, 'upload_images'), 'success': False, 'error': 'network'})
        service.paused = True; self.due(service, row['id'], 'upload'); service.watch_transfer_retries()
        self.assertFalse(any(v['action'] == 'upload_images' for v in service.pending.values()))
        self.assertIn('队列已暂停', retry_info(service.store.get(row['id']), 'upload')['waiting'])
        service.released.add(row['id']); service.watch_transfer_retries()
        self.assertTrue(any(v['action'] == 'upload_images' for v in service.pending.values()))
        service.pending.clear(); service.store.update(row['id'], 'failed')
        down, other = self.bound_row(service); service.safety_paused = True
        service.watch_transfer_retries(); self.due(service, down['id'], 'download'); service.watch_transfer_retries()
        self.assertTrue(any(v['action'] == 'retry_download' for v in service.pending.values()))

    def test_offline_wait_does_not_spend_retries_or_rewrite_the_database_every_tick(self):
        service = self.service(); row, client = self.bound_row(service); service.clients.clear()
        service.watch_transfer_retries(); self.due(service, row['id'], 'download'); service.watch_transfer_retries()
        first = service.store.get(row['id']); self.assertEqual(retry_info(first, 'download').get('count', 0), 0)
        with patch.object(service.store, 'update', wraps=service.store.update) as update:
            for _ in range(4): service.watch_transfer_retries()
        update.assert_not_called()
        service.clients[client['socket']] = client; service.watch_transfer_retries()
        self.assertEqual(retry_info(service.store.get(row['id']), 'download')['count'], 1)

    def test_disconnect_cancels_old_upload_request_and_waits_for_original_browser(self):
        service = self.service(); row, client = self.upload(service); old = self.request(service, 'upload_images')
        service._disconnected(client['socket']); self.assertNotIn(old, service.pending)
        self.assertEqual(service.store.get(row['id'])['status'], 'uploading')
        self.due(service, row['id'], 'upload'); service.watch_transfer_retries()
        self.assertFalse(service.pending)
        service.clients[client['socket']] = client; service.watch_transfer_retries()
        service._response(client, {'request_id': old, 'success': True})
        self.assertNotIn('run_tasks', [v['action'] for v in self.actions(client)])
        self.assertEqual(retry_info(service.store.get(row['id']), 'upload')['count'], 1)

    def test_partial_page_snapshot_cannot_erase_binding_or_restart_active_download(self):
        service = self.service(); row, client = self.bound_row(service); service.retry_download(row)
        service._response(client, {'request_id': self.request(service, 'retry_download'), 'success': True, 'data': {'downloadId': 9}})
        snapshot = {'batch_id': row['attempt'], 'running': False, 'tasks': [{
            'taskId': 'task', 'download_path': service.download_name(row), 'mediaBindings': []}]}
        service._snapshot(client, snapshot)
        current = service.store.get(row['id'])
        self.assertEqual(current['remote']['bindings'], row['remote']['bindings'])
        self.assertEqual(current['status'], 'downloading')

    def test_duplicate_or_old_download_interruption_does_not_reset_current_recovery(self):
        service = self.service(); row, client = self.bound_row(service)
        event = {'batch_id': row['attempt'], 'event_id': 'interrupted-8', 'data': {
            'state': 'interrupted', 'error': 'NETWORK_FAILED', 'downloadId': 8,
            'download_path': service.download_name(row), 'taskId': 'task', 'mediaId': 'media', 'submissionId': 'submission'}}
        service._download(client, event); first = service.store.get(row['id'])
        service._download(client, event); self.assertEqual(first, service.store.get(row['id']))
        self.due(service, row['id'], 'download'); service.watch_transfer_retries()
        service._response(client, {'request_id': self.request(service, 'retry_download'), 'success': True, 'data': {'downloadId': 9}})
        before = service.store.get(row['id']); event['event_id'] = 'late-old-eight'
        service._download(client, event); self.assertEqual(before, service.store.get(row['id']))

    def test_upstream_running_batch_cannot_turn_download_retry_back_into_generation(self):
        service = self.service(); row, client = self.bound_row(service)
        service.watch_transfer_retries(); planned = retry_info(service.store.get(row['id']), 'download')['next_at']
        snapshot = {'batch_id': row['attempt'], 'running': True, 'tasks': [{
            'taskId': 'task', 'download_path': service.download_name(row), 'mediaBindings': row['remote']['bindings']}]}
        service._snapshot(client, snapshot); current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'download_failed'); self.assertTrue(current['remote']['cloud_finished'])
        self.assertEqual(retry_info(current, 'download')['next_at'], planned)
        service.store.update(row['id'], 'generating', remote={**row['remote'], 'cloud_finished': False})
        service._snapshot(client, snapshot)
        self.assertEqual(service.store.get(row['id'])['status'], 'download_failed')

    def test_manual_download_button_resets_exhausted_cycle_without_regenerating(self):
        service = self.service(); row, client = self.bound_row(service)
        service._save_transfer(row, 'download', {'count': 5, 'stopped': True, 'next_at': 0})
        service.execute({'action': 'retry_download_selected', 'ids': [row['id']]})
        self.assertEqual(retry_info(service.store.get(row['id']), 'download')['count'], 1)
        self.assertNotIn('run_tasks', [v['action'] for v in self.actions(client)])

    def test_missing_binding_probes_are_bounded_and_never_generate(self):
        service = self.service(); row, client = self.bound_row(service)
        service.store.update(row['id'], 'download_failed', remote={**row['remote'], 'bindings': []})
        service.watch_transfer_retries(); self.due(service, row['id'], 'download')
        for _ in range(11):
            current = service.store.get(row['id']); info = {**retry_info(current, 'download'), 'probe_at': 0}
            service._save_transfer(current, 'download', info); service.watch_transfer_retries()
        self.assertTrue(retry_info(service.store.get(row['id']), 'download')['stopped'])
        self.assertEqual(len([v for v in self.actions(client) if v['action'] == 'snapshot']), 10)
        self.assertNotIn('run_tasks', [v['action'] for v in self.actions(client)])

    def test_restart_upload_preserves_retry_count_and_never_resumes_paid_submission(self):
        service = self.service(); row, client = self.upload(service)
        service.store.recover(); current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'uploading'); self.assertFalse(retry_info(current, 'upload')['active'])
        service.store.reserve(row['id'], row['attempt'], 15, 'duration_rule')
        service.store.recover(); self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')

    def test_restart_download_keeps_saved_media_and_retry_count(self):
        service = self.service(); row, client = self.bound_row(service); service.retry_download(row)
        service.store.recover(); current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'download_failed'); self.assertEqual(retry_info(current, 'download')['count'], 1)
        self.assertEqual(current['remote']['bindings'], row['remote']['bindings'])

    def test_account_change_or_insufficient_balance_cannot_trigger_generation_after_retry(self):
        service = self.service(); row, client = self.upload(service)
        service._response(client, {'request_id': self.request(service, 'upload_images'), 'success': False, 'error': 'network'})
        client['credits'] = 1; self.due(service, row['id'], 'upload'); service.watch_transfer_retries()
        current = service.store.get(row['id']); self.assertEqual(current['status'], 'queued')
        self.assertFalse(current['attempt']); self.assertFalse(current['browser'])
        self.assertNotIn('run_tasks', [value['action'] for value in self.actions(client)])

    def test_upload_initial_wait_gets_a_recoverable_schedule(self):
        service = self.service(); row, client = self.upload(service)
        service.pending.clear(); service.store.update(row['id'], 'uploading', remote={**row['remote'], 'transfer_retries': {}})
        client['page_ready'] = False
        service.start_upload(service.store.get(row['id']))
        self.assertGreater(retry_info(service.store.get(row['id']), 'upload')['next_at'], 0)
        client['page_ready'] = True; self.due(service, row['id'], 'upload'); service.watch_transfer_retries()
        self.assertTrue(any(request['action'] == 'upload_images' for request in service.pending.values()))

    def test_zero_automatic_limit_still_allows_one_explicit_manual_download(self):
        service = self.service(); service.preferences['download_retry_limit'] = 0
        row, client = self.bound_row(service); service.watch_transfer_retries()
        self.assertTrue(retry_info(service.store.get(row['id']), 'download')['stopped'])
        service.retry_download(service.store.get(row['id']))
        self.assertEqual(service.store.get(row['id'])['status'], 'downloading')
        self.assertIn('手动', retry_detail(service.store.get(row['id']), service.preferences))

    def test_limits_status_text_and_explicit_ui_entry(self):
        self.assertEqual(retry_limit({}, 'upload'), 5); self.assertEqual(retry_limit({'download_retry_limit': 100}, 'download'), 10)
        dialog, _, commands = self.ui(); service = self.service(); row, client = self.bound_row(service)
        service.watch_transfer_retries(); current = service.store.get(row['id'])
        text = retry_detail(current, {}, now=retry_info(current, 'download')['next_at'] - 5)
        self.assertIn('5 秒后', text); self.assertIn('1/5', text)
        dialog.update_state({'jobs': [current], 'clients': [], 'paused': True, 'preferences': {}, 'spaces': []})
        dialog.queue.selectRow(1); dialog.retry_download_button.click()
        self.assertEqual(commands[-1]['action'], 'retry_download_selected')
        self.assertEqual(commands[-1]['ids'], [row['id']])
        dialog.upload_retries.setValue(2); dialog.download_retries.setValue(3); dialog.save_preferences()
        self.assertEqual(commands[-1]['value']['upload_retry_limit'], 2)
        self.assertEqual(commands[-1]['value']['download_retry_limit'], 3)


if __name__ == '__main__': unittest.main()
