"""Recovery receipt ordering and snapshot regressions; isolated business data."""
from contextlib import closing
import json
import unittest
from unittest.mock import patch

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.page_recovery import record_response, schedule, watch


class LateGenerationTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def row(self):
        service = self.service(); service.store.enqueue(self.jobs()); client = self.client(service)
        row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', attempt='a'*32, browser=client['id'], remote={
            'profile': client['profile'], 'upstream_task': 'task', 'bindings': [{
                'schema': 2, 'taskId': 'task', 'submissionId': 'submission', 'mediaIds': [], 'readyIds': []}]})
        message = {'batch_id': 'a'*32, 'taskId': 'task', 'submissionId': 'submission',
            'download_path': service.download_name(service.store.get(row['id'])), 'response': {'acknowledged': True}}
        return service, service.store.get(row['id']), client, message

    def saved(self, service):
        with closing(service.namespace.file('generation-diagnostics.sqlite3').connect(read_only=True)) as db:
            return json.loads(db.execute('SELECT evidence FROM responses').fetchone()[0])

    def test_receipt_after_recovery_started_is_saved_immediately(self):
        service, row, client, message = self.row()
        schedule(service, client, message)
        record_response(service, client, message)
        current = service.store.get(row['id'])
        self.assertIn('generation_response', current['remote'])
        self.assertEqual(self.saved(service)['response'], message['response'])
        self.assertFalse(getattr(service, '_generation_responses', {}))

    def test_receipt_after_probes_completed_is_still_saved(self):
        service, row, client, message = self.row()
        schedule(service, client, message)
        current = service.store.get(row['id'])
        service.store.update(row['id'], 'uncertain', remote={**current['remote'],
            'page_recovery': {'count': 3, 'next_at': 0}})
        watch(service)
        record_response(service, client, message)
        self.assertIn('generation_response', service.store.get(row['id'])['remote'])
        self.assertEqual(self.saved(service)['response'], message['response'])

    def test_null_response_is_diagnostic_evidence_not_media_ownership(self):
        service, row, client, message = self.row()
        record_response(service, client, {**message, 'response': None})
        schedule(service, client, message)
        self.assertIsNone(self.saved(service)['response'])
        self.assertFalse(service.store.get(row['id'])['remote']['bindings'][0]['mediaIds'])

    def test_response_updates_same_diagnostic_row_without_growing_storage(self):
        service, row, client, message = self.row()
        schedule(service, client, message)
        record_response(service, client, {**message, 'response': None})
        record_response(service, client, message)
        self.assertEqual(self.saved(service)['response'], message['response'])
        with closing(service.namespace.file('generation-diagnostics.sqlite3').connect(read_only=True)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM responses').fetchone()[0], 1)

    def test_replaced_task_or_submission_cannot_supply_receipt(self):
        service, row, client, message = self.row()
        for wrong in ({'taskId': 'another-task'}, {'submissionId': 'another-submission'},
                      {'batch_id': 'b'*32}, {'download_path': 'FlowBridge/'+'b'*32+'/result.mp4'}):
            record_response(service, client, {**message, **wrong})
        self.assertFalse(getattr(service, '_generation_responses', {}))

    def test_diagnostic_write_failure_does_not_prevent_recovery(self):
        service, row, client, message = self.row()
        record_response(service, client, message)
        with patch.object(service.namespace, 'file', side_effect=OSError('disk unavailable')):
            schedule(service, client, message)
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'uncertain')
        self.assertIn('page_recovery', current['remote'])
        self.assertEqual(current['remote']['diagnostic_error'], 'OSError')

    def test_cached_running_snapshot_does_not_erase_missing_binding_state(self):
        service, row, client, message = self.row()
        schedule(service, client, message)
        service._snapshot(client, {'batch_id': row['attempt'], 'running': True, 'tasks': [{
            'taskId': 'task', 'download_path': message['download_path'], 'status': ['generating'],
            'mediaBindings': row['remote']['bindings'], 'downloadIds': [None]}]})
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')


if __name__ == '__main__': unittest.main()
