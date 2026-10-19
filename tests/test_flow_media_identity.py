"""Prevent cross-task videos without generating or touching any real records."""
import copy
import json
import unittest
from unittest.mock import patch

import test_flow_assistant as fixtures
import test_flow_production as production
from app_plugins.builtin.flow_assistant.media_identity import generated_bindings, owns_download
from app_plugins.builtin.flow_assistant.transfer_policy import retry_info


class MediaIdentityTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client
    bound_row = production.ProductionTests.bound_row

    def event(self, service, row, **updates):
        data = {'state': 'complete', 'filename': 'synthetic.mp4', 'download_path': service.download_name(row),
                'taskId': 'task', 'submissionId': 'submission', 'mediaId': 'media', **updates}
        return {'batch_id': row['attempt'], 'event_id': 'test-event', 'data': data}

    def test_only_exact_generation_response_owns_a_download(self):
        service = self.service(); row, _ = self.bound_row(service)
        event = self.event(service, row)
        self.assertTrue(owns_download(row, event['data']))
        for key in ('taskId', 'submissionId', 'mediaId'):
            wrong = {**event['data'], key: 'another-task-or-video'}
            self.assertFalse(owns_download(row, wrong), key)
        for source in ('dom', 'polling', None):
            wrong = copy.deepcopy(row); wrong['remote']['bindings'][0]['source'] = source
            self.assertFalse(owns_download(wrong, event['data']))
        wrong = copy.deepcopy(row); wrong['remote']['upstream_task'] = 'other-task'
        self.assertFalse(owns_download(wrong, event['data']))
        wrong = copy.deepcopy(row); wrong['remote']['bindings'][0]['readyIds'] = []
        self.assertFalse(owns_download(wrong, event['data']))
        for malformed in (None, {}, 'media', [None, {'schema': 2}], [{'schema': 2, 'source': 'generation_response',
                'taskId': 'task', 'submissionId': 'sub', 'mediaIds': 'media', 'readyIds': 'media'}]):
            self.assertEqual(generated_bindings({'bindings': malformed}), [])

    def test_wrong_media_is_not_archived_and_never_regenerates(self):
        service = self.service(); row, client = self.bound_row(service)
        with patch.object(service, 'archive') as archive:
            service._download(client, self.event(service, row, mediaId='unrelated-page-video'))
            archive.assert_not_called()
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'uncertain')
        self.assertNotIn('download', current['remote'])
        self.assertIn('归属证据', current['error'])
        actions = [json.loads(c.args[0]).get('action') for c in client['socket'].sendTextMessage.call_args_list]
        self.assertIn('snapshot', actions); self.assertNotIn('run_tasks', actions)

    def test_download_arriving_before_its_snapshot_recovers_once(self):
        service = self.service(); row, client = self.bound_row(service)
        bindings = row['remote']['bindings']
        service.store.update(row['id'], 'generating', remote={**row['remote'], 'bindings': [], 'upstream_task': ''})
        event = self.event(service, row)
        snapshot = {'batch_id': row['attempt'], 'running': True, 'tasks': [{'taskId': 'task',
            'download_path': service.download_name(row), 'mediaBindings': bindings}]}
        with patch.object(service, 'archive') as archive:
            service._download(client, event); archive.assert_not_called()
            service._snapshot(client, snapshot); archive.assert_called_once_with(row['id'])
            service._download(client, event); self.assertEqual(archive.call_count, 1)
        self.assertNotIn('unverified_download', service.store.get(row['id'])['remote'])

    def test_archive_rechecks_persisted_binding(self):
        service = self.service(); row, _ = self.bound_row(service)
        service.store.update(row['id'], 'archive_failed', remote={**row['remote'],
            'download': self.event(service, row, taskId='other-task')['data']})
        with patch.object(service.pool, 'submit') as submit:
            service.archive(row['id']); submit.assert_not_called()
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')

    def test_snapshot_cannot_replace_confirmed_task_or_media_identity(self):
        service = self.service(); row, client = self.bound_row(service)
        for task_id, media_id in (('other-task', 'media'), ('task', 'other-media')):
            bindings = copy.deepcopy(row['remote']['bindings']); bindings[0]['mediaIds'] = [media_id]
            snapshot = {'batch_id': row['attempt'], 'running': True, 'tasks': [{'taskId': task_id,
                'download_path': service.download_name(row), 'mediaBindings': bindings}]}
            service._snapshot(client, snapshot)
            current = service.store.get(row['id'])
            self.assertEqual(current['status'], 'uncertain')
            self.assertEqual(current['remote']['bindings'], row['remote']['bindings'])
            self.assertEqual(current['remote']['upstream_task'], 'task')

    def test_late_notifications_do_not_downgrade_or_overwrite_finished_records(self):
        service = self.service(); row, client = self.bound_row(service)
        for status in ('completed', 'needs_review', 'cancelled', 'archiving'):
            service.store.update(row['id'], status)
            before = service.store.get(row['id'])
            with patch.object(service, 'archive') as archive:
                service._download(client, self.event(service, row, mediaId='wrong'))
                archive.assert_not_called()
            self.assertEqual(before, service.store.get(row['id']))

    def test_legacy_binding_is_not_silently_upgraded_or_downloaded(self):
        service = self.service(); row, client = self.bound_row(service)
        remote = copy.deepcopy(row['remote']); remote['bindings'][0].pop('source')
        service.store.update(row['id'], 'download_failed', remote=remote)
        service.retry_download(service.store.get(row['id']), automatic=True)
        current = service.store.get(row['id'])
        self.assertTrue(retry_info(current, 'download')['stopped'])
        self.assertEqual(current['remote']['bindings'], remote['bindings'])
        self.assertNotIn('retry_download', [r['action'] for r in service.pending.values()])
        self.assertNotIn('run_tasks', [r['action'] for r in service.pending.values()])


if __name__ == '__main__': unittest.main()
