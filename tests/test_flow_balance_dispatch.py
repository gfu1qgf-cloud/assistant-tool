"""Offline small accounts must be tried before a ready thousand-credit account."""
import copy
import time
import unittest
from flow_reference_fixtures import distinct_jobs
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.credit_cache import with_balance
from app_plugins.builtin.flow_assistant.page_readiness import recover_preparation_block
from app_plugins.builtin.flow_assistant.balance_dispatch import profile_order


class BalanceDispatchTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser

    def queue(self, service):
        service.store.enqueue(self.jobs(duration=8, budget=0)); service.paused = False
        return service.store.rows()[-1]

    def setup_accounts(self, service):
        rich = self.browser(service, 'Profile 1', points=1300)
        small = self.browser(service, 'Profile 2', points=26)
        service.preferences.update(profiles=[rich['profile'],small['profile']])
        service.preferences = with_balance(service.preferences, small, list(service.clients.values()))
        service.clients.pop(small['socket'])
        return rich, small

    def test_ready_rich_account_waits_for_known_funded_small_offline_account(self):
        service = self.service(); rich, small = self.setup_accounts(service); row = self.queue(service)
        launched = []; service.launch_requested.connect(launched.append); service._tick()
        self.assertEqual(launched, [small['profile']]); self.assertEqual(service.store.get(row['id'])['status'], 'queued')
        self.assertFalse(any(p['action'] == 'prepare' for p in service.pending.values()))
        replacement = self.browser(service, small['profile'], points=26); service._tick()
        self.assertEqual(service.store.get(row['id'])['browser'], replacement['id'])
        self.assertNotEqual(service.store.get(row['id'])['browser'], rich['id'])

    def test_fallback_is_bounded_and_does_not_keep_opening_failed_small_profile(self):
        service = self.service(); rich, small = self.setup_accounts(service); row = self.queue(service)
        launched = []; service.launch_requested.connect(launched.append); service._tick(); service._tick()
        self.assertEqual(launched, [small['profile']])
        deadline = service.launching[small['profile']] + 90
        with patch('time.time', return_value=deadline + .1):
            service._tick()
            request = service.high_credit_pending
            self.assertIsNotNone(request)
            service.execute({'action': 'high_credit_prompt_shown', 'nonce': request['nonce'], 'shown_at': time.time()})
            service.execute({'action': 'high_credit_decision', 'nonce': request['nonce'], 'approved': True})
            service._tick()
        self.assertEqual(service.store.get(row['id'])['browser'], rich['id'])
        self.assertEqual(launched, [small['profile']])

    def test_browser_start_order_also_uses_cached_balance_not_profile_list_order(self):
        service = self.service(); rich, small = self.setup_accounts(service)
        service.preferences = with_balance(service.preferences, rich, list(service.clients.values()))
        service.clients.clear(); self.queue(service)
        launched = []; service.launch_requested.connect(launched.append); service._tick()
        self.assertEqual(launched, [small['profile']])

    def test_unavailable_small_accounts_require_consent_and_busy_small_is_not_exhausted(self):
        for mode in ('banned','low','busy'):
            with self.subTest(mode=mode):
                self.setUp(); service = self.service(); rich, small = self.setup_accounts(service)
                if mode == 'banned': service.execute({'action':'mark_account_unavailable', 'keys':['profile:'+small['profile']]})
                elif mode == 'low':
                    small['credits'] = 2
                    service.preferences = with_balance(service.preferences, small, [])
                else:
                    service.store.enqueue(distinct_jobs(self, 1, duration=8, budget=0)); busy = service.store.rows()[-1]
                    service.store.update(busy['id'], 'generating', browser=small['id'], remote={'profile':small['profile'],'account':small['account']})
                row = self.queue(service); before = copy.deepcopy(service.preferences)
                service._tick()
                if mode == 'busy':
                    self.assertIsNone(service.high_credit_pending)
                    self.assertEqual(service.store.get(row['id'])['status'], 'queued')
                else:
                    self.assertEqual(service.store.get(row['id'])['status'], 'queued')
                    self.assertIsNotNone(service.high_credit_pending)
                    service.execute({'action': 'high_credit_prompt_shown', 'nonce': service.high_credit_pending['nonce'], 'shown_at': time.time()})
                    service.execute({'action': 'high_credit_decision', 'nonce': service.high_credit_pending['nonce'], 'approved': True})
                    service._tick()
                    self.assertEqual(service.store.get(row['id'])['browser'], rich['id'])
                self.assertEqual(service.preferences.get('account_blocks'), before.get('account_blocks'))
                service.execute({'action':'stop'})

    def test_safe_prepare_exhaustion_block_recovers_once_on_a_fresh_exact_idle_page(self):
        service = self.service(); client = self.browser(service); client['preparation_block_recovered'] = False
        message = '页面或浏览器窗口连续未就绪，自动等待已达3次；未提交生成。'
        service.preferences['browser_blocks'] = {'profile:'+client['profile']:message}
        client['prepare_wait'] = {'count':4,'next_at':time.time()+60}
        self.assertTrue(recover_preparation_block(service, client))
        self.assertNotIn('prepare_wait',client); self.assertFalse(service.preferences['browser_blocks'])
        service.preferences['browser_blocks'] = {'profile:'+client['profile']:message}
        self.assertFalse(recover_preparation_block(service, client))

    def test_unknown_profile_is_ranked_as_fifty_but_never_authorizes_paid_dispatch(self):
        service=self.service(); rich=self.browser(service,'Profile 1',points=1300)
        service.preferences.update(profiles=['Profile 1','Profile 2'], profile_projects={
            'Profile 1':fixtures.PROJECT,'Profile 2':fixtures.PROJECT})
        self.assertEqual(profile_order('Profile 2',service.preferences),(0,50))
        row=self.queue(service); launched=[];service.launch_requested.connect(launched.append)
        service._tick();self.assertEqual(launched,['Profile 2']);self.assertEqual(service.store.get(row['id'])['status'],'queued')
        unknown=self.browser(service,'Profile 2');unknown.update(account='',credits=None,credits_local=False)
        service._tick();self.assertIsNone(unknown['credits']);self.assertEqual(service.store.get(row['id'])['status'],'queued')
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))
        self.assertFalse(service.store.batches()[row['payload']['batch_id']]['reserved'])

    def test_online_unknown_account_gets_a_real_probe_before_rich_account(self):
        service=self.service();self.browser(service,'Profile 1',points=1300)
        unknown=self.browser(service,'Profile 2');unknown.update(account='',credits=None,credits_local=False)
        row=self.queue(service);service._tick()
        self.assertTrue(any(p['action']=='get_points' and p['client']==unknown['id'] for p in service.pending.values()))
        self.assertEqual(service.store.get(row['id'])['status'],'queued');self.assertIsNone(unknown['credits'])


if __name__ == '__main__': unittest.main()
