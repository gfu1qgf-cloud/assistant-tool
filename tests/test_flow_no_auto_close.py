"""Idle manual Chrome windows must never require closure for queue progress."""
import unittest

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.browser_slots import (
    admission_profiles, can_start, occupied_profiles, request_start, seen, summary)
from app_plugins.builtin.flow_assistant.browser_rotation import watch
from app_plugins.builtin.flow_assistant.credit_cooldown import close_idle_exhausted, credit_hold
from app_plugins.builtin.flow_assistant.idle_close import request_close
from app_plugins.builtin.flow_assistant.high_credit import allowed
from app_plugins.builtin.flow_assistant.store import choose_client


class NoAutoCloseTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    browser = fixtures.CreditCooldownTests.browser
    client = fixtures.CreditCooldownTests.client
    jobs = fixtures.CreditCooldownTests.jobs
    observe = fixtures.CreditCooldownTests.observe

    def test_five_manual_windows_do_not_block_three_other_accounts_while_one_result_is_held(self):
        service = self.service()
        profiles = ['Rich', 'Low1', 'Low2', 'Low3', 'Original', 'Next1', 'Next2', 'Next3']
        service.preferences.update(concurrency=4, profiles=profiles,
            profile_projects={profile: fixtures.PROJECT for profile in profiles})
        rich = self.browser(service, 'Rich', points=814, close=True)
        self.observe(service, rich, 814)
        service.preferences['high_credit_accounts'][rich['account']].update(
            decision='skipped', decision_source='user_declined', asked_cycle=0)
        low = [self.browser(service, profile, close=True) for profile in profiles[1:4]]
        for client in low:
            self.observe(service, client, 2)
        original = self.browser(service, 'Original', points=1, close=True)
        before = list(service.clients)
        for client in service.clients.values():
            seen(service, client)
        service.store.enqueue(self.jobs(duration=8, count=20, budget=0))
        service.store.enqueue(self.jobs(duration=8, count=8, budget=0, prompt='Another queued prompt'))
        row = service.store.rows()[0]
        service.store.update(row['id'], 'uncertain', browser=original['id'], attempt='f' * 32,
            remote={'profile': original['profile'], 'account': original['account']})
        self.assertEqual(len(occupied_profiles(service)), 5)
        self.assertTrue(can_start(service, 'Next1'))
        launches = []
        service.launch_requested.connect(launches.append)
        service.paused = False
        for _ in range(24):
            service._tick()
            for profile in launches:
                if not any(c.get('profile') == profile for c in service.clients.values()):
                    self.browser(service, profile, points=40)
            if sum(r['status'] == 'preparing' for r in service.store.rows()) == 3:
                break
        self.assertEqual(len(set(launches)), 1)
        self.assertEqual(sum(r['status'] == 'preparing' for r in service.store.rows()), 1)
        self.assertEqual(service.store.get(row['id'])['status'], 'uncertain')
        self.assertEqual(service.store.get(row['id'])['attempt'], 'f' * 32)
        self.assertEqual(sum(r['status'] == 'queued' for r in service.store.rows()), 26)
        for socket in before:
            self.assertIn(socket, service.clients)
            socket.close.assert_not_called()
        self.assertFalse(allowed(rich, service.preferences))
        self.assertTrue(all(credit_hold(c, service.preferences) for c in low))
        self.assertFalse(any(p['action'] == 'close_exhausted' for p in service.pending.values()))
        self.assertNotIn('退出', summary(service)['message'])

    def test_five_usable_manual_browsers_still_run_at_most_four_tasks(self):
        service = self.service()
        clients = [self.browser(service, 'Manual ' + str(i), points=40) for i in range(5)]
        service.preferences['concurrency'] = 4
        service.store.enqueue(self.jobs(duration=8, count=12, budget=0))
        service.paused = False
        for _ in range(12):
            service._tick()
        self.assertEqual(sum(r['status'] == 'preparing' for r in service.store.rows()), 4)
        self.assertEqual(len(service.clients), 5)
        self.assertFalse(any(p['action'] == 'close_exhausted' for p in service.pending.values()))

    def test_pending_unconnected_launches_are_still_bounded_and_never_repeated(self):
        service = self.service()
        profiles = ['Pending ' + str(i) for i in range(6)]
        service.preferences.update(concurrency=4, profiles=profiles,
            profile_projects={p: fixtures.PROJECT for p in profiles})
        for profile in profiles[:4]:
            self.assertTrue(request_start(service, profile))
        self.assertFalse(request_start(service, profiles[0]))
        self.assertFalse(can_start(service, profiles[4]))
        self.assertEqual(len(admission_profiles(service)), 4)

    def test_all_close_entry_points_are_inert_even_for_healthy_idle_exhausted_browser(self):
        service = self.service()
        client = self.browser(service, points=2, close=True)
        self.observe(service, client, 2)
        client['credit_check']['at'] -= 31
        client['socket'].sendTextMessage.reset_mock()
        self.assertFalse(close_idle_exhausted(service))
        self.assertFalse(request_close(service, client, {}, 'close'))
        self.assertFalse(watch(service, []))
        self.assertFalse(service.request(client, 'close_exhausted', {'idle': True}))
        self.assertFalse(service.pending)
        client['socket'].sendTextMessage.assert_not_called()

    def test_old_close_flags_do_not_hold_accounts_or_prevent_manual_credit_reads(self):
        service = self.service()
        client = self.browser(service, points=40)
        client['credit_close'] = {'state': 'closing', 'reason': 'high_credit_backup'}
        service.store.enqueue(self.jobs(duration=8, budget=0))
        row = service.store.rows()[0]
        self.assertIs(choose_client([client], service.store.rows(), 4, row['payload'], service.preferences, 12), client)
        self.assertTrue(service.request(client, 'get_points', manual_credit=True))
        self.assertEqual([p['action'] for p in service.pending.values()], ['get_points'])


if __name__ == '__main__':
    unittest.main()
