"""Credit exhaustion survives restart; only verified idle browsers may close."""
import json
import time
import unittest
from flow_reference_fixtures import upload_receipt, distinct_jobs
from unittest.mock import MagicMock, patch

import test_flow_assistant as fixtures
import test_flow_setup as setup_fixtures
from app_plugins.builtin.flow_assistant.credit_cooldown import (
    COOLDOWN_SECONDS, credit_hold, credit_reason, remember_balance, close_idle_exhausted)
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.connection_recovery import repair_idle
from app_plugins.builtin.flow_assistant.page_readiness import retry_initial_credits, watch_preparation
from app_plugins.builtin.flow_assistant.policy import block_for, capable, waiting_reason
from app_plugins.builtin.flow_assistant.store import QueueStore
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION

PROJECT = 'https://flow.google.com/project/credit-fixture'


class CreditCooldownTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client
    ui = setup_fixtures.SetupTests.ui

    def browser(self, service, profile='Profile 23', points=50, close=False):
        client = self.client(service, profile, points)
        client.update(account=profile.replace(' ', '').casefold() + '@example.test',
            credits_local=True, connected=time.time(), page_at=time.time(), page_ready=True,
            project=PROJECT, saved_project=PROJECT, batch_running=False,
            capabilities=['close_exhausted_v1'] if close else [])
        service.preferences.setdefault('profile_projects', {})[profile] = PROJECT
        return client

    def observe(self, service, client, points, *, manual=False, email=None):
        service.request(client, 'get_points', manual_credit=manual)
        key = next(k for k, p in service.pending.items() if p['client'] == client['id'] and p['action'] == 'get_points')
        service._response(client, {'request_id': key, 'success': True,
            'data': {'email': email or client['account'], 'remaining_points': points}})

    def low(self, service, client, points=9):
        self.observe(service, client, points)
        return credit_hold(client, service.preferences)

    def test_below_ten_includes_positive_balance_but_ten_remains_usable(self):
        service = self.service()
        for points in (0, 1, 5, 9, 10, 11):
            with self.subTest(points=points):
                client = self.browser(service, 'Profile ' + str(points))
                self.observe(service, client, points)
                self.assertEqual(bool(credit_hold(client, service.preferences)), points < 10)
                self.assertEqual(capable(client, self.jobs(duration=6)[0], service.preferences, 10), points >= 10)
        self.assertEqual(COOLDOWN_SECONDS, 21600)

    def test_six_hour_boundary_and_durable_profile_and_account_links(self):
        service = self.service(); client = self.browser(service)
        hold = self.low(service, client)
        saved = QueueStore(self.namespace).settings()
        self.assertEqual(hold['until'] - hold['at'], 21600)
        self.assertTrue(credit_hold({'profile': client['profile']}, saved, hold['until'] - 1))
        self.assertFalse(credit_hold({'profile': client['profile']}, saved, hold['until']))
        self.assertTrue(credit_hold({'account': client['account'].upper()}, saved))

    def test_restart_skips_low_offline_profile_and_opens_backup(self):
        service = self.service(); low = self.browser(service)
        self.low(service, low)
        service.commit_preferences({**service.preferences, 'profiles': [low['profile'], 'Backup'],
            'profile_projects': {low['profile']: PROJECT, 'Backup': PROJECT}, 'concurrency': 1})
        service.execute({'action': 'stop'})
        restarted = self.service()
        restarted.store.enqueue(self.jobs(duration=6, budget=0)); restarted.paused = False
        launches = []; restarted.launch_requested.connect(launches.append)
        restarted._tick()
        self.assertEqual(launches, ['Backup'])
        self.assertTrue(block_for({'profile': low['profile']}, restarted.preferences))

    def test_reconnect_does_not_probe_credits_or_repair_low_profile(self):
        service = self.service(); old = self.browser(service)
        self.low(service, old)
        socket = MagicMock()
        service.clients[socket] = {'socket': socket, 'authorized': False, 'connected': time.time()}
        service._message(socket, json.dumps({'type': 'hello', 'protocol': 1, 'token': 'x' * 40,
            'browser_id': old['id'], 'profile': old['profile'], 'project': PROJECT,
            'extension_version': EXTENSION_VERSION}))
        client = service.clients[socket]
        self.assertFalse(any(p['action'] == 'get_points' for p in service.pending.values()))
        self.assertFalse(repair_idle(service, client))
        self.assertEqual(client['credit_check']['state'], 'cooldown')

    def test_positive_manual_read_releases_only_selected_account_and_keeps_bans(self):
        service = self.service(); one = self.browser(service); two = self.browser(service, 'Other')
        self.low(service, one); self.low(service, two)
        service.preferences['account_blocks'] = {'banned@example.test': {'kind': 'disabled', 'reason': 'disabled'}}
        self.observe(service, one, 10, manual=True)
        self.assertFalse(credit_hold(one, service.preferences))
        self.assertTrue(credit_hold(two, service.preferences))
        self.assertIn('banned@example.test', service.preferences['account_blocks'])
        self.assertTrue(service.paused)
        self.assertFalse(service.released)

    def test_failed_manual_probe_with_partial_positive_data_does_not_release_hold(self):
        service = self.service(); client = self.browser(service); hold = self.low(service, client)
        service.request(client, 'get_points', manual_credit=True)
        key = next(iter(service.pending))
        service._response(client, {'request_id': key, 'success': False, 'error': 'temporary error',
            'data': {'email': client['account'], 'remaining_points': 1000}})
        self.assertEqual(credit_hold(client, service.preferences), hold)
        self.assertEqual(client['credit_check']['state'], 'failed')

    def test_manual_read_still_below_ten_renews_hold_and_reports_detection_success(self):
        service = self.service(); client = self.browser(service); hold = self.low(service, client)
        with patch('time.time', return_value=hold['at'] + 300):
            self.observe(service, client, 5, manual=True)
        self.assertEqual(credit_hold(client, service.preferences)['until'], hold['until'] + 300)
        self.assertEqual(client['credit_check']['state'], 'success')

    def test_new_account_does_not_inherit_old_login_cooldown(self):
        service = self.service(); client = self.browser(service); old_account = client['account']
        self.low(service, client)
        self.observe(service, client, 50, manual=True, email='new@example.test')
        self.assertFalse(credit_hold(client, service.preferences))
        self.assertFalse(credit_hold({'profile': client['profile']}, service.preferences))
        self.assertTrue(credit_hold({'account': old_account}, service.preferences))

    def test_automatic_late_positive_cannot_undo_exhaustion_or_extend_deadline(self):
        service = self.service(); client = self.browser(service); duplicate = self.browser(service, 'Duplicate')
        duplicate['account'] = client['account']
        service.request(duplicate, 'get_points'); key = next(iter(service.pending))
        hold = self.low(service, client, 5)
        service._response(duplicate, {'request_id': key, 'success': True,
            'data': {'email': client['account'], 'remaining_points': 1000}})
        self.assertEqual(credit_hold(client, service.preferences)['until'], hold['until'])
        self.assertEqual(client['credits'], 5)
        self.assertTrue(block_for(duplicate, service.preferences))

    def test_click_during_automatic_probe_waits_for_new_manual_observation(self):
        service = self.service(); client = self.browser(service)
        service.request(client, 'get_points'); old = next(iter(service.pending))
        client['credits'] = 9; remember_balance(service, client)
        service.execute({'action': 'credits', 'manual': True, 'keys': ['profile:' + client['profile']]})
        service._response(client, {'request_id': old, 'success': True,
            'data': {'email': client['account'], 'remaining_points': 1000}})
        self.assertTrue(credit_hold(client, service.preferences))
        fresh = next(k for k, p in service.pending.items() if p['action'] == 'get_points')
        self.assertNotEqual(old, fresh)
        self.assertTrue(service.pending[fresh]['manual_credit'])
        service._response(client, {'request_id': fresh, 'success': True,
            'data': {'email': client['account'], 'remaining_points': 50}})
        self.assertFalse(credit_hold(client, service.preferences))

    def test_debit_below_ten_is_persisted_before_last_generation_is_sent(self):
        service = self.service(); client = self.browser(service, points=19)
        service.store.enqueue(self.jobs(duration=6, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'uploading', attempt='a' * 32, browser=client['id'],
            remote={'account': client['account'], 'profile': client['profile'], 'cost': 10, 'cost_source': 'duration_rule'})
        service.request(client, 'upload_images', job=row['id']); key = next(iter(service.pending))
        observations = []
        client['socket'].sendTextMessage.side_effect = lambda text: observations.append((
            json.loads(text)['action'], credit_hold(client, service.store.settings()).get('balance')))
        service._response(client, {'request_id': key, 'success': True, 'data': upload_receipt(service, row['id'])})
        self.assertEqual(observations, [('run_tasks', 9)])
        self.assertEqual(service.store.get(row['id'])['status'], 'submitting')
        service._response(client, {'request_id': next(iter(service.pending)), 'success': True})
        published = {**client, 'environment_block': block_for(client, service.preferences)}
        self.assertEqual(browser_status(published, service.preferences, service.store.rows())['state'], '执行中')

    def test_expired_online_hold_gets_one_fresh_read_when_queue_runs(self):
        service = self.service(); client = self.browser(service); self.low(service, client)
        service.preferences['credit_cooldowns'][client['account']]['until'] = time.time() - 1
        service.store.enqueue(self.jobs(duration=6, budget=0)); service.paused = False
        service._tick(); service._tick()
        pending = [(key, p) for key, p in service.pending.items() if p['action'] == 'get_points']
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0][1]['manual_credit'])
        self.assertEqual(service.store.rows()[0]['status'], 'queued')
        service._response(client, {'request_id': pending[0][0], 'success': True,
            'data': {'email': client['account'], 'remaining_points': 50}})
        service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')

    def test_expired_offline_profile_can_launch_once_again(self):
        service = self.service(); client = self.browser(service); self.low(service, client)
        service.preferences.update(profiles=[client['profile']], concurrency=1)
        service.launched.add(client['profile'])
        remember_balance(service, client)
        service.preferences['credit_cooldowns'][client['account']]['until'] = time.time() - 1
        service.clients.clear()
        service.store.enqueue(self.jobs(duration=6, budget=0)); service.paused = False
        launches = []; service.launch_requested.connect(launches.append)
        service._tick(); service._tick()
        self.assertEqual(launches, [client['profile']])

    def test_initial_retry_and_preparation_reopen_cannot_bypass_active_hold(self):
        service = self.service(); client = self.browser(service); self.low(service, client)
        client.update(credits_waiting_ready=True, credits_ready_failures=0)
        self.assertFalse(retry_initial_credits(service, client))
        service.store.enqueue(self.jobs(duration=6, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'queued', remote={'prepare_retry': {
            'reopen': True, 'profile': client['profile'], 'next_at': 0}})
        service.clients.clear()
        launches = []; service.launch_requested.connect(launches.append)
        self.assertFalse(watch_preparation(service))
        self.assertFalse(launches)

    def test_four_funded_accounts_still_prepare_in_parallel_beside_exhausted_account(self):
        service = self.service(); low = self.browser(service); self.low(service, low)
        for index in range(4): self.browser(service, 'Funded' + str(index), points=20 + index)
        service.preferences['concurrency'] = 4
        service.store.enqueue(distinct_jobs(self, 4, duration=6, budget=0)); service.paused = False
        for _ in range(4): service._tick()
        rows = service.store.rows()
        self.assertEqual(len({row['browser'] for row in rows if row['status'] == 'preparing'}), 4)
        self.assertNotIn(low['id'], {row['browser'] for row in rows})

    def test_normal_positive_session_balance_does_not_expire_or_poll(self):
        service = self.service(); client = self.browser(service)
        client['credits_at'] = time.time() - 4 * 3600
        service.store.enqueue(self.jobs(duration=6, budget=0)); service.paused = False; service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')
        self.assertFalse(any(p['action'] == 'get_points' for p in service.pending.values()))

    def test_below_ten_cannot_bypass_hold_with_a_zero_cost_prepare_reply(self):
        service = self.service(); client = self.browser(service); self.low(service, client)
        service.store.enqueue(self.jobs(duration=None, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'preparing', browser=client['id'], attempt='a' * 32,
            remote={'account': client['account']})
        service.request(client, 'prepare', {}, row['id'])
        key = next(k for k, p in service.pending.items() if p['action'] == 'prepare')
        with patch.object(service, 'start_upload') as upload:
            service._response(client, {'request_id': key, 'success': True, 'data': {
                'verified': True, 'model': 'omni_1_1_flash', 'cost': 0, 'email': client['account']}})
            upload.assert_not_called()
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        self.assertTrue(credit_hold(client, service.preferences))

    def test_manual_replenishment_resets_old_close_failures_for_same_account_windows(self):
        service = self.service(); one = self.browser(service); two = self.browser(service, 'Duplicate')
        two['account'] = one['account']; self.low(service, one)
        one['credit_close'] = two['credit_close'] = {'count': 3, 'state': 'failed'}
        remember_balance(service, one)
        self.assertEqual(two['credit_close']['count'], 3, 'automatic repeated balances must not reset retry limits')
        self.observe(service, one, 50, manual=True)
        self.assertNotIn('credit_close', one); self.assertNotIn('credit_close', two)
        self.assertFalse(credit_hold(one, service.preferences))

    def test_close_waits_for_all_unresolved_result_and_archive_states(self):
        service = self.service(); client = self.browser(service, close=True); self.low(service, client)
        client['credit_check']['at'] -= 31
        service.store.enqueue(self.jobs(duration=6, budget=0)); row = service.store.rows()[0]
        for status in ('preparing', 'uploading', 'submitting', 'generating', 'downloading',
                       'uncertain', 'download_failed', 'archiving', 'archive_failed', 'download_parked'):
            with self.subTest(status=status):
                service.store.update(row['id'], status, browser=client['id'],
                    remote={'account': client['account'], 'profile': client['profile']})
                self.assertFalse(close_idle_exhausted(service))
                self.assertFalse(service.pending)
        service.store.update(row['id'], 'completed')
        self.assertFalse(close_idle_exhausted(service))
        self.assertFalse(service.pending)

    def test_close_waits_for_other_window_of_same_account(self):
        service = self.service(); client = self.browser(service, close=True); self.low(service, client)
        client['credit_check']['at'] -= 31
        service.store.enqueue(self.jobs(duration=6, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', browser='other-window', remote={'account': client['account']})
        self.assertFalse(close_idle_exhausted(service)); self.assertFalse(service.pending)

    def test_failed_row_with_reserved_charge_does_not_allow_close(self):
        service = self.service(); client = self.browser(service, close=True); self.low(service, client)
        client['credit_check']['at'] -= 31
        service.store.enqueue(self.jobs(duration=6, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'failed', browser=client['id'], attempt='a' * 32,
            remote={'account': client['account']})
        service.store.reserve(row['id'], 'a' * 32, 10, 'duration_rule')
        self.assertFalse(close_idle_exhausted(service)); self.assertFalse(service.pending)

    def test_close_is_disabled_regardless_of_extension_capability_and_page_busy_state(self):
        service = self.service(); client = self.browser(service); self.low(service, client)
        client['credit_check']['at'] -= 31
        for capabilities in ([], ['close_exhausted_v1']):
            for busy in (False, True):
                client.update(capabilities=capabilities, batch_running=busy)
                self.assertFalse(close_idle_exhausted(service))
                self.assertFalse(service.pending)

    def test_manual_detection_is_not_interrupted_by_auto_close(self):
        service = self.service(); client = self.browser(service, close=True); self.low(service, client)
        client['credit_check']['at'] -= 31
        service.request(client, 'get_points', manual_credit=True)
        self.assertFalse(close_idle_exhausted(service))
        self.assertFalse(any(p['action'] == 'close_exhausted' for p in service.pending.values()))

    def test_offline_browser_shows_stored_balance_and_six_hour_deadline(self):
        service = self.service(); client = self.browser(service); self.low(service, client)
        dialog, _, _ = self.ui()
        dialog.state = {'clients': [], 'preferences': service.preferences}
        dialog.update_browsers()
        self.assertIn('9（冷却记录）', dialog.browsers.item(0, 3).text())
        self.assertEqual(dialog.browsers.item(0, 7).text(), '积分冷却')
        self.assertIn('6 小时', dialog.browsers.item(0, 8).text())
        service.store.enqueue(self.jobs(duration=6, budget=0)); row = service.store.rows()[0]
        service.preferences['profiles'] = [client['profile']]
        self.assertIn('积分冷却', waiting_reason(row, [], [row], service.preferences, {}, {}))


if __name__ == '__main__': unittest.main()
