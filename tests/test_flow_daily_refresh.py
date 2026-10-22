"""Browser-observed refill deadlines expire evidence, never manufacture credits."""
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.credit_refresh import deadline, due, watch, next_daily_time
from app_plugins.builtin.flow_assistant.credit_policy import balance_ready
from app_plugins.builtin.flow_assistant.credit_cooldown import credit_hold
from app_plugins.builtin.flow_assistant.credit_cache import restore_balance


class DailyRefreshTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    jobs = fixtures.CreditCooldownTests.jobs

    def observe(self, service, client, points=5, *, at=None, raw=None, checked=True, manual=False):
        now=time.time(); at=now+60 if at is None else at
        if raw is None:
            raw={'account':client['account'],'daily_time':'21:24','next_at':at,
                 'observed_at':now,'timezone':'Europe/Rome','text':'点数每天21:24刷新'}
        self.assertTrue(service.request(client,'get_points',manual_credit=manual))
        key=next(k for k,p in service.pending.items() if p['action']=='get_points' and p['client']==client['id'])
        service._response(client,{'request_id':key,'success':True,'data':{'email':client['account'],
            'remaining_points':points,'credit_refresh_checked':checked,'credit_refresh':raw}})
        return at

    def test_low_account_waits_for_page_deadline_and_persists_across_restart(self):
        service=self.service(); client=self.browser(service,'Daily')
        at=self.observe(service,client,at=time.time()+8*3600)
        self.assertEqual(credit_hold(client,service.preferences)['until'],at)
        saved=service.store.settings(); self.assertEqual(deadline(client,saved),at)
        cached={'profile':'Daily','saved_project':fixtures.PROJECT}
        self.assertTrue(restore_balance(cached,saved))
        self.assertTrue(credit_hold(cached,saved,at-.1))
        self.assertFalse(credit_hold(cached,saved,at))
        self.assertFalse(balance_ready(cached,at,saved))

    def test_daily_deadline_expires_positive_balance_even_when_cache_lifetime_is_unlimited(self):
        service=self.service(); client=self.browser(service,'Daily')
        service.preferences['timing']={'credit_validity_seconds':0}
        at=self.observe(service,client,points=50)
        self.assertTrue(balance_ready(client,at-.1,service.preferences))
        self.assertFalse(balance_ready(client,at,service.preferences))
        self.assertEqual(client['credits'],50)

    def test_tick_rechecks_idle_online_account_at_due_time_without_queue_or_browser_launch(self):
        service=self.service(); client=self.browser(service,'Daily'); at=self.observe(service,client)
        launches=[]; service.launch_requested.connect(launches.append)
        with patch('time.time',return_value=at+1):
            service._tick(); service._tick()
        self.assertEqual([p['action'] for p in service.pending.values()],['get_points'])
        self.assertFalse(launches); self.assertEqual(client['credits'],5)
        self.assertFalse(service.store.rows())

    def test_fresh_observation_after_refill_releases_hold_without_guessing_new_balance(self):
        service=self.service(); client=self.browser(service,'Daily'); at=self.observe(service,client)
        with patch('time.time',return_value=at+1):
            self.observe(service,client,points=50,at=at+86400)
        self.assertEqual(client['credits'],50)
        self.assertFalse(credit_hold(client,service.preferences,at+1))
        self.assertTrue(balance_ready(client,at+1,service.preferences))

    def test_late_pre_refill_observation_still_requires_a_post_refill_read(self):
        service=self.service(); client=self.browser(service,'Daily'); now=time.time(); at=now+60
        with patch('time.time',return_value=at+1):
            self.observe(service,client,points=5,raw={'account':client['account'],'daily_time':'21:24',
                'next_at':at,'observed_at':now,'timezone':'UTC'})
            self.assertTrue(due(client,service.preferences))
            self.assertTrue(watch(service))

    def test_generation_download_and_upload_retry_wait_for_original_task_without_any_reload(self):
        service=self.service(); client=self.browser(service,'Daily'); at=self.observe(service,client)
        service.store.enqueue(self.jobs(duration=8,budget=0)); row=service.store.rows()[0]
        for status in ('preparing','uploading','generating','downloading','uncertain','download_failed'):
            service.store.update(row['id'],status,browser=client['id'],attempt='a'*32,
                remote={'profile':'Daily','account':client['account'],'transfer_retries':{'upload':{'next_at':at+100}}})
            before=service.store.rows()
            with patch('time.time',return_value=at+1): self.assertFalse(watch(service))
            self.assertFalse(service.pending); self.assertEqual(service.store.rows(),before)

    def test_explicit_blocks_and_safety_pause_survive_due_time(self):
        service=self.service(); client=self.browser(service,'Daily'); at=self.observe(service,client)
        service.preferences['browser_blocks']={'profile:Daily':'用户手动关闭／暂停'}
        with patch('time.time',return_value=at+1): self.assertFalse(watch(service))
        service.preferences['browser_blocks']={}; service.safety_paused=True
        with patch('time.time',return_value=at+1): self.assertFalse(watch(service))
        self.assertFalse(service.pending)

    def test_failed_reads_retry_bounded_by_existing_program_settings(self):
        service=self.service(); client=self.browser(service,'Daily'); at=self.observe(service,client)
        service.preferences['timing']={'credit_read_attempts':2,'credit_retry_seconds':7}
        for index in range(4):
            with patch('time.time',return_value=at+1+index*8):
                requested=watch(service)
                self.assertEqual(requested,index<2)
                if requested:
                    key=next(iter(service.pending)); service._response(client,{'request_id':key,
                        'success':False,'error':'积分读取暂时失败'})
        self.assertEqual(client['credits'],5); self.assertEqual(client['credit_refresh_probe']['count'],2)

    def test_missing_schedule_and_wrong_account_never_replace_another_accounts_deadline(self):
        service=self.service(); one=self.browser(service,'One'); two=self.browser(service,'Two')
        at=self.observe(service,one)
        self.observe(service,two,raw={'account':one['account'],'daily_time':'21:24','next_at':at,
            'observed_at':time.time(),'timezone':'UTC'})
        self.assertFalse(deadline(two,service.preferences)); self.assertEqual(deadline(one,service.preferences),at)
        self.observe(service,one,points=50,checked=False,manual=True)
        self.assertEqual(deadline(one,service.preferences),at)
        self.assertTrue(service.request(one,'get_points'))
        key=next(iter(service.pending)); service._response(one,{'request_id':key,'success':True,
            'data':{'email':one['account'],'remaining_points':50,'credit_refresh_checked':True,'credit_refresh':None}})
        self.assertFalse(deadline(one,service.preferences))

    def test_no_page_schedule_keeps_original_six_hour_cooldown(self):
        service=self.service(); client=self.browser(service,'Daily'); now=time.time()
        self.observe(service,client,checked=False)
        hold=credit_hold(client,service.preferences)
        self.assertAlmostEqual(hold['until']-hold['at'],21600)
        self.assertFalse(deadline(client,service.preferences))

    def test_malformed_or_stale_deadlines_are_ignored(self):
        service=self.service(); client=self.browser(service,'Daily'); now=time.time()
        base={'account':client['account'],'daily_time':'21:24','next_at':now+60,'observed_at':now}
        for change in ({'daily_time':'25:24'},{'next_at':True},{'observed_at':now-3600},
                       {'next_at':now-1},{'next_at':now+27*3600}):
            self.observe(service,client,points=50,raw={**base,**change})
            self.assertFalse(deadline(client,service.preferences))

    def test_saved_daily_clock_advances_after_fresh_read_with_no_visible_panel(self):
        from datetime import datetime, timezone
        service=self.service(); client=self.browser(service,'Daily')
        now=datetime(2026,10,22,20,tzinfo=timezone.utc).timestamp()
        at=datetime(2026,10,22,21,24,tzinfo=timezone.utc).timestamp()
        with patch('time.time',return_value=now):
            self.observe(service,client,raw={'account':client['account'],'daily_time':'21:24',
                'next_at':at,'observed_at':now,'timezone':'UTC'})
        with patch('time.time',return_value=at+1): self.observe(service,client,points=50,checked=False)
        self.assertEqual(deadline(client,service.preferences),at+86400)
        self.assertEqual(client['credits'],50)

    def test_saved_clock_uses_named_timezone_across_daylight_saving_change(self):
        from datetime import datetime, timezone
        before=datetime(2026,3,28,20,24,tzinfo=timezone.utc).timestamp()
        self.assertEqual(next_daily_time('21:24','Europe/Rome',before+1),before+23*3600)
        self.assertEqual(next_daily_time('21:24','Not/AZone',before),0)


if __name__=='__main__': unittest.main()
