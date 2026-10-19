"""Local setup/update and one-shot page recovery, without real browser work."""
import json
import time
import unittest
from unittest.mock import MagicMock
from contextlib import closing

import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.setup_pairing import check_reload, claim_setup, lookup_setup
from app_plugins.builtin.flow_assistant.page_recovery import record_response, schedule, watch


class PairingRecoveryTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client

    def test_project_entry_has_one_time_ticket_not_the_long_term_key(self):
        service = self.service(); replies = []; service.setup_ready.connect(replies.append)
        project = 'https://flow.google.com/project/my-project'
        service.execute({'action': 'setup_profile', 'profile': 'Profile 23', 'project': project})
        reply = replies[-1]
        self.assertTrue(reply['project_claim_url'].startswith(project+'#lzx_setup='))
        self.assertNotIn(service.pair['token'], reply['project_claim_url'])
        self.assertFalse(service.pending)

    def test_update_requires_recent_ticket_and_rechecks_busy_state(self):
        service = self.service(); client = {'socket': MagicMock(), 'authorized': False}
        service.execute({'action': 'setup_profile', 'profile': 'Profile 23', 'project': 'https://flow.google.com/project/p'})
        ticket = service.setup_requests['Profile 23']['id']
        self.assertTrue(check_reload(service, client, {'ticket': ticket}))
        self.assertFalse(service.setup_requests['Profile 23'].get('claimed', False), 'update check must not consume the following pair claim')
        self.assertFalse(check_reload(service, client, {'ticket': '0'*32}))
        service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', remote={'profile': 'Profile 23'})
        self.assertFalse(check_reload(service, client, {'ticket': ticket}))
        self.assertEqual(service.store.get(row['id'])['status'], 'generating')
        service.store.update(row['id'], 'completed')
        self.assertTrue(claim_setup(service, client, {'ticket': ticket}))
        self.assertFalse(check_reload(service, client, {'ticket': ticket}))

    def test_expired_update_request_cannot_reload_a_browser(self):
        service = self.service(); client = {'socket': MagicMock(), 'authorized': False}
        service.setup_requests['Profile 23'] = {'id': 'a'*32, 'expires': time.time()-1, 'project': 'https://flow.google.com/project/p'}
        self.assertFalse(check_reload(service, client, {'ticket': 'a'*32}))

    def test_unconfirmed_generation_cannot_reload_even_for_extension_update(self):
        service, row, client = self.row()
        client.update(extension_version='1.1.4', saved_project='https://flow.google.com/project/p', setup_pending=False)
        service.store.update(row['id'], 'uncertain', remote={'profile': client['profile'], 'bindings': [{'old': 'preserved'}]})
        before = service.store.get(row['id']); replies = []; service.setup_ready.connect(replies.append)
        service.execute({'action': 'setup_profile', 'profile': client['profile'], 'project': client['saved_project']})
        self.assertFalse(replies[-1]['success']); self.assertFalse(client['setup_pending'])
        self.assertFalse(service.setup_requests)
        self.assertEqual(service.store.get(row['id']), before)

    def test_cloud_finished_download_can_update_without_repairing_its_binding(self):
        service, row, client = self.row()
        client.update(extension_version='1.1.4', saved_project='https://flow.google.com/project/p', setup_pending=False)
        service.store.update(row['id'], 'download_failed', remote={'profile': client['profile'], 'cloud_finished': True,
            'bindings': [{'old': 'preserved'}]})
        before = service.store.get(row['id']); replies = []; service.setup_ready.connect(replies.append)
        service.execute({'action': 'setup_profile', 'profile': client['profile'], 'project': client['saved_project']})
        self.assertTrue(replies[-1]['success'])
        ticket = service.setup_requests[client['profile']]['id']
        requester = {'socket': MagicMock(), 'authorized': False}
        self.assertTrue(check_reload(service, requester, {'ticket': ticket}))
        self.assertFalse(claim_setup(service, requester, {'ticket': ticket}), 'code-only update must not change credentials')
        self.assertEqual(service.store.get(row['id']), before)

    def test_bare_options_can_recover_only_its_own_pending_entry(self):
        service = self.service(); client = {'socket': MagicMock(), 'authorized': False}
        service.setup_requests['Profile 23'] = {'id': 'a'*32, 'expires': time.time()+60, 'project': 'https://flow.google.com/project/p'}
        self.assertFalse(lookup_setup(service, client, {'profile': 'Profile 23', 'token': 'wrong'}))
        self.assertFalse(lookup_setup(service, client, {'profile': 'Profile 24', 'token': service.pair['token']}))
        self.assertTrue(lookup_setup(service, client, {'profile': 'Profile 23', 'token': service.pair['token']}))
        reply = json.loads(client['socket'].sendTextMessage.call_args.args[0])
        self.assertEqual(reply['ticket'], 'a'*32); self.assertNotIn('token', reply)

    def row(self):
        service = self.service(); service.store.enqueue(self.jobs()); client = self.client(service)
        row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', attempt='a'*32, browser=client['id'], remote={'profile': client['profile']})
        return service, service.store.get(row['id']), client

    def test_missing_binding_only_reads_snapshots_never_refreshes_or_submits(self):
        service, row, client = self.row()
        schedule(service, client, {'batch_id': row['attempt']})
        current = service.store.get(row['id']); plan = current['remote']['page_recovery']
        service.store.update(row['id'], 'uncertain', remote={**current['remote'], 'page_recovery': {**plan, 'next_at': 0}})
        self.assertTrue(watch(service)); self.assertFalse(watch(service))
        request = next(iter(service.pending.values()))
        self.assertEqual(request['action'], 'snapshot')
        for _ in range(3):
            current = service.store.get(row['id'])
            service.store.update(row['id'], 'uncertain', remote={**current['remote'], 'page_recovery': {**current['remote']['page_recovery'], 'next_at': 0}})
            self.assertTrue(watch(service)); self.assertFalse(watch(service))
        self.assertFalse(any(r['action'] == 'refresh_project' for r in service.pending.values()))
        self.assertFalse(any(r['action'] == 'run_tasks' for r in service.pending.values()))
        self.assertTrue(service.store.get(row['id'])['remote']['page_recovery']['done'])

    def test_another_active_batch_is_never_refreshed(self):
        service, row, client = self.row(); schedule(service, client, {'batch_id': row['attempt']})
        current = service.store.get(row['id'])
        service.store.update(row['id'], 'uncertain', remote={**current['remote'], 'page_recovery': {'count': 0, 'next_at': 0}})
        client['active_batch'] = 'b'*32
        self.assertFalse(watch(service)); self.assertFalse(service.pending)

    def test_legacy_refresh_request_is_blocked_before_sending(self):
        service, row, client = self.row()
        service.store.update(row['id'], 'uncertain', remote={'page_recovery': {'count': 1}})
        service.request(client, 'refresh_project', {'batch_id': row['attempt']}, row['id'])
        current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'uncertain')
        self.assertFalse(service.pending)
        self.assertFalse(any(r['action'] == 'run_tasks' for r in service.pending.values()))

    def test_response_evidence_is_durable_but_not_media_ownership(self):
        service, row, client = self.row()
        message = {'batch_id': row['attempt'], 'download_path': service.download_name(row), 'taskId': 'task',
            'submissionId': 'submission', 'response': {'unknown_output': ['keep this response for diagnosis']}}
        record_response(service, client, message)
        schedule(service, client, {'batch_id': row['attempt']})
        current = service.store.get(row['id']); evidence = current['remote']['generation_response']
        self.assertNotIn('response', evidence); self.assertEqual(len(evidence['sha256']), 64)
        with closing(service.namespace.file('generation-diagnostics.sqlite3').connect(read_only=True)) as db:
            saved = json.loads(db.execute('SELECT evidence FROM responses WHERE attempt=?', (row['attempt'],)).fetchone()[0])
        self.assertEqual(saved['response'], message['response'])
        self.assertNotIn('bindings', current['remote']); self.assertFalse(service.pending)
        record_response(service, client, {**message, 'response': {'replacement': True}})
        self.assertEqual(service.store.get(row['id'])['remote']['generation_response'], evidence)


if __name__ == '__main__': unittest.main()
