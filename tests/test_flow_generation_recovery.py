"""Only an exact terminal generation failure may authorize a new paid attempt."""
import copy
import json
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.generation_recovery import confirmed_failure, watch
from app_plugins.builtin.flow_assistant.store import QueueStore


class GenerationRecoveryTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    observe = fixtures.CreditCooldownTests.observe

    def failed_task(self):
        service = self.service(); client = self.browser(service)
        client.update(batch_running=False); self.observe(service, client, 50)
        service.store.enqueue(self.jobs(duration=8, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', attempt='a'*32, browser=client['id'], remote={
            'account': client['account'], 'profile': client['profile'], 'upstream_task': 'task', 'cost': 12,
            'bindings': [{'schema': 2, 'taskId': 'task', 'submissionId': 'submission',
                'source': 'generation_response', 'mediaIds': ['new-media'], 'readyIds': []}]})
        service.store.reserve(row['id'], 'a'*32, 12, 'duration_rule')
        message = {'type': 'generation_failed', 'batch_id': 'a'*32, 'taskId': 'task',
            'submissionId': 'submission', 'mediaId': 'new-media', 'download_path': 'FlowBridge/'+'a'*32+'/result.mp4',
            'failure_source': 'refund_card', 'error': 'Flow 已明确提示生成失败并退款'}
        return service, client, service.store.get(row['id']), message

    def test_exact_failure_waits_and_never_invents_a_refund(self):
        service, client, row, message = self.failed_task()
        self.assertTrue(confirmed_failure(service, client, message))
        result = service.store.get(row['id']); info = result['remote']['generation_retry']
        self.assertEqual(result['status'], 'failed'); self.assertEqual(info['state'], 'waiting')
        self.assertAlmostEqual(info['next_at']-info['failed_at'], 120, delta=1)
        self.assertEqual(client['credits'], 50); self.assertFalse(service.pending)
        self.assertFalse(watch(service)); self.assertTrue(service.paused)

    def test_old_or_unbound_failure_and_successful_results_never_authorize_regeneration(self):
        service, client, row, message = self.failed_task()
        for change in ({'batch_id': 'b'*32}, {'mediaId': 'old-media'}, {'submissionId': 'old'},
                       {'taskId': 'other'}, {'failure_source': 'timeout'}, {'download_path': 'wrong'}):
            self.assertFalse(confirmed_failure(service, client, {**message, **change}), change)
        remote = copy.deepcopy(row['remote']); remote['bindings'][0]['readyIds'] = ['new-media']
        service.store.update(row['id'], 'generating', remote=remote)
        self.assertFalse(confirmed_failure(service, client, message)); self.assertFalse(watch(service))

    def test_late_snapshots_and_status_do_not_revive_the_failed_attempt(self):
        service, client, row, message = self.failed_task(); confirmed_failure(service, client, message)
        service._status(client, {'batch_id': row['attempt'], 'status': 'failed', 'message': 'old late status'})
        service._snapshot(client, {'url': client['project'], 'running': False, 'batch_id': row['attempt'], 'tasks': [{
            'taskId': 'task', 'download_path': message['download_path'], 'mediaBindings': row['remote']['bindings'], 'status': ['failed']}]})
        self.assertEqual(service.store.get(row['id'])['status'], 'failed')
        self.assertEqual(service.store.get(row['id'])['remote']['generation_retry']['state'], 'waiting')

    def test_retry_waits_for_fresh_points_then_gets_a_new_attempt_and_keeps_budget_charges(self):
        service, client, row, message = self.failed_task(); service.paused = False
        confirmed_failure(service, client, message)
        when = service.store.get(row['id'])['remote']['generation_retry']['next_at'] + .1
        with patch('time.time', return_value=when):
            service._tick()
            current = service.store.get(row['id']); self.assertEqual(current['status'], 'queued')
            self.assertEqual(current['payload']['generation_recovery']['count'], 1)
            reads = [key for key, request in service.pending.items() if request['action'] == 'get_points']
            self.assertEqual(len(reads), 1); self.assertFalse(any(p['action'] == 'run_tasks' for p in service.pending.values()))
            service._response(client, {'request_id': reads[0], 'success': True,
                'data': {'email': client['account'], 'remaining_points': 50}})
            service._tick(); current = service.store.get(row['id'])
            self.assertEqual(current['status'], 'preparing'); self.assertNotEqual(current['attempt'], row['attempt'])
            key = next(key for key, request in service.pending.items() if request['action'] == 'prepare')
            service._response(client, {'request_id': key, 'success': True, 'data': {'verified': True,
                'model': 'omni_1_1_flash', 'email': client['account'], 'cost': 12}})
            key = next(key for key, request in service.pending.items() if request['action'] == 'upload_images')
            service._response(client, {'request_id': key, 'success': True})
        self.assertEqual(service.store.get(row['id'])['status'], 'submitting')
        self.assertEqual(service.store.batches()[row['payload']['batch_id']]['reserved'], 24)
        sent = [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list]
        self.assertEqual(sum(item.get('action') == 'run_tasks' for item in sent), 1)

    def test_queue_pause_low_credit_cooldown_and_permanent_errors_remain_protected(self):
        service, client, row, message = self.failed_task()
        confirmed_failure(service, client, message)
        info = service.store.get(row['id'])['remote']['generation_retry']; info['next_at'] = 0
        service.store.update(row['id'], 'failed', remote={**row['remote'], 'generation_retry': info})
        self.assertFalse(watch(service), 'a confirmed failure does not unpause the queue')
        service.paused = False; self.observe(service, client, 9)
        service._tick()
        self.assertFalse(any(request['action'] == 'get_points' for request in service.pending.values()))
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        service.store.update(row['id'], 'generating', attempt=row['attempt'], browser=client['id'], remote=row['remote'])
        confirmed_failure(service, client, {**message, 'error': 'generation rejected by safety policy'})
        self.assertEqual(service.store.get(row['id'])['remote']['generation_retry']['state'], 'stopped')

    def test_retry_limit_survives_restart_and_original_failed_attempt_cannot_be_requeued_twice(self):
        service, client, row, message = self.failed_task()
        payload = {**row['payload'], 'generation_recovery': {'count': 2}}
        service.store.set_payload(row['id'], payload)
        self.assertTrue(confirmed_failure(service, client, message))
        self.assertEqual(service.store.get(row['id'])['remote']['generation_retry']['state'], 'stopped')
        self.assertFalse(watch(service))
        self.assertTrue(service.store.requeue_failed_generation(row['id'], row['attempt'], payload, {}))
        self.assertFalse(service.store.requeue_failed_generation(row['id'], row['attempt'], payload, {}))
        self.assertEqual(QueueStore(self.namespace).get(row['id'])['payload']['generation_recovery']['count'], 2)

    def test_failed_media_is_not_archived_after_a_late_ready_hint(self):
        service, client, row, message = self.failed_task(); confirmed_failure(service, client, message)
        remote = service.store.get(row['id'])['remote']
        remote['bindings'][0].update(failedIds=['new-media'], readyIds=['new-media'])
        service.store.update(row['id'], 'failed', remote=remote)
        data = {'taskId':'task','submissionId':'submission','mediaId':'new-media','state':'complete',
                'download_path':message['download_path'],'filename':'untrusted.mp4'}
        with patch.object(service, 'archive') as archive:
            service._download(client, {'batch_id':row['attempt'],'event_id':'late-ready','data':data})
        archive.assert_not_called()
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        service.paused = False; self.assertFalse(watch(service))


if __name__ == '__main__': unittest.main()
