"""A full browser limit must not starve funded work in existing windows."""
import time
import unittest

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.balance_dispatch import prefer_smaller
from app_plugins.builtin.flow_assistant.browser_slots import occupied_profiles


class CapacityLivenessTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser

    def configured(self):
        service = self.service()
        profiles = ['Rich', 'Small', 'Cold', 'Exhausted', 'Unknown']
        service.preferences.update(concurrency=4, profiles=profiles,
            profile_projects={p: fixtures.PROJECT for p in profiles})
        rich = self.browser(service, 'Rich', 889)
        small = self.browser(service, 'Small', 51)
        cold = self.browser(service, 'Cold')
        cold.update(credits=None, account='', credits_local=False, page_ready=False)
        self.browser(service, 'Exhausted', 0)
        service.small_balance_probes = {'Cold': time.time() - 1}
        service.paused = False
        return service, rich, small

    def test_actual_12_queued_zero_active_four_slots_scenario_starts_existing_small_account(self):
        service, rich, small = self.configured()
        service.store.enqueue(self.jobs(duration=10, budget=0, count=7))
        service.store.enqueue(self.jobs(duration=8, budget=0, count=5))
        launches = []; service.launch_requested.connect(launches.append)
        self.assertEqual(len(occupied_profiles(service)), 4)
        service._tick()
        active = [r for r in service.store.rows() if r['status'] == 'preparing']
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['browser'], small['id'])
        self.assertNotEqual(active[0]['browser'], rich['id'])
        self.assertFalse(launches)

    def test_unavailable_unknown_fifty_hint_cannot_block_usable_fifty_one_balance(self):
        service, rich, small = self.configured()
        service.store.enqueue(self.jobs(duration=10, budget=0))
        row = service.store.rows()[0]
        self.assertFalse(prefer_smaller(service, row, small))
        self.assertFalse(service.preferences.get('browser_slots'))

    def test_open_usable_account_can_progress_when_all_backup_probes_lack_window_capacity(self):
        service, rich, small = self.configured()
        rich['credits'] = 90  # Test an ordinary funded fallback; high backups require separate explicit consent.
        service.preferences['parameter_blocks'] = {'profile:Small': [{
            'model':'omni_1_1_flash', 'duration':10, 'aspect':'9:16', 'resolution':'720p', 'reason':'unsupported'}]}
        service.store.enqueue(self.jobs(duration=10, budget=0))
        service._tick()
        row = service.store.rows()[0]
        self.assertEqual(row['status'], 'preparing')
        self.assertEqual(row['browser'], rich['id'])


if __name__ == '__main__': unittest.main()
