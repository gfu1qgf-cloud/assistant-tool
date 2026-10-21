"""High-credit backups never consume credits without timely explicit consent."""
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from PyQt6 import QtTest

import test_flow_credit_cooldown as fixtures
from flow_reference_fixtures import upload_receipt
from app_plugins.builtin.flow_assistant.credit_cache import with_balance, restore_balance
from app_plugins.builtin.flow_assistant.credit_policy import debit_balance
from app_plugins.builtin.flow_assistant.high_credit import (allowed, is_high, record, note_normal_spend)
from app_plugins.builtin.flow_assistant.high_credit_ui import HighCreditConfirmation
from app_plugins.builtin.flow_assistant.store import QueueStore, choose_client
from app_plugins.builtin.flow_assistant.policy import capable
from app_plugins.builtin.flow_assistant.image_affinity import dispatch_order
from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
from app_plugins.builtin.flow_assistant.timing import value


class HighCreditTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    observe = fixtures.CreditCooldownTests.observe

    def queue(self, service, count=4):
        service.store.enqueue(self.jobs(duration=8, count=count, budget=0))
        service.paused = False
        return service.store.rows()

    def answer(self, service, approved, **updates):
        request = service.high_credit_pending
        self.assertIsNotNone(request)
        service.execute({'action': 'high_credit_decision', 'nonce': request['nonce'],
                         'approved': approved, **updates})

    def test_threshold_is_two_tiers_and_exactly_one_hundred_is_protected(self):
        service = self.service()
        for points in (50, 99, 100, 101, 814, 1038):
            client = self.browser(service, 'Profile ' + str(points), points=points)
            self.observe(service, client, points)
            self.assertEqual(is_high(client, service.preferences), points >= 100)
            self.assertEqual(capable(client, self.jobs(duration=8)[0], service.preferences, 12), points < 100)
        self.assertEqual(value(service.preferences, 'high_credit_confirmation_seconds'), 5)

    def test_cached_existing_hundreds_are_migrated_and_cannot_bypass_the_gate(self):
        service = self.service(); rich = self.browser(service, points=814)
        prefs = with_balance(service.preferences, rich, [])
        prefs['profiles'] = [rich['profile']]
        prefs.pop('high_credit_accounts', None); prefs.pop('high_credit_links', None)
        service.commit_preferences(prefs); service.clients.clear(); self.queue(service)
        launched = []; service.launch_requested.connect(launched.append); service._tick()
        self.assertTrue(record(rich, QueueStore(self.namespace).settings()))
        self.assertEqual(service.high_credit_pending['account'], rich['account'])
        self.assertEqual(launched, [])

    def test_retained_rich_account_does_not_outrank_a_fifty_credit_account(self):
        service = self.service(); rich = self.browser(service, 'Rich', points=814)
        small = self.browser(service, 'Small', points=39); rows = self.queue(service)
        service.preferences.update(concurrency=1, account_images={rich['account']:
            {'active': True, 'account': rich['account'], 'image': rows[0]['payload']['image_identity']['sha256']}})
        self.assertLess(dispatch_order(small, service.preferences), dispatch_order(rich, service.preferences))
        service._tick()
        self.assertEqual(service.store.get(rows[0]['id'])['browser'], small['id'])
        self.assertIsNone(service.high_credit_pending)

    def test_four_small_accounts_still_run_parallel_without_asking_for_rich(self):
        service = self.service(); rich = self.browser(service, 'Rich', points=1038)
        small = [self.browser(service, 'Small ' + str(i), points=50 + i * 10) for i in range(4)]
        self.queue(service, 8); service.preferences['concurrency'] = 4
        for _ in range(6): service._tick()
        active = [r for r in service.store.rows() if r['status'] == 'preparing']
        self.assertEqual({r['browser'] for r in active}, {c['id'] for c in small})
        self.assertFalse(allowed(rich, service.preferences)); self.assertIsNone(service.high_credit_pending)

    def test_busy_fifty_credit_account_is_not_considered_exhausted(self):
        service = self.service(); self.browser(service, 'Rich', points=1038)
        small = self.browser(service, 'Small', points=50); rows = self.queue(service)
        service.store.update(rows[0]['id'], 'generating', browser=small['id'],
            remote={'account': small['account'], 'profile': small['profile']})
        service._tick()
        self.assertIsNone(service.high_credit_pending)
        self.assertEqual(service.store.get(rows[1]['id'])['status'], 'queued')

    def test_expired_small_balance_gets_a_refresh_before_backup_question(self):
        service = self.service(); self.browser(service, 'Rich', points=1038)
        small = self.browser(service, 'Small', points=50); self.queue(service)
        small.update(credits_at=time.time() - 7 * 3600, connected=time.time() - 7 * 3600)
        service._tick()
        self.assertIsNone(service.high_credit_pending)
        self.assertTrue(any(p['action'] == 'get_points' and p['client'] == small['id'] for p in service.pending.values()))

    def test_unknown_offline_profile_is_probed_before_any_high_credit_question(self):
        service = self.service(); self.browser(service, 'Rich', points=1038)
        service.preferences.update(profiles=['Unknown'], profile_projects={'Unknown': fixtures.PROJECT})
        rows = self.queue(service); launched = []; service.launch_requested.connect(launched.append)
        service._tick()
        self.assertEqual(launched, ['Unknown']); self.assertIsNone(service.high_credit_pending)
        self.assertEqual(service.store.get(rows[0]['id'])['status'], 'queued')

    def test_unknown_online_profile_still_confirming_is_not_skipped_early(self):
        service = self.service(); self.browser(service, 'Rich', points=1038)
        small = self.browser(service, 'Small', points=50)
        small.update(account='', credits=None, credits_local=False, page_ready=False,
                     setup_pending=True, setup_blocked='扩展正在后台自动确认本配置')
        self.queue(service); service._tick()
        self.assertIsNone(service.high_credit_pending)

    def test_only_high_accounts_require_yes_before_prepare_or_launch(self):
        service = self.service(); rich = self.browser(service, points=100); rows = self.queue(service)
        asked = []; service.high_credit_confirmation_requested.connect(asked.append)
        service._tick(); service._tick()
        self.assertEqual(len(asked), 1); self.assertEqual(service.store.get(rows[0]['id'])['status'], 'queued')
        self.assertFalse(any(p['action'] == 'prepare' for p in service.pending.values()))
        self.answer(service, True); service._tick()
        self.assertEqual(service.store.get(rows[0]['id'])['browser'], rich['id'])

    def test_decline_skips_to_next_high_account_and_does_not_prompt_in_a_loop(self):
        service = self.service(); one = self.browser(service, 'One', points=100)
        two = self.browser(service, 'Two', points=1038); rows = self.queue(service)
        asked = []; service.high_credit_confirmation_requested.connect(asked.append)
        service._tick(); self.assertEqual(asked[-1]['account'], one['account'])
        self.answer(service, False); service._tick(); self.assertEqual(asked[-1]['account'], two['account'])
        self.answer(service, False)
        for _ in range(10): service._tick()
        self.assertEqual(len(asked), 2); self.assertTrue(all(r['status'] == 'queued' for r in service.store.rows()))
        service.execute({'action': 'start'}); service._tick(); self.assertEqual(len(asked), 2)

    def test_timeout_and_late_yes_both_skip_without_charging(self):
        service = self.service(); rich = self.browser(service, points=1038); self.queue(service)
        service._tick(); pending = dict(service.high_credit_pending)
        with patch('time.time', return_value=pending['expires_at'] + .01):
            self.answer(service, True)
        self.assertFalse(allowed(rich, service.preferences)); self.assertEqual(record(rich, service.preferences)['decision'], 'skipped')
        self.assertFalse(any(batch['reserved'] for batch in service.store.batches().values()))

    def test_worker_timeout_is_independent_of_the_gui(self):
        service = self.service(); rich = self.browser(service, points=1038); self.queue(service)
        service._tick(); deadline = service.high_credit_pending['expires_at']
        with patch('time.time', return_value=deadline + .01): service._tick()
        self.assertIsNone(service.high_credit_pending)
        self.assertEqual(record(rich, service.preferences)['decision'], 'skipped')

    def test_wrong_nonce_and_duplicate_yes_cannot_authorize_another_account(self):
        service = self.service(); rich = self.browser(service, points=1038); self.queue(service); service._tick()
        pending = dict(service.high_credit_pending)
        self.answer(service, True, nonce='wrong')
        self.assertFalse(allowed(rich, service.preferences)); self.answer(service, False)
        service.execute({'action': 'high_credit_decision', 'nonce': pending['nonce'], 'approved': True})
        self.assertFalse(allowed(rich, service.preferences))

    def test_fifty_credit_account_arriving_during_question_invalidates_yes(self):
        service = self.service(); rich = self.browser(service, points=1038); self.queue(service); service._tick()
        small = self.browser(service, 'Small', points=50); self.answer(service, True); service._tick()
        self.assertFalse(allowed(rich, service.preferences))
        self.assertEqual(service.store.rows()[0]['browser'], small['id'])

    def test_changed_login_during_question_invalidates_yes(self):
        service = self.service(); rich = self.browser(service, points=1038); self.queue(service); service._tick()
        old = copy.deepcopy(service.high_credit_pending)
        rich['account'] = 'changed@example.test'; self.answer(service, True)
        self.assertFalse(allowed(rich, service.preferences))
        self.assertEqual(service.preferences['high_credit_accounts'][old['account']]['decision'], 'skipped')

    def test_marker_survives_debits_refresh_alias_and_restart_but_consent_does_not(self):
        service = self.service(); rich = self.browser(service, points=100); self.queue(service); service._tick()
        self.answer(service, True); debit_balance(rich, service.clients.values(), 12)
        self.observe(service, rich, 88, manual=True)
        self.assertTrue(is_high(rich, service.preferences)); self.assertTrue(allowed(rich, service.preferences))
        other = {**rich, 'id': 'alias', 'profile': 'Alias', 'credits': 88}
        self.assertTrue(allowed(other, service.preferences))
        service.execute({'action': 'stop'}); restarted = self.service()
        self.assertTrue(is_high(other, restarted.preferences)); self.assertFalse(allowed(other, restarted.preferences))
        self.browser(restarted, points=88); restarted.paused = False; restarted._tick()
        self.assertIsNotNone(restarted.high_credit_pending)

    def test_skip_survives_restart_and_manual_credit_refresh(self):
        service = self.service(); rich = self.browser(service, points=1038); self.queue(service); service._tick()
        self.answer(service, False); self.observe(service, rich, 38, manual=True)
        service.execute({'action': 'stop'}); restarted = self.service()
        replacement = self.browser(restarted, points=38); restarted.paused = False; restarted._tick()
        self.assertTrue(is_high(replacement, restarted.preferences)); self.assertIsNone(restarted.high_credit_pending)
        self.assertFalse(allowed(replacement, restarted.preferences))

    def test_small_account_spend_then_exhaustion_allows_one_new_question(self):
        service = self.service(); rich = self.browser(service, points=1038); self.queue(service); service._tick()
        self.answer(service, False); small = self.browser(service, 'Small', points=50)
        note_normal_spend(service, small); small['credits'] = 2; service._tick()
        self.assertEqual(service.high_credit_pending['account'], rich['account'])
        self.answer(service, False); service._tick(); self.assertIsNone(service.high_credit_pending)

    def test_real_small_submission_advances_the_cycle_once_before_exhaustion_question(self):
        service = self.service(); rich = self.browser(service, points=1038); rows = self.queue(service)
        service._tick(); self.answer(service, False)
        small = self.browser(service, 'Small', points=14); service._tick()
        row = service.store.get(rows[0]['id'])
        key = next(k for k, p in service.pending.items() if p['job'] == row['id'] and p['action'] == 'prepare')
        service._response(small, {'request_id': key, 'success': True,
            'data': {'verified': True, 'model': 'omni_1_1_flash', 'email': small['account']}})
        key = next(k for k, p in service.pending.items() if p['job'] == row['id'] and p['action'] == 'upload_images')
        response = {'request_id': key, 'success': True, 'data': upload_receipt(service, row['id'])}
        service._response(small, response); service._response(small, response)
        self.assertEqual(small['credits'], 2); self.assertEqual(service.preferences['normal_credit_spends'], 1)
        self.assertEqual(rich['credits'], 1038)
        service._tick(); self.assertEqual(service.high_credit_pending['account'], rich['account'])

    def test_new_small_account_between_upload_callbacks_prevents_high_charge_without_a_tick(self):
        service = self.service(); rich = self.browser(service, points=1038); rows = self.queue(service)
        service._tick(); self.answer(service, True); service._tick(); row = service.store.get(rows[0]['id'])
        key = next(k for k, p in service.pending.items() if p['action'] == 'prepare')
        service._response(rich, {'request_id': key, 'success': True,
            'data': {'verified': True, 'model': 'omni_1_1_flash', 'email': rich['account']}})
        key = next(k for k, p in service.pending.items() if p['action'] == 'upload_images')
        response = {'request_id': key, 'success': True, 'data': upload_receipt(service, row['id'])}
        self.browser(service, 'New small', points=50)
        service._response(rich, response)
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        self.assertEqual(rich['credits'], 1038)
        self.assertFalse(any(batch['reserved'] for batch in service.store.batches().values()))

    def test_offline_high_profile_only_opens_after_approval(self):
        service = self.service(); rich = self.browser(service, points=1038)
        service.preferences = with_balance(service.preferences, rich, [])
        service.preferences['profiles'] = [rich['profile']]; service.clients.clear(); self.queue(service)
        launched = []; service.launch_requested.connect(launched.append); service._tick()
        self.assertEqual(launched, []); self.answer(service, True); service._tick()
        self.assertEqual(launched, [rich['profile']])

    def test_new_high_detection_during_upload_stops_before_reserving_or_paying(self):
        service = self.service(); client = self.browser(service, points=50); rows = self.queue(service)
        service._tick(); row = service.store.get(rows[0]['id'])
        service.store.update(row['id'], 'uploading', remote={**row['remote'], 'cost': 12, 'cost_source': 'fixed'})
        row = service.store.get(row['id']); receipt = upload_receipt(service, row['id'])
        self.observe(service, client, 1038, manual=True)
        service.pending['uploaded'] = {'action': 'upload_images', 'client': client['id'], 'job': row['id'],
            'attempt': row['attempt'], 'at': time.time()}
        with patch.object(service, 'media_disks_ready', return_value=True):
            service._response(client, {'request_id': 'uploaded', 'success': True, 'data': receipt})
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        self.assertEqual(client['credits'], 1038)
        self.assertFalse(any(batch['reserved'] for batch in service.store.batches().values()))
        self.assertFalse(any(p['action'] == 'run_tasks' for p in service.pending.values()))

    def test_already_submitted_high_jobs_are_preserved(self):
        service = self.service(); rich = self.browser(service, points=1038); rows = self.queue(service)
        service.store.update(rows[0]['id'], 'generating', browser=rich['id'],
            remote={'account': rich['account'], 'profile': rich['profile'], 'activity_at': time.time()})
        self.observe(service, rich, 1038); service._tick()
        self.assertEqual(service.store.get(rows[0]['id'])['status'], 'generating')

    def test_standalone_selector_also_blocks_unapproved_high_credit(self):
        service = self.service(); rich = self.browser(service, points=1038)
        self.assertIsNone(choose_client([rich], [], 4, preferences=service.preferences))

    def test_manual_retry_allows_new_question_without_removing_marker(self):
        service = self.service(); rich = self.browser(service, points=1038); self.queue(service); service._tick()
        self.answer(service, False)
        service.execute({'action': 'retry_high_credit', 'keys': ['profile:' + rich['profile']]}); service._tick()
        self.assertIsNotNone(service.high_credit_pending); self.assertTrue(is_high(rich, service.preferences))

    def test_confirmation_box_default_escape_close_and_timeout_are_no(self):
        request = {'nonce': 'n', 'account': 'a@example.test', 'profile': 'Profile 1', 'credits': 1038,
                   'expires_at': time.time() + 5}
        box = HighCreditConfirmation(request); self.addCleanup(box.close)
        self.assertEqual(box.defaultButton(), box.button(QtWidgets.QMessageBox.StandardButton.No))
        results = []; box.finished.connect(results.append); box.open()
        QtTest.QTest.keyClick(box, QtCore.Qt.Key.Key_Escape)
        self.assertNotEqual(results[-1], int(QtWidgets.QMessageBox.StandardButton.Yes))
        timeout = HighCreditConfirmation({**request, 'expires_at': time.time() + .05}); self.addCleanup(timeout.close)
        timed = []; timeout.finished.connect(timed.append); timeout.open(); QtTest.QTest.qWait(160)
        self.assertEqual(timed, [int(QtWidgets.QMessageBox.StandardButton.No)])

    def test_plugin_yes_and_close_deliver_explicit_and_negative_decisions(self):
        parent = QtWidgets.QWidget(); self.addCleanup(parent.close)
        plugin = FlowAssistantPlugin(); plugin.context = SimpleNamespace(parent_widget=parent)
        self.addCleanup(plugin.stop); commands = []; plugin.command.connect(commands.append)
        request = {'nonce': 'n', 'account': 'a@example.test', 'profile': 'Profile 1', 'credits': 1038,
                   'expires_at': time.time() + 5}
        plugin._confirm_high_credit(request)
        plugin.high_credit_box.button(QtWidgets.QMessageBox.StandardButton.Yes).click()
        self.assertTrue(commands[-1]['approved'])
        plugin._confirm_high_credit({**request, 'nonce': 'next'})
        plugin.high_credit_box.close(); self.assertFalse(commands[-1]['approved'])


if __name__ == '__main__': unittest.main()
