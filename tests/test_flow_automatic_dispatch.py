"""Automatic dispatch, quiet session accounting and legacy download recovery."""
import copy
import json
import time
import unittest
from flow_reference_fixtures import upload_receipt
from unittest.mock import patch

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.routing import dispatch_rank
from app_plugins.builtin.flow_assistant.credit_policy import balance_ready
from app_plugins.builtin.flow_assistant.policy import waiting_reason
from app_plugins.builtin.flow_assistant.store import choose_client
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION
from app_plugins.builtin.flow_assistant.transfer_policy import retry_info


class AutomaticDispatchTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def test_idle_old_ten_second_profile_automatically_accepts_six(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=6, budget=0))
        client = self.client(service, 'OldTen')
        service.preferences.update(profile_lanes={'OldTen': 10}, auto_lane=True)
        service.paused = False; service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')
        self.assertEqual(service.preferences['profile_lanes']['OldTen'], 6)
        self.assertEqual(service.preferences['profile_lane_modes']['OldTen'], 'automatic')
        request = json.loads(client['socket'].sendTextMessage.call_args.args[0])
        self.assertEqual(request['params']['duration'], 6)

    def test_already_matching_job_precedes_reassignment(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=10, budget=0) + self.jobs(duration=6, budget=0))
        self.client(service, 'Six'); service.preferences.update(profile_lanes={'Six': 6})
        service.paused = False; service._tick()
        self.assertEqual([r['status'] for r in service.store.rows()], ['queued', 'preparing'])
        self.assertEqual(service.preferences['profile_lanes']['Six'], 6)

    def test_offline_old_ten_profile_is_reassigned_and_opened_for_six(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=6, budget=0))
        service.preferences.update(profiles=['OldTen'], profile_lanes={'OldTen': 10},
            profile_projects={'OldTen': 'https://flow.google.com/project/fixture'})
        launches = []; service.launch_requested.connect(launches.append)
        service.paused = False; service._tick()
        self.assertEqual(launches, ['OldTen'])
        self.assertEqual(service.preferences['profile_lanes']['OldTen'], 6)

    def test_generation_and_pending_download_keep_their_profile_and_account(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=10, budget=0) + self.jobs(duration=6, budget=0))
        client = self.client(service, 'Busy'); client.update(page_ready=True, page_at=time.time())
        row = service.store.rows()[0]
        service.preferences.update(profile_lanes={'Busy': 10}, concurrency=2)
        service.paused = False
        for state in ('generating', 'downloading', 'download_failed', 'uncertain'):
            with self.subTest(state=state):
                service.pending.clear()
                service.store.update(row['id'], state, browser=client['id'], remote={
                    'account': client['account'], 'profile': 'Busy', 'activity_at': time.time()})
                service._tick()
                self.assertEqual(service.preferences['profile_lanes']['Busy'], 10)
                self.assertEqual(service.store.rows()[1]['status'], 'queued')
                self.assertFalse(any(p['action'] == 'prepare' for p in service.pending.values()))

    def test_offline_held_profile_is_not_relaunched_or_reassigned(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=10, budget=0) + self.jobs(duration=6, budget=0))
        row = service.store.rows()[0]
        service.store.update(row['id'], 'uncertain', remote={'profile': 'Busy'})
        service.preferences.update(profiles=['Busy'], profile_lanes={'Busy': 10},
            profile_projects={'Busy': 'https://flow.google.com/project/fixture'})
        launches = []; service.launch_requested.connect(launches.append)
        service.paused = False; service._tick()
        self.assertFalse(launches); self.assertEqual(service.preferences['profile_lanes']['Busy'], 10)

    def test_manual_fixed_lane_has_opt_out_and_can_return_to_automatic(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=6, budget=0))
        client = self.client(service, 'Fixed')
        service.execute({'action': 'lane', 'scope': 'profile', 'key': 'Fixed', 'duration': 10})
        service.paused = False; service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'queued')
        service.execute({'action': 'lane', 'scope': 'profile', 'key': 'Fixed', 'mode': 'automatic'})
        service._tick(); self.assertEqual(service.store.rows()[0]['status'], 'preparing')
        self.assertEqual(service.preferences['profile_lanes']['Fixed'], 6)

    def test_reassignment_requires_sufficient_credit_and_never_changes_job(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=6, budget=0)); row = service.store.rows()[0]
        client = self.client(service, 'Ten', credits=9)
        service.preferences.update(profile_lanes={'Ten': 10})
        service.paused = False; service._tick()
        self.assertEqual(service.preferences['profile_lanes']['Ten'], 10)
        self.assertEqual(service.store.get(row['id'])['payload'], row['payload'])
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')

    def test_old_session_balance_does_not_expire_or_trigger_menu_polling(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=6, budget=0))
        client = self.client(service); client.update(credits_local=True, credits_at=time.time()-3600)
        self.assertTrue(balance_ready(client)); service.paused = False
        service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')
        self.assertFalse(any(p['action'] == 'get_points' for p in service.pending.values()))

    def test_credit_is_debited_before_send_once_and_shared_across_windows(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=10, budget=0))
        client = self.client(service, 'One', credits=50); other = self.client(service, 'Two', credits=50)
        other['account'] = client['account']
        row = service.store.rows()[0]
        service.store.update(row['id'], 'uploading', attempt='a'*32, browser=client['id'],
            remote={'account': client['account'], 'cost': 15, 'cost_source': 'duration_rule'})
        service.request(client, 'upload_images', job=row['id'])
        request = next(iter(service.pending)); seen = []
        client['socket'].sendTextMessage.side_effect = lambda text: seen.append(client['credits'])
        service._response(client, {'request_id': request, 'success': True, 'data': upload_receipt(service, row['id'])})
        self.assertEqual(seen, [35]); self.assertEqual(other['credits'], 35)
        service._response(client, {'request_id': request, 'success': True})
        run = next(key for key, value in service.pending.items() if value['action'] == 'run_tasks')
        service._response(client, {'request_id': run, 'success': True})
        self.assertEqual(client['credits'], 35); self.assertTrue(client['credits_estimated'])

    def test_completed_status_only_requests_snapshot_not_points(self):
        service = self.service(); service.store.enqueue(self.jobs())
        client = self.client(service); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', attempt='a'*32, browser=client['id'])
        service._status(client, {'batch_id': 'a'*32, 'status': 'success'})
        self.assertEqual({p['action'] for p in service.pending.values()}, {'snapshot'})

    def test_explicit_refresh_still_queries_and_resets_estimate(self):
        service = self.service(); client = self.client(service)
        client.update(credits=35, credits_estimated=True, credits_local=True)
        service.execute({'action': 'credits'})
        request = next(key for key, p in service.pending.items() if p['action'] == 'get_points')
        service._response(client, {'request_id': request, 'success': True,
            'data': {'email': client['account'], 'remaining_points': 42}})
        self.assertEqual(client['credits'], 42); self.assertTrue(client['credits_local'])
        self.assertFalse(client['credits_estimated'])

    def test_pending_credit_query_is_not_duplicated_and_late_balance_cannot_undo_debit(self):
        service = self.service(); client = self.client(service)
        service.request(client, 'get_points'); service.request(client, 'get_points')
        self.assertEqual(len(service.pending), 1)
        request = next(iter(service.pending))
        service.credit_debits[client['account'].casefold()] = 15
        client.update(credits=35, credits_local=True)
        service._response(client, {'request_id': request, 'success': True,
            'data': {'email': client['account'], 'remaining_points': 50}})
        self.assertEqual(client['credits'], 35); self.assertTrue(client['credits_estimated'])

    def test_old_domain_rejection_rearms_once_only_after_matching_extension_update(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=10, budget=0)); client = self.client(service)
        row = service.store.rows()[0]; media = 'bound_media'
        service.store.update(row['id'], 'download_failed', attempt='a'*32, browser=client['id'],
            error='下载域名不受信任，已安全拦截', remote={'bindings': [{
                'schema': 2, 'source': 'generation_response', 'taskId': 'task', 'submissionId': 'sub', 'mediaIds': [media], 'readyIds': [media],
                'urls': {media: 'https://flow.google.com/asb/'+media+'=s0,v0,x1'}}],
                'upstream_task': 'task', 'transfer_retries': {'download': {'count': 5, 'stopped': True, 'attempt': 'a'*32}}})
        client['extension_version'] = '1.1.2'; service.recover_extension_downloads(client)
        self.assertTrue(retry_info(service.store.get(row['id']), 'download')['stopped'])
        client['extension_version'] = EXTENSION_VERSION; service.recover_extension_downloads(client)
        info = retry_info(service.store.get(row['id']), 'download')
        self.assertEqual(info['count'], 0); self.assertFalse(info['stopped']); self.assertTrue(info['asb_recovery'])
        with patch.object(service.store, 'update') as update:
            service.recover_extension_downloads(client); update.assert_not_called()


if __name__ == '__main__': unittest.main()
