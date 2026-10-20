"""Exercise cross-stage failures without contacting Flow or spending credits."""
import time
import unittest
from flow_reference_fixtures import upload_receipt

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.store import choose_client


class FlowAuditStateTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def browser(self, service, profile='Profile 1', credits=50):
        client = self.client(service, profile, credits)
        client.update(account=profile.replace(' ', '').lower()+'@example.test',
                      page_ready=True, page_at=time.time(), credits_local=True)
        return client

    def job(self, service, client, status='preparing', remote=None):
        service.store.enqueue(self.jobs(duration=8, budget=0))
        row = service.store.rows()[-1]
        service.store.update(row['id'], status, attempt=row['id'], browser=client['id'],
            remote={'profile': client['profile'], 'account': client['account'], **(remote or {})})
        return service.store.get(row['id'])

    def receipt(self, service, client, row, action, **updates):
        service.request(client, action, {}, row['id'])
        key = next(k for k, p in service.pending.items() if p['action'] == action and p['job'] == row['id'])
        return {'request_id': key, 'success': True, 'data': upload_receipt(service, row['id']) if action == 'upload_images' else {}, **updates}

    def test_wrong_browser_cannot_consume_the_owning_request(self):
        service = self.service(); one = self.browser(service); two = self.browser(service, 'Profile 2')
        row = self.job(service, one)
        reply = self.receipt(service, one, row, 'prepare', data={'verified': True, 'model': 'omni_1_1_flash'})
        service._response(two, reply)
        self.assertIn(reply['request_id'], service.pending)
        service._response(one, reply)
        self.assertEqual(service.store.get(row['id'])['status'], 'uploading')

    def test_failed_credit_response_cannot_authorize_partial_account_data(self):
        service = self.service(); client = self.browser(service)
        client.update(credits_waiting_ready=True, credits_waiting_account=True)
        service.request(client, 'get_points')
        key = next(iter(service.pending))
        service._response(client, {'request_id': key, 'success': False, 'error': '账户读取失败',
            'data': {'email': client['account'], 'remaining_points': 1500}})
        self.assertEqual(client['credits'], 50, 'a failed probe must preserve the prior confirmed balance, never adopt 1500')
        self.assertTrue(client.get('identity_unconfirmed'))
        self.assertFalse(client.get('credits_waiting_ready'), 'a non-transient failure must not loop the initial probe')
        self.assertIsNone(choose_client([client], [], 4, self.jobs(duration=8)[0], {}, 12))

    def test_late_upload_ack_cannot_submit_or_debit_an_already_submitted_job(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client, 'uploading', {'cost': 12, 'cost_source': 'duration_rule'})
        reply = self.receipt(service, client, row, 'upload_images')
        service.store.update(row['id'], 'generating')
        before = service.store.get(row['id'])
        service._response(client, reply)
        self.assertEqual(service.store.get(row['id']), before)
        self.assertEqual(client['credits'], 50)
        self.assertFalse(any(p['action'] == 'run_tasks' for p in service.pending.values()))

    def test_late_run_ack_does_not_regress_download_recovery(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client, 'submitting')
        reply = self.receipt(service, client, row, 'run_tasks')
        remote = {**row['remote'], 'cloud_finished': True,
                  'transfer_retries': {'download': {'active': False, 'count': 1, 'next_at': time.time()+15}}}
        service.store.update(row['id'], 'download_failed', remote=remote, error='正在恢复原下载')
        before = service.store.get(row['id'])
        service._response(client, reply)
        self.assertEqual(service.store.get(row['id']), before)

    def test_restart_pre_submit_phases_return_to_paused_queue_only(self):
        service = self.service(); client = self.browser(service)
        for status in ('preparing', 'uploading'):
            row = self.job(service, client, status)
            service.store.recover()
            after = service.store.get(row['id'])
            self.assertEqual(after['status'], 'queued')
            self.assertFalse(after['attempt'])
        self.assertTrue(service.paused)
        self.assertFalse(service.pending)
        paid = self.job(service, client, 'uploading')
        service.store.reserve(paid['id'], paid['attempt'], 12, 'duration_rule')
        service.store.recover()
        self.assertEqual(service.store.get(paid['id'])['status'], 'uncertain')

    def test_retry_and_regenerate_cannot_reopen_active_or_archiving_work(self):
        service = self.service(); client = self.browser(service)
        for state in ('preparing', 'uploading', 'submitting', 'generating', 'downloading', 'archiving'):
            row = self.job(service, client, state)
            before = service.store.get(row['id'])
            service.store.retry([row['id']])
            self.assertEqual(service.store.get(row['id']), before)
            service.store.retry([row['id']], regenerate=True)
            self.assertEqual(service.store.get(row['id']), before)

    def test_stale_archive_result_does_not_complete_a_new_attempt(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client, 'generating')
        before = service.store.get(row['id'])
        service._archived({'id': row['id'], 'attempt': 'a'*32, 'output': 'old-result.mp4'})
        self.assertEqual(service.store.get(row['id']), before)

    def test_high_priority_task_is_not_overtaken_by_an_easier_duration_lane(self):
        service = self.service(); client = self.browser(service)
        service.preferences.update(auto_lane=True, profile_lanes={client['profile']: 10})
        service.store.enqueue(self.jobs(duration=8, budget=0))
        urgent = service.store.rows()[0]
        service.store.set_payload(urgent['id'], {**urgent['payload'], 'priority': 90})
        service.store.enqueue(self.jobs(duration=10, budget=0))
        service.paused = False; service._tick()
        self.assertEqual(service.store.get(urgent['id'])['status'], 'preparing')

    def test_same_account_windows_are_serialized_despite_case_difference(self):
        service = self.service(); one = self.browser(service); two = self.browser(service, 'Profile 2')
        two['account'] = one['account'].upper()
        self.job(service, one, 'generating')
        self.assertIsNone(choose_client([one, two], service.store.rows(), 4, self.jobs(duration=8)[0], {}, 12))

    def test_credit_probe_holds_other_windows_of_same_account_but_not_other_accounts(self):
        service = self.service(); one = self.browser(service); two = self.browser(service, 'Profile 2')
        two['account'] = one['account']
        other = self.browser(service, 'Profile 3')
        service.execute({'action':'credits','manual':True,'keys':['profile:'+one['profile']]})
        self.assertIsNone(choose_client([one, two], [], 4, self.jobs(duration=8)[0], {}, 12))
        self.assertEqual(choose_client([one, two, other], [], 4, self.jobs(duration=8)[0], {}, 12)['id'], other['id'])

    def test_busy_page_without_active_row_cannot_be_reloaded_or_repaired(self):
        from unittest.mock import MagicMock
        from app_plugins.builtin.flow_assistant.setup_pairing import check_reload, claim_setup
        service = self.service(); client = self.browser(service)
        client['batch_running'] = True
        service.setup_requests[client['profile']] = {'id': 'a'*32, 'expires': time.time()+60,
            'project': client['saved_project']}
        requester = {'socket': MagicMock()}
        self.assertFalse(check_reload(service, requester, {'ticket':'a'*32}))
        self.assertFalse(claim_setup(service, requester, {'ticket':'a'*32}))
        self.assertFalse(service.setup_requests[client['profile']].get('claimed'))

    def test_synchronous_old_socket_disconnect_does_not_fail_the_replacement_job(self):
        import json
        from unittest.mock import MagicMock
        service = self.service(); old = self.browser(service)
        row = self.job(service, old, 'generating')
        replacement = MagicMock()
        service.clients[replacement] = {'socket': replacement, 'authorized':False, 'connected':time.time()}
        old['socket'].close.side_effect = lambda: service._disconnected(old['socket'])
        service._message(replacement, json.dumps({'type':'hello','protocol':1,'token':service.pair['token'],
            'browser_id':old['id'],'profile':old['profile'],'project':old['saved_project'],
            'extension_version':old['extension_version']}))
        self.assertEqual(service.store.get(row['id'])['status'], 'generating')
        self.assertTrue(service.clients[replacement]['authorized'])

    def test_archive_submit_failure_releases_busy_state_and_keeps_download(self):
        from unittest.mock import MagicMock
        service = self.service(); client = self.browser(service)
        binding = {'schema':2,'source':'generation_response','taskId':'task','submissionId':'sub',
                   'mediaIds':['media'],'readyIds':['media'],'urls':{}}
        download = {'state':'complete','mediaId':'media','taskId':'task','submissionId':'sub'}
        row = self.job(service, client, 'archive_failed', {'bindings':[binding], 'upstream_task':'task', 'download':download})
        service.pool.shutdown(wait=True)
        service.pool = MagicMock(); service.pool.submit.side_effect = RuntimeError('executor closed')
        service.archive(row['id'])
        self.assertNotIn(row['id'], service.archiving)
        self.assertEqual(service.store.get(row['id'])['status'], 'archive_failed')
        self.assertEqual(service.store.get(row['id'])['remote']['download'], download)

    def test_failed_record_with_existing_charge_is_not_automatically_regenerated(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client, 'failed')
        service.store.reserve(row['id'], row['attempt'], 12, 'duration_rule')
        service.store.retry([row['id']])
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        self.assertEqual(service.store.get(row['id'])['attempt'], row['attempt'])

    def test_late_status_cannot_resurrect_a_cancelled_record(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client, 'cancelled')
        before = service.store.get(row['id'])
        service._status(client, {'batch_id': row['attempt'], 'status':'failed', 'message':'late error'})
        self.assertEqual(service.store.get(row['id']), before)

    def test_partial_media_id_snapshot_can_fill_in_but_cannot_replace_submission(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client, 'generating')
        def snapshot(ids, sub='sub'):
            service._snapshot(client, {'url':client['saved_project'], 'running':True,'batch_id':row['attempt'],
                'tasks':[{'taskId':'task','download_path':service.download_name(row), 'status':['generating'],
                    'mediaBindings':[{'schema':2,'source':'generation_response','taskId':'task','submissionId':sub,
                                      'mediaIds':ids,'readyIds':[],'urls':{}}]}]})
        snapshot([]); snapshot(['real-media'])
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'generating')
        self.assertEqual(current['remote']['bindings'][0]['mediaIds'], ['real-media'])
        snapshot([])
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'][0]['mediaIds'], ['real-media'])
        snapshot(['different-media'])
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'][0]['mediaIds'], ['real-media'])

    def test_empty_media_ids_do_not_authorize_a_different_submission(self):
        service = self.service(); client = self.browser(service)
        row = self.job(service, client, 'generating')
        binding = {'schema':2,'source':'generation_response','taskId':'task','submissionId':'original',
                   'mediaIds':[],'readyIds':[],'urls':{}}
        service.store.update(row['id'], 'generating', remote={**row['remote'], 'bindings':[binding], 'upstream_task':'task'})
        incoming = {**binding,'submissionId':'different','mediaIds':['not-ours']}
        service._snapshot(client, {'url':client['saved_project'],'running':True,'batch_id':row['attempt'],
            'tasks':[{'taskId':'task','download_path':service.download_name(row),'mediaBindings':[incoming]}]})
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        self.assertEqual(service.store.get(row['id'])['remote']['bindings'][0]['submissionId'], 'original')


class FlowCreditOrderTests(unittest.TestCase):
    def client(self, credits, **updates):
        return {'id': str(credits), 'profile': str(credits), 'account': str(credits)+'@example.test',
            'credits': credits, 'credits_at': time.time(), 'credits_local': True, 'page_ready': True, **updates}

    def test_credit_tiers_apply_even_with_unknown_cost_and_without_a_job(self):
        clients = [self.client(n) for n in (1500, 300, 70)]
        for job in (None, {'duration': None}):
            self.assertEqual(choose_client(clients, [], 4, job)['credits'], 70)
        self.assertEqual(choose_client(clients[:2], [], 4, {'duration': None})['credits'], 300)

    def test_small_funded_account_precedes_rich_matching_automatic_lane(self):
        small, rich = self.client(40), self.client(1500)
        prefs = {'auto_lane': True, 'profile_lanes': {'40': 10, '1500': 8}}
        self.assertEqual(choose_client([rich, small], [], 4, {'duration': 8}, prefs, 12)['id'], '40')

    def test_thresholds_affordability_fixed_lanes_and_busy_accounts(self):
        clients = [self.client(n) for n in (9, 99, 100, 999, 1000, 1500)]
        self.assertEqual(choose_client(clients, [], 4, {'duration': 8}, {}, 12)['credits'], 99)
        active = [{'status': 'generating', 'browser': '99', 'remote': {'account':'99@example.test'}}]
        self.assertEqual(choose_client(clients, active, 4, {'duration': 8}, {}, 12)['credits'], 100)
        prefs = {'auto_lane': False, 'allow_general': False,
                 'profile_lanes': {str(c['credits']): 8 if c['credits'] >= 1000 else 10 for c in clients}}
        self.assertEqual(choose_client(clients, [], 4, {'duration': 8}, prefs, 12)['credits'], 1000)
        self.assertIsNone(choose_client(clients, active, 1, {'duration': 8}, {}, 12))


if __name__ == '__main__': unittest.main()
