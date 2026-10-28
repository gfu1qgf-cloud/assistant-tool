"""Low-credit disconnects and queued starts cannot produce reopening loops."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
import test_flow_launch_spacing as spacing
from app_plugins.builtin.flow_assistant.browser_slots import disconnected, closed, request_start
from app_plugins.builtin.flow_assistant.credit_cooldown import credit_hold
from app_plugins.builtin.flow_assistant.exhausted_browser import KEY, reason
from app_plugins.builtin.flow_assistant.policy import block_for
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION, project_identity


class ExhaustedDisconnectTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    observe = fixtures.CreditCooldownTests.observe

    def exhausted(self):
        service = self.service(); service.timer.stop()
        client = self.browser(service, 'Low')
        service.preferences.update(profiles=['Low', 'Spare'], concurrency=1)
        service.preferences['profile_projects']['Spare'] = fixtures.PROJECT
        self.observe(service, client, 1)
        disconnected(service, client)
        service.clients.clear()
        return service, client

    def test_low_disconnect_survives_expired_cooldown_and_native_absence_without_reopening(self):
        service, client = self.exhausted()
        at = time.time()+30000
        with patch('time.time', return_value=at):
            self.assertFalse(credit_hold({'profile':'Low'}, service.preferences))
            closed(service, 'Low')
            self.assertTrue(reason({'profile':'Low'}, service.preferences))
            service.store.enqueue(self.jobs(duration=8, budget=0)); service.paused = False
            launches=[]; service.launch_requested.connect(launches.append)
            for _ in range(5): service._tick()
            self.assertEqual(launches, ['Spare'])
            self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))
            self.assertFalse(request_start(service, 'Low', self.jobs(duration=8)[0]))
        self.assertIn('Low', service.store.settings()[KEY])
        restarted = self.service()
        with patch('time.time', return_value=at):
            self.assertFalse(reason({'profile':'Low'}, restarted.preferences),'restart clears a hold without a known future refresh')

    def test_a_known_daily_refresh_allows_recheck_after_that_deadline(self):
        service = self.service(); client = self.browser(service, 'Low')
        now = time.time(); refresh = now+120
        service.preferences.update(profiles=['Low'], credit_refreshes={client['account']:{
            'daily_time':'23:59', 'next_at':refresh, 'timezone':'Europe/Rome'}})
        self.observe(service, client, 0); disconnected(service, client)
        self.assertTrue(reason({'profile':'Low'}, service.preferences, refresh-1))
        self.assertFalse(reason({'profile':'Low'}, service.preferences, refresh+1))

    def test_unknown_refresh_is_not_a_permanent_ban_after_three_days(self):
        service, client = self.exhausted()
        at = time.time()+3*86400
        with patch('time.time',return_value=at):
            self.assertFalse(reason({'profile':'Low'},service.preferences))
            self.assertFalse(block_for({'profile':'Low'},service.preferences))
            service.store.enqueue(self.jobs(duration=8,budget=0));service.paused=False
            launches=[];service.launch_requested.connect(launches.append);service._tick()
            self.assertEqual(launches,['Low'],'a stale balance is rechecked, not assumed sufficient')
            self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))

    def test_stale_low_disconnect_after_known_refresh_does_not_defer_until_tomorrow(self):
        service,client=self.exhausted();now=time.time();account=client['account']
        client['credits_at']=now-120
        service.preferences['credit_balances'][account]['observed_at']=client['credits_at']
        service.preferences['credit_refreshes']={account:{'daily_time':'23:59','next_at':now-60,'timezone':'Europe/Rome'}}
        disconnected(service,client)
        self.assertFalse(reason({'profile':'Low'},service.preferences,now+1))

    def test_more_recent_low_read_still_waits_for_next_refill(self):
        service,client=self.exhausted();now=time.time();entry=service.preferences[KEY]['Low']
        entry['refresh_at']=now-60
        balance=service.preferences['credit_balances'][client['account']]
        balance['observed_at']=now
        self.assertTrue(reason({'profile':'Low'},service.preferences,now+1))

    def test_positive_confirmed_read_releases_guard_and_manual_connected_page_is_not_blocked(self):
        service, client = self.exhausted()
        self.assertFalse(reason(client, service.preferences))
        service.clients[client['socket']] = client
        with patch('time.time', return_value=time.time()+1): self.observe(service, client, 50, manual=True)
        self.assertFalse(block_for({'profile':'Low'}, service.preferences))

    def test_disconnecting_after_an_old_refresh_waits_for_the_next_daily_refill(self):
        service = self.service(); client = self.browser(service, 'Low')
        now = time.time()
        service.preferences.update(profiles=['Low'], credit_refreshes={client['account']:{
            'daily_time':'23:59', 'next_at':now-60, 'timezone':'Europe/Rome'}})
        self.observe(service, client, 0)
        with patch('app_plugins.builtin.flow_assistant.credit_refresh.next_daily_time', return_value=now+86400):
            disconnected(service, client)
        self.assertTrue(reason({'profile':'Low'}, service.preferences, now+1))
        self.assertFalse(reason({'profile':'Low'}, service.preferences, now+86401))

    def test_old_disconnected_low_cache_is_skipped_without_changing_manual_iterator_cooldown(self):
        from app_plugins.builtin.flow_assistant.browser_manager import skip_reason
        service, client = self.exhausted()
        service.preferences.pop(KEY)
        with patch('time.time', return_value=time.time()+30000):
            self.assertTrue(reason({'profile':'Low'}, service.preferences))
            self.assertFalse(skip_reason('Low', service.preferences, []), 'manual Chrome expiry policy stays unchanged')

    def test_ordinary_disconnect_has_no_exhaustion_guard(self):
        service = self.service(); client = self.browser(service, 'Healthy')
        self.observe(service, client, 50); disconnected(service, client)
        self.assertNotIn('Healthy', service.preferences.get(KEY, {}))
        self.assertFalse(reason({'profile':'Healthy'}, service.preferences))

    def test_changed_project_does_not_inherit_another_project_guard(self):
        service, client = self.exhausted()
        service.preferences['profile_projects']['Low'] = 'https://flow.google.com/project/different'
        self.assertFalse(reason({'profile':'Low'}, service.preferences))


class QueuedLaunchRecheckTests(unittest.TestCase):
    setUpClass = spacing.LaunchSpacingTests.__dict__['setUpClass']
    setUp = spacing.LaunchSpacingTests.setUp
    ui = spacing.LaunchSpacingTests.ui
    fixture = spacing.LaunchSpacingTests.fixture
    expire = spacing.LaunchSpacingTests.expire

    def test_low_balance_received_while_waiting_cancels_the_actual_chrome_launch(self):
        plugin, launcher, commands = self.fixture()
        plugin.launch_browser('Profile 23', [plugin.snapshot['preferences']['profile_projects']['Profile 23']])
        plugin._launch_checked({'profile':'Profile 24', 'status':'enabled', 'version':EXTENSION_VERSION})
        self.assertEqual(launcher.launch_profile.call_count, 1)
        now=time.time(); prefs=plugin.snapshot['preferences']; account='low@example.test'
        prefs.update(credit_cooldowns={account:{'at':now,'until':now+21600,'balance':0}},
            credit_cooldown_links={'profile:Profile 24':account})
        self.expire(plugin)
        self.assertEqual(launcher.launch_profile.call_count, 1)
        self.assertTrue(any(c.get('action')=='browser_launch_result' and c.get('profile')=='Profile 24'
            and c.get('cancelled') for c in commands))
        self.assertFalse(plugin.pending_browser_launches)

    def test_removing_group_member_cancels_its_already_queued_launch(self):
        plugin, launcher, commands = self.fixture()
        prefs=plugin.snapshot['preferences']; prefs['profiles']=['Profile 23','Profile 24']
        plugin.launch_browser('Profile 23', [prefs['profile_projects']['Profile 23']])
        plugin._launch_checked({'profile':'Profile 24', 'status':'enabled', 'version':EXTENSION_VERSION})
        prefs['profiles']=['Profile 23']; self.expire(plugin)
        self.assertEqual(launcher.launch_profile.call_count, 1)


if __name__ == '__main__': unittest.main()
