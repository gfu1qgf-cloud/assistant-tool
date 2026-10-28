"""Real coordinator recovery with synthetic files, sockets and SQLite only."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_browser_lifecycle as fixtures
from app_plugins.builtin.flow_assistant import scheduler_recovery as recovery
from app_plugins.builtin.flow_assistant import browser_lifecycle as lifecycle,browser_budget,browser_slots
from app_plugins.builtin.flow_assistant.runtime_close import PAUSED


class StallRecoveryTests(unittest.TestCase):
    setUpClass=fixtures.BrowserLifecycleTests.__dict__['setUpClass']
    setUp=fixtures.BrowserLifecycleTests.setUp
    service=fixtures.BrowserLifecycleTests.service
    browser=fixtures.BrowserLifecycleTests.browser
    client=fixtures.BrowserLifecycleTests.client
    jobs=fixtures.BrowserLifecycleTests.jobs
    ready=fixtures.BrowserLifecycleTests.ready
    messages=fixtures.BrowserLifecycleTests.messages

    def failed_upload(self,service,client,**extra):
        service.store.enqueue(self.jobs(duration=8,budget=0))
        row=service.store.rows()[-1]
        service.store.update(row['id'],'failed',attempt='a'*32,browser=client['id'],remote={
            'profile':client['profile'],'account':client['account'],'transfer_retries':{'upload':{
                'count':5,'stopped':True,'last_error':'此图此前已发起上传，本次仅核对原素材，未重复拖放：图片上传达到最长等待时间，尚未确认本次图片的素材编号',**extra}}})
        return service.store.get(row['id'])

    def test_window_cap_is_configurable_and_reservations_stay_bounded(self):
        service=self.service();service.timer.stop()
        service.execute({'action':'preferences','value':{'concurrency':4,'browser_window_limit':12}})
        self.assertEqual(browser_budget.maximum(service),12)
        start=time.time()
        for i in range(20):
            with patch('time.time',return_value=start+100*i):browser_slots.request_start(service,'Config '+str(i))
        self.assertEqual(len(service.preferences['browser_slots']),12)
        for bad in (0,3,65,True,'12'):
            service.execute({'action':'preferences','value':{'browser_window_limit':bad}})
            self.assertEqual(browser_budget.maximum(service),12)
        self.assertEqual(service.store.settings()['browser_window_limit'],12)

    def test_ten_credits_remain_open_for_six_seconds(self):
        service=self.service();service.timer.stop();service.paused=False
        client=self.ready(service,points=10);service.store.enqueue(self.jobs(duration=6,budget=0))
        self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)
        service._tick();self.assertEqual(service.store.rows()[0]['browser'],client['id'])

    def test_ten_credits_close_for_unaffordable_fifo_head_without_erasing_balance(self):
        service=self.service();service.timer.stop();service.paused=False
        client=self.ready(service,points=10);service.store.enqueue(self.jobs(duration=8,budget=0))
        self.assertTrue(lifecycle.watch(service))
        self.assertEqual(client['runtime_close']['automatic_reason'],'idle_capacity')
        self.assertEqual(client['credits'],10)
        self.assertEqual(service.store.rows()[0]['status'],'queued')
        self.assertNotEqual(service.preferences.get('browser_blocks',{}).get('profile:'+client['profile']),PAUSED)

    def test_ten_credit_close_respects_user_option_and_paid_work(self):
        service=self.service();service.timer.stop();service.paused=False
        client=self.ready(service,points=10);service.store.enqueue(self.jobs(duration=8,budget=0))
        service.preferences['browser_options']={'auto_close_low_credit':False}
        self.assertFalse(lifecycle.watch(service))
        service.preferences['browser_options']={'auto_close_low_credit':True}
        row=service.store.rows()[0]
        service.store.update(row['id'],'generating',attempt='a'*32,browser=client['id'],remote={'profile':client['profile']})
        self.assertFalse(lifecycle.watch(service))

    def test_new_verified_connection_rearms_old_cooldown_without_freeing_live_slot(self):
        service=self.service();service.timer.stop();client=self.ready(service)
        browser_slots.seen(service,client);profile=client['profile']
        service.preferences.update(browser_blocks={'profile:'+profile:lifecycle.AUTO_CLOSING},browser_lifecycle={
            profile:{'state':'cooldown','at':time.time()-60,'kind':'page_fault','reopen_at':time.time()+1800}})
        client['runtime_close']={'state':'closed'}
        self.assertTrue(lifecycle.watch(service))
        self.assertEqual(service.preferences['browser_lifecycle'][profile]['state'],'eligible')
        self.assertNotIn('profile:'+profile,service.preferences['browser_blocks'])
        self.assertIn(profile,browser_budget.profiles(service));self.assertNotIn('runtime_close',client)

    def test_manual_disabled_browser_never_rearms(self):
        service=self.service();service.timer.stop();client=self.ready(service);profile=client['profile']
        service.preferences.update(browser_blocks={'profile:'+profile:PAUSED},browser_lifecycle={
            profile:{'state':'cooldown','at':time.time()-60,'kind':'page_fault','reopen_at':time.time()+1800}})
        self.assertFalse(lifecycle.watch(service));self.assertEqual(service.preferences['browser_blocks']['profile:'+profile],PAUSED)

    def test_stopped_unpaid_upload_is_requeued_but_paid_and_permanent_failures_are_retained(self):
        service=self.service();service.timer.stop();one=self.ready(service)
        unpaid=self.failed_upload(service,one)
        two=self.ready(service,'Paid');paid=self.failed_upload(service,two)
        service.store.update(paid['id'],'failed',remote={**paid['remote'],'generation_response':{'taskId':'paid-original'}})
        three=self.ready(service,'Broken');permanent=self.failed_upload(service,three,permanent=True)
        before=copy.deepcopy(service.store.get(paid['id']))
        recovery.recover(service)
        self.assertEqual(service.store.get(unpaid['id'])['status'],'queued')
        self.assertEqual(service.store.get(unpaid['id'])['payload'],unpaid['payload'])
        self.assertEqual(service.store.get(paid['id']),before)
        self.assertEqual(service.store.get(permanent['id'])['status'],'failed')

    def test_strong_retry_preserves_manual_marks_credit_deadlines_and_window_reservations(self):
        service=self.service();service.timer.stop();manual=self.ready(service,'Manual')
        low=self.ready(service,'Cooling',points=5)
        # The ordinary observation creates the real credit policy records.
        from test_flow_credit_cooldown import CreditCooldownTests
        CreditCooldownTests.observe(self,service,low,5)
        service.preferences.setdefault('browser_blocks',{})['profile:'+manual['profile']]=PAUSED
        browser_slots.seen(service,manual);browser_slots.seen(service,low)
        before=copy.deepcopy({k:service.preferences.get(k) for k in ('browser_blocks','credit_cooldowns','browser_slots')})
        recovery.recover(service)
        self.assertEqual({k:service.preferences.get(k) for k in before},before)

    def test_watch_retries_twice_then_notifies_once_and_recovers_on_real_progress(self):
        service=self.service();service.timer.stop();service.paused=False
        client=self.ready(service);service.store.enqueue(self.jobs(duration=8,budget=0))
        notices=[];service.scheduler_attention.connect(notices.append);start=time.time()
        with patch('time.time',return_value=start):self.assertFalse(recovery.watch(service))
        with patch('time.time',return_value=start+31):self.assertTrue(recovery.watch(service))
        self.assertEqual(service.scheduler_recovery['round'],1);self.assertFalse(notices)
        with patch('time.time',return_value=start+77):recovery.watch(service)
        self.assertEqual(service.scheduler_recovery['round'],2);self.assertFalse(notices)
        with patch('time.time',return_value=start+123):recovery.watch(service)
        with patch('time.time',return_value=start+1000):recovery.watch(service)
        self.assertEqual(len(notices),1);self.assertIn(client['profile'],notices[0]['details'])
        row=service.store.rows()[0];service.store.update(row['id'],'generating',attempt='b'*32,browser=client['id'])
        with patch('time.time',return_value=start+1001):recovery.watch(service)
        self.assertFalse(service.scheduler_recovery)

    def test_heartbeats_and_preparation_status_changes_do_not_reset_retry_budget(self):
        service=self.service();service.timer.stop();service.paused=False;client=self.ready(service)
        service.store.enqueue(self.jobs(count=2,duration=8,budget=0));start=time.time()
        with patch('time.time',return_value=start):recovery.watch(service)
        with patch('time.time',return_value=start+31):recovery.watch(service)
        row=service.store.rows()[0];service.store.update(row['id'],'preparing',attempt='c'*32,browser=client['id'])
        with patch('time.time',return_value=start+35):recovery.watch(service)
        self.assertEqual(service.scheduler_recovery['round'],1)

    def test_offline_and_user_pause_never_trigger_automatic_page_operations(self):
        service=self.service();service.timer.stop();client=self.ready(service)
        service.store.enqueue(self.jobs(count=2,duration=8,budget=0))
        service.scheduler_recovery={'round':0,'next_at':0}
        with patch('model.OfflineMode.is_offline',return_value=True):
            self.assertTrue(recovery.watch(service))
        self.assertFalse(service.pending);self.assertFalse(service.scheduler_recovery)
        service.scheduler_recovery={'round':0,'next_at':0}
        self.assertTrue(recovery.watch(service));self.assertFalse(service.pending)
