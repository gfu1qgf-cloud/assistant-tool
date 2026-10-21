"""Funded current accounts must not be displaced by optional offline probes."""
import time
import unittest

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.credit_cache import with_balance
from app_plugins.builtin.flow_assistant.image_affinity import remember_owner, retained_pool
from app_plugins.builtin.flow_assistant.page_readiness import defer_prepare
from app_plugins.builtin.flow_assistant.policy import capable


class DrainBeforeLaunchTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    jobs = fixtures.CreditCooldownTests.jobs

    def offline(self, service, profile, points, *, selected=False):
        client = self.browser(service, profile, points=points)
        service.preferences['profiles'] = list(dict.fromkeys([*service.preferences.get('profiles', []), profile]))
        service.preferences = with_balance(service.preferences, client, service.clients.values())
        if selected:
            remember_owner(service, service.store.rows()[0], client)
        service.clients.pop(client['socket'])
        return client

    def test_ready_ordinary_account_runs_before_unmeasured_fifty_hint(self):
        service = self.service(); existing = self.browser(service, 'Existing', points=70)
        service.preferences.update(concurrency=1, profiles=['Existing', 'Unknown'],
            profile_projects={'Existing': fixtures.PROJECT, 'Unknown': fixtures.PROJECT})
        service.store.enqueue(self.jobs(duration=10, budget=0)); service.paused = False
        launches = []; service.launch_requested.connect(launches.append)
        service._tick()
        self.assertFalse(launches)
        self.assertEqual(service.store.rows()[0]['browser'], existing['id'])

    def test_funded_current_owner_does_not_open_another_old_retained_smaller_account(self):
        service = self.service(); owner = self.browser(service, 'Current', points=70)
        service.preferences['concurrency'] = 1
        service.store.enqueue(self.jobs(duration=10, count=3, budget=0)); service.paused = False
        service._tick(); row = service.store.rows()[0]
        service.store.update(row['id'], 'completed'); service.pending.clear()
        self.offline(service, 'OldSelected', 15, selected=True)
        launches = []; service.launch_requested.connect(launches.append)
        service._tick()
        self.assertFalse(launches)
        self.assertEqual(service.store.rows()[1]['browser'], owner['id'])

    def test_four_reserved_members_wait_for_prepare_retry_without_opening_a_fifth_old_owner(self):
        service = self.service(); service.preferences['concurrency'] = 4
        owners = [self.browser(service, 'Current ' + str(i), points=70+i) for i in range(4)]
        service.store.enqueue(self.jobs(duration=10, count=8, budget=0)); service.paused = False
        for _ in range(4): service._tick()
        row = service.store.rows()[0]; original = next(c for c in owners if c['id'] == row['browser'])
        service.pending = {key: value for key, value in service.pending.items() if value['job'] != row['id']}
        defer_prepare(service, original, row, 'controls waiting', 'FLOW_PAGE_NOT_READY')
        original['page_ready'] = True
        original['prepare_wait']['next_at'] = time.time() + 300
        self.offline(service, 'OldSelected', 15, selected=True)
        self.assertEqual(len(retained_pool(list(service.clients.values()), service.store.rows(), service.preferences)), 4)
        launches = []; service.launch_requested.connect(launches.append)
        service._tick()
        self.assertFalse(launches)
        self.assertEqual(service.store.get(row['id'])['status'], 'queued')

    def test_a_real_preparation_block_allows_replacement_without_approving_high_credit(self):
        service = self.service(); service.preferences['concurrency'] = 1
        owner = self.browser(service, 'Unavailable', points=70)
        service.store.enqueue(self.jobs(duration=10, budget=0)); service.paused = False
        service.preferences['browser_blocks'] = {'profile:Unavailable': 'explicit fixture block'}
        self.offline(service, 'Replacement', 40)
        launches = []; service.launch_requested.connect(launches.append)
        service._tick()
        self.assertEqual(launches, ['Replacement'])
        self.assertEqual(service.store.rows()[0]['status'], 'queued')

    def test_upload_retry_affordability_does_not_block_the_accounts_cheaper_jobs(self):
        service = self.service(); client = self.browser(service, points=15)
        service.store.enqueue(self.jobs(duration=10, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'uploading', attempt='a'*32, browser=client['id'],
            remote={'profile': client['profile'], 'account': client['account'], 'cost': 20})
        self.assertTrue(service.start_upload(service.store.get(row['id']), retry=True))
        self.assertFalse(client.get('blocked'))
        self.assertTrue(capable(client, self.jobs(duration=6, budget=0)[0], service.preferences, 10))


if __name__ == '__main__': unittest.main()
