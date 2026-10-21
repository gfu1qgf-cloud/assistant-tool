"""Full physical capacity must not pin the queue behind a skipped rich account."""
import json
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.high_credit import allowed
from app_plugins.builtin.flow_assistant.browser_slots import seen, occupied_profiles, can_start
from app_plugins.builtin.flow_assistant.browser_slots import confirm_absence
from app_plugins.builtin.flow_assistant.browser_rotation import watch
from app_plugins.builtin.flow_assistant.policy import waiting_reason


class BackupRotationTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    observe = fixtures.CreditCooldownTests.observe

    def full(self):
        service = self.service()
        rich = self.browser(service, 'Rich', points=814)
        self.observe(service, rich, 814)
        rich['capabilities'] = ['close_exhausted_v1', 'close_idle_backup_v1', 'window_inventory_v1']
        entries = service.preferences['high_credit_accounts']
        entries[rich['account']].update(decision='skipped', decision_source='user_declined', asked_cycle=0)
        workers = [self.browser(service, 'Working ' + str(i), points=50) for i in range(3)]
        service.preferences.update(concurrency=4, profiles=['Rich', *[c['profile'] for c in workers], 'Backup'])
        service.preferences['profile_projects']['Backup'] = fixtures.PROJECT
        for client in [rich, *workers]: seen(service, client)
        service.store.enqueue(self.jobs(duration=10, count=8, budget=0))
        rows = service.store.rows()
        for client, row in zip(workers, rows):
            service.store.update(row['id'], 'generating', browser=client['id'],
                remote={'account': client['account'], 'profile': client['profile']})
        service.paused = False
        return service, rich, service.store.rows()

    def test_full_four_windows_close_skipped_backup_then_open_small_without_touching_tasks(self):
        service, rich, before = self.full()
        launches = []; service.launch_requested.connect(launches.append)
        self.assertFalse(can_start(service, 'Backup'))
        service._tick()
        self.assertFalse(launches)
        request = next((key, p) for key, p in service.pending.items() if p['action'] == 'close_exhausted')
        self.assertEqual(request[1]['client'], rich['id'])
        sent = json.loads(rich['socket'].sendTextMessage.call_args.args[0])
        self.assertEqual(sent['params']['reason'], 'high_credit_backup')
        self.assertNotIn('balance', sent['params'])  # Never fabricate a low balance.
        self.assertEqual(service.store.rows(), before)
        self.assertFalse(allowed(rich, service.preferences))
        service._response(rich, {'request_id': request[0], 'success': True, 'data': {'closing': True}})
        service._tick(); self.assertFalse(launches)
        service._response(rich, {'request_id': request[0], 'success': True, 'data': {'closed': True}})
        service._tick()
        self.assertEqual(launches, ['Backup'])
        self.assertEqual(len(occupied_profiles(service)), 4)
        self.assertEqual(service.store.rows(), before)
        self.assertEqual(service.preferences['profile_lanes']['Backup'], 10)

    def test_existing_paid_or_unconfirmed_rich_work_must_finish_before_rotation(self):
        service, rich, rows = self.full()
        row = rows[-1]
        for status in ('preparing', 'uploading', 'submitting', 'generating', 'downloading',
                       'uncertain', 'download_failed', 'archiving', 'archive_failed', 'download_parked'):
            service.store.update(row['id'], status, browser=rich['id'], remote={'account': rich['account']})
            self.assertFalse(watch(service, service.store.rows()[:1]), status)
            self.assertFalse(service.pending)
        service.store.update(row['id'], 'failed', attempt='a' * 32)
        service.store.reserve(row['id'], 'a' * 32, 12, 'duration_rule')
        self.assertFalse(watch(service, service.store.rows()[:1]))

    def test_busy_close_or_failure_keeps_the_slot_and_does_not_open_a_fifth_window(self):
        service, rich, _ = self.full()
        launches = []; service.launch_requested.connect(launches.append)
        service._tick()
        key = next(k for k, p in service.pending.items() if p['action'] == 'close_exhausted')
        service._response(rich, {'request_id': key, 'success': False, 'code': 'FLOW_CLOSE_BUSY', 'error': 'download pending'})
        service._tick()
        self.assertEqual(len(occupied_profiles(service)), 4)
        self.assertFalse(launches)
        self.assertEqual(rich['credit_close']['count'], 0)

    def test_no_alternative_or_no_admitted_queue_does_not_close_a_pool(self):
        service, rich, rows = self.full()
        self.assertFalse(watch(service, []))
        service.preferences['profiles'].remove('Backup')
        self.assertFalse(watch(service, [r for r in rows if r['status'] == 'queued']))
        self.assertFalse(service.pending)

    def test_manual_credit_probe_cannot_clear_an_inflight_close_lease(self):
        service, rich, _ = self.full(); service._tick()
        self.assertFalse(service.request(rich, 'get_points', manual_credit=True))
        self.assertEqual(rich['credit_close']['state'], 'checking')
        self.assertFalse(any(p['action'] == 'get_points' for p in service.pending.values()))

    def test_native_absence_report_releases_only_that_profile_and_preserves_all_jobs(self):
        service, rich, rows = self.full()
        service._message(rich['socket'], json.dumps({'type': 'windows_closed', 'profile': rich['profile'],
                                                     'project': rich['saved_project']}))
        self.assertNotIn('Rich', occupied_profiles(service))
        self.assertTrue(can_start(service, 'Backup'))
        self.assertEqual(service.store.rows(), rows)

    def test_row_waiting_reason_does_not_copy_the_skipped_accounts_error_to_every_job(self):
        service, rich, rows = self.full()
        queued = next(row for row in rows if row['status'] == 'queued')
        reason = waiting_reason(queued, [rich], rows, service.preferences, {}, service.store.batches())
        self.assertIn('排队', reason)
        self.assertNotIn('本轮已跳过', reason)

    def test_restart_absence_proof_clears_stale_backup_reservation_without_opening_it(self):
        service, rich, rows = self.full()
        service.clients.pop(rich['socket'])
        message = {'protocol': 1, 'token': service.pair['token'], 'browser_id': rich['id'],
                   'profile': rich['profile'], 'project': rich['saved_project']}
        for field in ('token', 'browser_id', 'project'):
            self.assertFalse(confirm_absence(service, {**message, field: 'wrong'}))
        self.assertIn('Rich', occupied_profiles(service))
        self.assertTrue(confirm_absence(service, message))
        self.assertNotIn('Rich', occupied_profiles(service))
        self.assertEqual(service.store.rows(), rows)

    def test_paid_result_keeps_its_ownership_even_when_native_window_absence_is_proven(self):
        service, rich, rows = self.full()
        row = rows[-1]
        service.store.update(row['id'], 'generating', browser=rich['id'],
            remote={'profile': rich['profile'], 'account': rich['account']})
        service.clients.pop(rich['socket'])
        self.assertTrue(confirm_absence(service, {'protocol': 1, 'token': service.pair['token'],
            'browser_id': rich['id'], 'profile': rich['profile'], 'project': rich['saved_project']}))
        self.assertIn('Rich', occupied_profiles(service))
        self.assertEqual(service.store.get(row['id'])['status'], 'generating')


if __name__ == '__main__': unittest.main()
