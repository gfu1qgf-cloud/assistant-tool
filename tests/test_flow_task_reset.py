"""Orphan uploads can move accounts; retirement never replays a paid attempt."""
import copy
import json
import time
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets
import test_flow_assistant as fixtures
import test_flow_credit_cooldown as credits
import test_flow_setup as setups
from app_plugins.builtin.flow_assistant.connection_recovery import occupied, repair_idle
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.store import QueueStore, choose_client


class TaskResetTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client
    browser = credits.CreditCooldownTests.browser
    ui = setups.SetupTests.ui

    def actions(self, client):
        return [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list]

    def orphan(self, service, *, status='uploading', count=0):
        service.paused = False
        service.store.enqueue(self.jobs(duration=8, budget=0))
        row = service.store.rows()[-1]
        service.store.update(row['id'], status, attempt='a' * 32, browser='old-browser', remote={
            'account': 'old@example.test', 'profile': 'Profile 23', 'cost': 10,
            'upload_recovery': {'count': count}, 'connection_wait_at': time.time() - 80,
            'transfer_retries': {'upload': {'attempt': 'a' * 32, 'next_at': time.time() - 1,
                'waiting_since': time.time() - 80, 'active': False, 'count': 0}}})
        return service.store.get(row['id'])

    def command(self, row, **values):
        return {'action': 'reset_tasks', 'request_id': 'reset-click', 'ids': [row['id']],
            'versions': {row['id']: {key: row[key] for key in ('attempt', 'browser', 'status')}}, **values}

    def test_manual_upload_reset_frees_original_browser_and_next_tick_uses_other(self):
        service = self.service(); row = self.orphan(service); other = self.browser(service, 'Profile 24')
        service.paused = True
        responses = []; service.tasks_reset.connect(responses.append)
        service.execute(self.command(row))
        reset = service.store.get(row['id'])
        self.assertEqual((reset['status'], reset['attempt'], reset['browser'], reset['remote']), ('queued', '', '', {}))
        self.assertEqual(reset['payload'], row['payload']); self.assertIn(row['id'], service.released)
        self.assertTrue(responses[-1]['success']); self.assertEqual(responses[-1]['reset'], [row['id']])
        service._tick(); current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'preparing'); self.assertEqual(current['browser'], other['id'])
        self.assertNotEqual(current['attempt'], row['attempt'])
        self.assertNotIn('run_tasks', [value['action'] for value in self.actions(other)])

    def test_late_upload_ack_cannot_submit_reset_job_or_corrupt_new_attempt(self):
        service = self.service(); row = self.orphan(service)
        original = self.browser(service, 'Profile 23'); original['id'] = row['browser']
        other = self.browser(service, 'Profile 24')
        service.request(original, 'upload_images', job=row['id'])
        old_request = next(key for key, item in service.pending.items() if item['action'] == 'upload_images')
        service.execute(self.command(row)); service._tick()
        before = service.store.get(row['id']); self.assertEqual(before['browser'], other['id'])
        self.assertGreater(original['reset_wait_until'], time.time())
        service._response(original, {'request_id': old_request, 'success': True})
        self.assertEqual(before, service.store.get(row['id']))
        self.assertFalse(service.store.attempt_charged(row['attempt']))
        self.assertNotIn('run_tasks', [value['action'] for value in self.actions(original)])
        self.assertFalse(service.store.reset_holds())

    def test_reset_preserves_charges_history_files_and_allocates_new_output(self):
        service = self.service(); row = self.orphan(service)
        service.store.reserve(row['id'], row['attempt'], 10, 'duration_rule')
        service.store.update(row['id'], 'generating', remote={**row['remote'], 'generation_response': {'media': 'owned'}})
        row = service.store.get(row['id']); output = self.target / 'Flow_001_chosen.mp4'; output.write_bytes(b'original result')
        before = service.store.batches()
        service.execute(self.command(row)); self.assertEqual(service.store.get(row['id'])['status'], 'generating')
        service.execute(self.command(row, confirmed_submitted=True))
        reset = service.store.get(row['id']); self.assertEqual(reset['status'], 'queued')
        self.assertNotEqual(reset['payload']['output'], row['payload']['output'])
        self.assertEqual(output.read_bytes(), b'original result'); self.assertEqual(before, service.store.batches())
        with service.store.connection() as db:
            previous = json.loads(db.execute('SELECT message FROM history WHERE job=? AND status="reset_previous"', (row['id'],)).fetchone()[0])
        self.assertEqual(previous['previous'], row)
        original_socket = fixtures.MagicMock()
        service._download({'id': row['browser'], 'socket': original_socket},
            {'batch_id': row['attempt'], 'event_id': 'late-download', 'data': {'state': 'complete'}})
        self.assertEqual(service.store.get(row['id']), reset)
        self.assertEqual(json.loads(original_socket.sendTextMessage.call_args.args[0])['type'], 'ack')

    def test_state_change_during_confirmation_requires_a_fresh_click(self):
        service = self.service(); row = self.orphan(service); command = self.command(row)
        service.store.reserve(row['id'], row['attempt'], 10, 'duration_rule')
        service.store.update(row['id'], 'submitting')
        responses = []; service.tasks_reset.connect(responses.append)
        service.execute(command)
        self.assertEqual(service.store.get(row['id'])['status'], 'submitting')
        self.assertFalse(responses[-1]['reset']); self.assertIn('变化', responses[-1]['skipped'][0]['reason'])

    def test_charge_guard_is_checked_even_if_status_and_browser_have_not_changed(self):
        service = self.service(); row = self.orphan(service)
        service.store.reserve(row['id'], row['attempt'], 10, 'duration_rule')
        service.execute(self.command(row))
        self.assertEqual(service.store.get(row['id']), row)

    def test_archiving_worker_and_safety_pause_are_not_bypassed(self):
        service = self.service(); row = self.orphan(service); service.archiving.add(row['id'])
        service.execute(self.command(row, confirmed_submitted=True))
        self.assertEqual(service.store.get(row['id']), row)
        service.archiving.clear(); service.safety_paused = True; service.paused = True
        service.execute(self.command(row)); self.assertNotIn(row['id'], service.released)
        self.browser(service, 'Profile 24'); service._tick()
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')

    def test_persistent_old_operation_hold_survives_service_restart_and_new_browser_id(self):
        service = self.service(); row = self.orphan(service); service.execute(self.command(row))
        reopened = QueueStore(self.namespace); self.assertTrue(reopened.reset_holds())
        replacement = self.browser(service, 'Profile 23'); replacement['id'] = 'replacement-browser'
        replacement['account'] = row['remote']['account']; service.refresh_reset_holds()
        self.assertGreater(replacement['reset_wait_until'], time.time())
        self.assertIsNone(choose_client([replacement], [], 4, row['payload'], service.preferences, 10))
        self.assertEqual(browser_status(replacement, service.preferences)['state'], '等待旧操作结束')
        with service.store.connection() as db: db.execute('UPDATE reset_holds SET until=0')
        service.refresh_reset_holds()
        self.assertEqual(choose_client([replacement], [], 4, row['payload'], service.preferences, 10), replacement)

    def test_automatic_orphan_reassignment_retains_a_bounded_counter_and_dispatches_other_account(self):
        service = self.service(); row = self.orphan(service); other = self.browser(service, 'Profile 24')
        service._tick(); current = service.store.get(row['id'])
        self.assertEqual(current['status'], 'preparing'); self.assertEqual(current['browser'], other['id'])
        self.assertEqual(current['remote']['upload_recovery']['count'], 1)
        self.assertFalse(service.store.attempt_charged(row['attempt']))

    def test_ready_replacement_with_changed_id_is_not_ignored_forever(self):
        service = self.service(); row = self.orphan(service)
        replacement = self.browser(service, 'Profile 23'); replacement['account'] = row['remote']['account']
        self.assertTrue(service.watch_unsubmitted())
        reset = service.store.get(row['id']); self.assertEqual(reset['status'], 'queued'); self.assertFalse(reset['browser'])
        # Wait out an old native operation before reusing the same page/account.
        service.store.finish_reset_hold(row['attempt'], row['browser']); service.refresh_reset_holds(); service._tick()
        self.assertEqual(service.store.get(row['id'])['browser'], replacement['id'])

    def test_reset_can_start_a_different_offline_profile_instead_of_reopening_the_original(self):
        service = self.service(); row = self.orphan(service)
        service.preferences.update(profiles=['Profile 23', 'Profile 24'], profile_projects={
            'Profile 23': credits.PROJECT, 'Profile 24': credits.PROJECT})
        service.launched.add('Profile 23'); service.launching['Profile 23'] = time.time()
        launched = []; service.launch_requested.connect(launched.append)
        service.execute(self.command(row)); service._tick()
        self.assertEqual(launched, ['Profile 24'])
        self.assertNotIn('Profile 23', service.launched)

    def test_automatic_reassignment_limit_and_manual_reset_rearms_it(self):
        service = self.service(); row = self.orphan(service, count=3)
        self.assertTrue(service.watch_unsubmitted()); failed = service.store.get(row['id'])
        self.assertEqual(failed['status'], 'failed'); self.assertIn('完全重置', failed['error'])
        service.execute(self.command(failed)); self.assertEqual(service.store.get(row['id'])['remote'], {})

    def test_no_automatic_reassignment_before_wait_or_during_pause_live_request_or_paid_evidence(self):
        for condition in ('wait', 'pause', 'safety', 'pending', 'charge', 'evidence', 'disabled'):
            with self.subTest(condition=condition):
                self.setUp()
                service = self.service(); row = self.orphan(service)
                if condition == 'wait':
                    remote = copy.deepcopy(row['remote']); remote['connection_wait_at'] = time.time()
                    remote['transfer_retries']['upload']['waiting_since'] = time.time()
                    service.store.update(row['id'], 'uploading', remote=remote)
                elif condition == 'pause': service.paused = True
                elif condition == 'safety': service.safety_paused = True
                elif condition == 'pending':
                    client = self.browser(service); client['id'] = row['browser']; service.request(client, 'upload_images', job=row['id'])
                elif condition == 'charge': service.store.reserve(row['id'], row['attempt'], 10, 'duration_rule')
                elif condition == 'evidence': service.store.update(row['id'], 'uploading', remote={**row['remote'], 'upstream_task': 'paid'})
                elif condition == 'disabled': service.preferences['timing'] = {'upload_reassign_attempts': 0}
                before = service.store.get(row['id']); self.assertFalse(service.watch_unsubmitted())
                self.assertEqual(service.store.get(row['id']), before)
                service.execute({'action': 'stop'})

    def test_waiting_upload_can_repair_its_page_but_live_and_paid_work_cannot(self):
        service = self.service(); row = self.orphan(service)
        original = self.browser(service, 'Profile 23'); original['id'] = row['browser']
        original.update(account=row['remote']['account'], page_ready=False, connected=time.time()-90)
        self.assertFalse(occupied(service, original)); self.assertTrue(repair_idle(service, original))
        self.assertFalse(service.watch_unsubmitted(), 'allow the in-flight page repair to finish first')
        service.pending.clear(); service.request(original, 'upload_images', job=row['id'])
        self.assertTrue(occupied(service, original))
        service.pending.clear(); service.store.reserve(row['id'], row['attempt'], 10, 'duration_rule')
        self.assertTrue(occupied(service, original)); self.assertFalse(repair_idle(service, original))

    def test_automatic_recovery_does_not_clear_account_or_credit_holds(self):
        service = self.service(); row = self.orphan(service)
        service.preferences.update(browser_blocks={'profile:Profile 23': '保留隔离'}, credit_cooldowns={'old@example.test': {'balance': 0}})
        before = copy.deepcopy(service.preferences); service.watch_unsubmitted()
        self.assertEqual(service.preferences, before)

    def test_ui_reset_is_visible_pre_submit_has_no_paid_dialog_and_risky_cancel_does_nothing(self):
        dialog, _, commands = self.ui(); service = self.service(); row = self.orphan(service)
        dialog.state = {'jobs': [row]}; dialog.selected_ids = lambda: [row['id']]
        with patch.object(QtWidgets.QMessageBox, 'warning') as warning:
            dialog.reset_button.click()
        warning.assert_not_called(); self.assertEqual(commands[-1]['action'], 'reset_tasks')
        request = commands[-1]['request_id']; self.assertFalse(dialog.reset_button.isEnabled())
        dialog.tasks_reset({'request_id': request, 'success': True, 'reset': [row['id']], 'skipped': []})
        self.assertTrue(dialog.reset_button.isEnabled()); self.assertIn('其他可用浏览器', dialog.note.text())
        row['status'] = 'generating'; before = len(commands)
        with patch.object(QtWidgets.QMessageBox, 'warning', return_value=QtWidgets.QMessageBox.StandardButton.Cancel):
            dialog.reset_button.click()
        self.assertEqual(len(commands), before)
        with patch.object(QtWidgets.QMessageBox, 'warning', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            dialog.reset_button.click()
        self.assertTrue(commands[-1]['confirmed_submitted'])


if __name__ == '__main__': unittest.main()
