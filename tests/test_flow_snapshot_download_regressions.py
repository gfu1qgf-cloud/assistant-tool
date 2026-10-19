"""Additional boundary cases; synthetic state and temporary files only."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_assistant as fixtures
import test_flow_production as production
from app_plugins.builtin.flow_assistant.transfer_policy import retry_info


class FlowBugAuditTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client
    bound_row = production.ProductionTests.bound_row

    def snapshot(self, service, row, bindings):
        return {'batch_id': row['attempt'], 'running': True,
                'url': 'https://labs.google/project/test', 'controls_ready': True,
                'tasks': [{'taskId': 'task', 'download_path': service.download_name(row),
                           'mediaBindings': bindings}]}

    def test_empty_snapshot_preserves_identified_unfinished_media(self):
        service = self.service()
        row, client = self.bound_row(service)
        remote = copy.deepcopy(row['remote'])
        remote['bindings'][0].update(readyIds=[], urls={})
        service.store.update(row['id'], 'generating', remote=remote)
        service._snapshot(client, self.snapshot(service, row, []))
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'], remote['bindings'],
                         'A temporary empty page state must not erase generation-response ownership')

    def test_partial_snapshot_preserves_saved_ready_url(self):
        service = self.service()
        row, client = self.bound_row(service)
        partial = copy.deepcopy(row['remote']['bindings'])
        partial[0].update(readyIds=[], urls={})
        service._snapshot(client, self.snapshot(service, row, partial))
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'], row['remote']['bindings'],
                         'The same identity with incomplete readiness must not erase the saved download URL')

    def test_successful_snapshot_does_not_later_timeout(self):
        service = self.service()
        client = self.client(service)
        service.request(client, 'snapshot')
        for request in service.pending.values():
            request['at'] = time.time() - 61
        service._snapshot(client, {'batch_id': '', 'tasks': [],
            'url': 'https://labs.google/project/test', 'controls_ready': True})
        self.assertEqual(client.get('page_error'), '')
        service._tick()
        self.assertEqual(client.get('page_error'), '',
                         'An answered snapshot must not produce a delayed page timeout')

    def test_refresh_original_media_accepts_supported_asb_url(self):
        service = self.service()
        row, client = self.bound_row(service)
        command = {'action': 'refresh_download_url', 'job': row['id'],
                   'url': 'https://flow.google.com/asb/media?signature=synthetic'}
        with patch.object(service, 'retry_download') as retry:
            service.extra_action(command)
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'][0]['urls']['media'], command['url'])
        retry.assert_called_once()

    def test_old_download_interrupt_does_not_cancel_new_retry_before_ack(self):
        service = self.service()
        row, client = self.bound_row(service)
        info = {'attempt': row['attempt'], 'count': 1, 'active': False,
                'download_id': 8, 'next_at': time.time() - 1}
        service.store.update(row['id'], 'download_failed', remote={**row['remote'],
            'transfer_retries': {'download': info}})
        service.retry_download(service.store.get(row['id']), automatic=True)
        request_id = next(key for key, value in service.pending.items()
                          if value['action'] == 'retry_download')
        service._download(client, {'batch_id': row['attempt'], 'event_id': 'late-old-interrupt',
            'data': {'state': 'interrupted', 'error': 'NETWORK_FAILED', 'downloadId': 8,
                     'filename': 'synthetic.mp4', 'download_path': service.download_name(row),
                     'taskId': 'task', 'submissionId': 'submission', 'mediaId': 'media'}})
        service._response(client, {'request_id': request_id, 'success': True,
                                  'data': {'downloadId': 9}})
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'downloading')
        self.assertEqual(retry_info(current, 'download').get('download_id'), 9)

    def test_current_download_failure_before_ack_is_still_processed(self):
        service = self.service()
        row, client = self.bound_row(service)
        service.retry_download(row)
        request_id = next(key for key, value in service.pending.items()
                          if value['action'] == 'retry_download')
        event = {'batch_id': row['attempt'], 'event_id': 'current-interrupt',
            'data': {'state': 'interrupted', 'error': 'NETWORK_FAILED', 'downloadId': 9,
                     'download_path': service.download_name(row), 'taskId': 'task',
                     'submissionId': 'submission', 'mediaId': 'media'}}
        service._download(client, event)
        self.assertIn(request_id, service.pending)
        self.assertEqual(service.store.get(row['id'])['status'], 'downloading')
        service._response(client, {'request_id': request_id, 'success': True, 'data': {'downloadId': 9}})
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'download_failed')
        self.assertIn('NETWORK_FAILED', current['error'])
        self.assertNotIn('deferred_download_interrupts', current['remote'])
        service._download(client, event)
        self.assertEqual(current, service.store.get(row['id']))

    def test_fresh_signed_url_and_ready_state_survive_later_partial_snapshot(self):
        service = self.service()
        row, client = self.bound_row(service)
        updated = copy.deepcopy(row['remote']['bindings'])
        fresh = 'https://flow.google.com/asb/media=s0,v0?signature=fresh'
        updated[0]['urls']['media'] = fresh
        service._snapshot(client, self.snapshot(service, row, updated))
        partial = copy.deepcopy(updated)
        partial[0].update(readyIds=[], urls={})
        service._snapshot(client, self.snapshot(service, row, partial))
        current = service.store.get(row['id'])
        self.assertEqual(current['remote']['bindings'][0]['urls']['media'], fresh)
        self.assertEqual(current['remote']['bindings'][0]['readyIds'], ['media'])
        with patch.object(service, 'request') as request:
            service.retry_download(current)
        self.assertEqual(request.call_args.args[1], 'retry_download')
        self.assertEqual(request.call_args.args[2]['restore']['bindings'][0]['urls']['media'], fresh)

    def test_snapshot_does_not_clear_an_unanswered_credit_request(self):
        service = self.service()
        client = self.client(service)
        service.request(client, 'get_points')
        service.request(client, 'snapshot')
        service._snapshot(client, {'tasks': [], 'url': 'https://labs.google/project/test'})
        self.assertEqual([r['action'] for r in service.pending.values()], ['get_points'])

    def test_asb_address_still_rejects_other_media_and_unsafe_urls(self):
        service = self.service()
        row, client = self.bound_row(service)
        for url in ('http://flow.google.com/asb/media', 'https://flow.google.com.evil.test/asb/media',
                    'https://flow.google.com:8443/asb/media', 'https://user@flow.google.com/asb/media',
                    'https://flow.google.com/asb/another', 'https://flow.google.com/asb/media/extra'):
            before = service.store.get(row['id'])
            with self.subTest(url=url), self.assertRaises(ValueError):
                service.extra_action({'action': 'refresh_download_url', 'job': row['id'], 'url': url})
            self.assertEqual(before, service.store.get(row['id']))


if __name__ == '__main__':
    unittest.main()
