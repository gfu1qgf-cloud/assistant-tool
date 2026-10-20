"""Consecutive desktop jobs must wait for the previous page batch to finish."""
import time
import unittest

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.policy import waiting_reason
from app_plugins.builtin.flow_assistant.store import choose_client

BUSY = '插件队列正在运行，请先停止'


class BatchHandoffTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def page(self, service, client, running, *, url=None):
        service._snapshot(client, {'url': url or client['saved_project'], 'running': running,
            'batch_id': 'a' * 32, 'tasks': [{'taskId': 'old-task',
                'download_path': 'FlowBridge/' + 'a' * 32 + '/result.mp4', 'mediaBindings': []}]})

    def test_first_archive_finishing_does_not_release_a_running_page_batch(self):
        service = self.service()
        client = self.client(service, credits=52)
        client.update(page_ready=True, page_at=time.time(), credits_local=True)
        service.store.enqueue(self.jobs(count=2, duration=8, budget=0))
        first, second = service.store.rows()
        service.store.update(first['id'], 'completed', attempt='a' * 32,
                             browser=client['id'], remote={'account': client['account']})
        self.page(service, client, True)
        service.paused = False
        service._tick()
        self.assertEqual(service.store.get(second['id'])['status'], 'queued')
        self.assertFalse(any(p['action'] == 'prepare' for p in service.pending.values()))
        self.page(service, client, False)
        service._tick()
        self.assertEqual(service.store.get(second['id'])['status'], 'preparing')

    def test_same_account_window_cannot_bypass_page_batch_drain(self):
        service = self.service()
        one = self.client(service, 'One')
        two = self.client(service, 'Two')
        two['account'] = one['account']
        one['batch_running'] = True
        self.assertIsNone(choose_client([one, two], [], 2, self.jobs(duration=8, budget=0)[0], {}, 12))
        other = self.client(service, 'Other')
        self.assertEqual(choose_client([one, two, other], [], 2,
            self.jobs(duration=8, budget=0)[0], {}, 12)['id'], other['id'])

    def test_busy_prepare_waits_before_upload_reservation_or_paid_command(self):
        service = self.service()
        client = self.client(service, credits=52)
        client.update(page_ready=True, page_at=time.time())
        service.store.enqueue(self.jobs(duration=8, budget=0))
        service.paused = False
        service._tick()
        request = next(key for key, p in service.pending.items() if p['action'] == 'prepare')
        service._response(client, {'request_id': request, 'success': False,
            'code': 'FLOW_BATCH_BUSY', 'error': '上一批网页队列仍在收尾'})
        row = service.store.rows()[0]
        self.assertEqual(row['status'], 'queued')
        self.assertFalse(row['attempt'])
        self.assertTrue(client['batch_running'])
        self.assertFalse(service.preferences.get('browser_blocks'))
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 0)
        self.assertFalse(any(p['action'] in {'upload_images', 'run_tasks'} for p in service.pending.values()))
        service._tick()
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        self.page(service, client, False)
        service._tick()
        self.assertEqual(service.store.get(row['id'])['status'], 'preparing')

    def rejected(self, service, **remote):
        service.store.enqueue(self.jobs(duration=8, budget=0))
        row = service.store.rows()[0]
        service.store.update(row['id'], 'uncertain', attempt='b' * 32, browser='browser',
            remote={'cost': 12, 'cost_source': 'duration_rule', **remote}, error=BUSY)
        service.store.reserve(row['id'], 'b' * 32, 12, 'duration_rule')
        return service.store.get(row['id'])

    def test_explicit_retry_of_exact_pre_accept_rejection_releases_only_unused_reservation(self):
        service = self.service()
        row = self.rejected(service)
        service.store.recover()
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        service.store.retry([row['id']])
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'queued')
        self.assertFalse(current['attempt'])
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 0)
        service.store.retry([row['id']])
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 0)

    def test_unknown_or_evidenced_submission_is_not_requeued_or_refunded(self):
        for remote in ({'upstream_task': 'actual-task'}, {'generation_response': {'sha256': 'proof'}},
                       {'download': {'state': 'interrupted'}}):
            service = self.service()
            row = self.rejected(service, **remote)
            service.store.retry([row['id']])
            self.assertNotEqual(service.store.get(row['id'])['status'], 'queued')
            self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 12)
        service = self.service()
        row = self.rejected(service)
        service.store.update(row['id'], 'uncertain', error='浏览器响应超时，保留记录。')
        service.store.retry([row['id']])
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 12)

    def test_finishing_state_and_queue_reason_are_not_credit_shortage(self):
        service = self.service()
        client = self.client(service, credits=52)
        client.update(page_ready=True, batch_running=True)
        service.store.enqueue(self.jobs(duration=8, budget=0))
        row = service.store.rows()[0]
        self.assertIn('收尾', browser_status(client, {})['reason'])
        reason = waiting_reason(row, [client], [row], {}, {}, {})
        self.assertIn('收尾', reason)
        self.assertNotIn('积分不足', reason)

    def test_unrelated_project_cannot_clear_old_batch_busy_state(self):
        service = self.service()
        client = self.client(service)
        client['batch_running'] = True
        self.page(service, client, False, url='https://labs.google/fx/tools/flow/project/another')
        self.assertTrue(client['batch_running'])

    def test_retry_keeps_other_attempt_charges_and_does_not_invent_credit_refund(self):
        service = self.service()
        client = self.client(service, credits=28)
        row = self.rejected(service)
        service.store.reserve(row['id'], 'c' * 32, 12, 'duration_rule')
        service.store.retry([row['id']])
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 12)
        self.assertEqual(client['credits'], 28)
        with service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM charges').fetchone()[0], 2)
            self.assertTrue(any('12' in message for (message,) in db.execute('SELECT message FROM history')))

    def test_page_drain_keeps_native_guard_busy_and_manual_credit_probe_deferred(self):
        service = self.service()
        client = self.client(service)
        client['batch_running'] = True
        observed = []
        service.running_changed.connect(observed.append)
        service.publish()
        self.assertTrue(observed[-1])
        service.execute({'action': 'credits', 'keys': ['profile:' + client['profile']], 'manual': True})
        self.assertEqual(client['credit_check']['state'], 'deferred')
        self.assertFalse(any(p['action'] == 'get_points' for p in service.pending.values()))


if __name__ == '__main__':
    unittest.main()
