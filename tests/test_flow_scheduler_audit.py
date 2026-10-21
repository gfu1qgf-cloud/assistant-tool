"""Adversarial scheduler transitions using temporary SQLite and fake sockets."""
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.balance_dispatch import prefer_smaller
from app_plugins.builtin.flow_assistant.credit_cache import with_balance
from app_plugins.builtin.flow_assistant.image_affinity import remember_owner
from app_plugins.builtin.flow_assistant.store import choose_client


class SchedulerAuditTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser

    def queue(self, service, **kwargs):
        service.store.enqueue(self.jobs(duration=8, budget=0, **kwargs))
        return service.store.rows()[-1]

    def offline(self, service, profile, points):
        client = self.browser(service, profile, points)
        service.preferences = with_balance(service.preferences, client, service.clients.values())
        service.clients.pop(client['socket'])
        return client

    def test_configured_balance_lifetime_is_used_by_every_dispatch_gate(self):
        service = self.service(); client = self.browser(service)
        row = self.queue(service)
        client['credits_at'] = time.time() - 7 * 3600
        for lifetime in (12 * 3600, 0):
            service.preferences['timing'] = {'credit_validity_seconds': lifetime}
            self.assertIs(choose_client([client], service.store.rows(), 4, row['payload'],
                service.preferences, 12), client)

    def test_definite_failed_small_profile_does_not_block_available_account_forever(self):
        service = self.service(); rich = self.browser(service, 'Rich', 1300)
        small = self.offline(service, 'Small', 26)
        service.preferences.update(profiles=['Small', 'Rich'],
            browser_launch_failures={'Small': {'message': 'process never started'}})
        row = self.queue(service)
        self.assertFalse(prefer_smaller(service, row, rich))
        self.assertNotIn(small['profile'], service.preferences.get('browser_slots', {}))

    def test_unreleased_image_does_not_block_a_released_image_via_small_balance_probe(self):
        service = self.service(); small = self.offline(service, 'Small', 26)
        rich = self.browser(service, 'Rich', 1300)
        service.preferences['profiles'] = ['Small', 'Rich']
        first = self.queue(service); remember_owner(service, first, small)
        image = self.target / 'other.png'; image.write_bytes(b'another image')
        second = self.queue(service, images=[image])
        service.released = {second['id']}; service.paused = True
        # Small is the selected pool's account, so it must be restored for the
        # released image even though another, paused image remains queued.
        self.assertTrue(prefer_smaller(service, second, rich))
        self.assertIn('Small', service.preferences.get('browser_slots', {}))

    def test_offline_launch_skips_known_balance_below_job_cost(self):
        service = self.service(); self.offline(service, 'TooSmall', 10)
        self.offline(service, 'Funded', 26)
        service.preferences.update(profiles=['TooSmall', 'Funded'])
        self.queue(service); service.paused = False
        launches = []; service.launch_requested.connect(launches.append)
        service._tick()
        self.assertEqual(launches, ['Funded'])

    def test_reconnected_profile_cannot_bypass_unresolved_old_account_result(self):
        service = self.service(); client = self.browser(service, 'SameProfile', 50)
        row = self.queue(service)
        held = {'status': 'uncertain', 'browser': 'old-browser-id',
                'remote': {'account': 'old-account@example.test', 'profile': client['profile']}}
        self.assertIsNone(choose_client([client], [held, row], 4, row['payload'], service.preferences, 12))

    def test_native_busy_and_reset_hold_apply_to_all_sessions_of_the_same_account(self):
        for state in ({'native_busy': True}, {'reset_wait_until': time.time() + 60},
                      {'credit_close': {'state': 'checking'}}, {'credit_close': {'state': 'closing'}}):
            with self.subTest(state=state):
                service = self.service(); one = self.browser(service, 'One'); two = self.browser(service, 'Two')
                two['account'] = one['account']; one.update(state)
                row = self.queue(service)
                self.assertIsNone(choose_client([one, two], service.store.rows(), 4,
                    row['payload'], service.preferences, 12))

    def test_short_balance_lifetime_expires_without_erasing_observation(self):
        service = self.service(); client = self.browser(service)
        row = self.queue(service); client['credits_at'] = time.time() - 3700
        service.preferences['timing'] = {'credit_validity_seconds': 3600}
        self.assertIsNone(choose_client([client], service.store.rows(), 4, row['payload'], service.preferences, 12))
        self.assertEqual(client['credits'], 50)

    def test_paid_and_download_holds_bind_profile_even_after_browser_and_account_change(self):
        service = self.service(); client = self.browser(service, 'SameProfile')
        row = self.queue(service)
        for status in ('submitting', 'generating', 'downloading', 'download_failed', 'uncertain'):
            with self.subTest(status=status):
                held = {'status': status, 'browser': 'old-id',
                    'remote': {'account': 'old@example.test', 'profile': 'SameProfile'}}
                self.assertIsNone(choose_client([client], [held, row], 4, row['payload'], service.preferences, 12))

    def test_waiting_for_one_small_account_does_not_stall_other_ready_lanes(self):
        service = self.service(); rich = self.browser(service, 'Rich', 1300)
        self.offline(service, 'Small', 26); ready = self.browser(service, 'Ready', 90)
        service.preferences.update(profiles=['Rich', 'Small', 'Ready'], concurrency=4,
            profile_lanes={'Rich': 6, 'Small': 6, 'Ready': 8},
            profile_lane_modes={p: 'fixed' for p in ('Rich', 'Small', 'Ready')})
        service.store.enqueue(self.jobs(duration=6, budget=0)); first = service.store.rows()[0]
        service.store.set_payload(first['id'], {**first['payload'], 'priority': 100})
        second = self.queue(service); service.paused = False
        service._tick()
        self.assertEqual(service.store.get(first['id'])['status'], 'queued')
        self.assertEqual(service.store.get(second['id'])['browser'], ready['id'])
        self.assertNotEqual(service.store.get(second['id'])['browser'], rich['id'])

    def test_many_waiting_clips_share_one_probe_without_blocking_a_ready_lane(self):
        service = self.service(); self.browser(service, 'Rich', 1300)
        self.offline(service, 'Small', 26); ready = self.browser(service, 'Ready', 90)
        service.preferences.update(profiles=['Rich', 'Small', 'Ready'], concurrency=4,
            profile_lanes={'Rich': 6, 'Small': 6, 'Ready': 8},
            profile_lane_modes={p: 'fixed' for p in ('Rich', 'Small', 'Ready')})
        for batch in range(2):
            service.store.enqueue(self.jobs(duration=6, budget=0, count=15, prompt=f'waiting batch {batch}'))
        for row in service.store.rows():
            service.store.set_payload(row['id'], {**row['payload'], 'priority': 100})
        last = self.queue(service); service.paused = False
        with patch('app_plugins.builtin.flow_assistant.service.prefer_smaller', wraps=prefer_smaller) as probe:
            service._tick()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(service.store.get(last['id'])['browser'], ready['id'])


if __name__ == '__main__': unittest.main()
