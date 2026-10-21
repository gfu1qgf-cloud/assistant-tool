"""Failed progress cannot turn four automatic profiles into dozens of windows."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.browser_slots import (
    request_start, occupied_profiles, can_start, seen, disconnected, launch_result)
from app_plugins.builtin.flow_assistant.credit_cooldown import close_idle_exhausted
from app_plugins.builtin.flow_assistant.page_readiness import watch_preparation
from app_plugins.builtin.flow_assistant.balance_dispatch import prefer_smaller


class BrowserSlotsTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    low = fixtures.CreditCooldownTests.low
    observe = fixtures.CreditCooldownTests.observe

    def configured(self):
        service = self.service()
        profiles = ['Profile ' + str(i) for i in range(1, 41)]
        service.preferences.update(concurrency=4, profiles=profiles,
            profile_projects={p:fixtures.PROJECT for p in profiles})
        return service, profiles

    def test_expired_unconnected_launches_keep_four_slots_across_submissions_and_start(self):
        service, profiles = self.configured(); launches=[];service.launch_requested.connect(launches.append)
        service.store.enqueue(self.jobs(duration=8, count=10, budget=0));service.paused=False
        start=time.time()
        for step in range(20):
            service.execute({'action':'start'})
            with patch('time.time', return_value=start+step*1000): service._tick()
        self.assertEqual(len(launches),4)
        self.assertEqual(len(set(launches)),4)
        self.assertEqual(len(service.preferences['browser_slots']),4)
        self.assertTrue(all(row['status']=='queued' for row in service.store.rows()))

    def test_cold_blocked_and_low_credit_windows_all_count_toward_the_limit(self):
        for kind in ('cold','blocked','low'):
            with self.subTest(kind=kind):
                self.setUp();service,profiles=self.configured()
                for profile in profiles[:4]:
                    client=self.browser(service,profile,points=40)
                    client['connected']=time.time()-86400
                    if kind=='cold':client.update(page_ready=False,credits_local=False,
                        connection_recovery={'state':'stopped','count':3})
                    elif kind=='blocked':client['blocked']='参数核对失败'
                    else:self.low(service,client)
                service.store.enqueue(self.jobs(duration=8,budget=0));service.paused=False
                launches=[];service.launch_requested.connect(launches.append)
                service._tick();service._tick()
                self.assertFalse(launches)
                self.assertFalse(can_start(service,profiles[4]))

    def test_an_unknown_disconnect_or_restart_does_not_release_physical_window_reservations(self):
        service,profiles=self.configured();client=self.browser(service,profiles[0]);seen(service,client)
        disconnected(service,client);service.clients.clear()
        for profile in profiles[1:4]:self.assertTrue(request_start(service,profile))
        self.assertFalse(can_start(service,profiles[4]))
        service.launch_attempts=set();service.launched.clear();service.launching.clear()
        service.preferences=service.store.settings()
        self.assertFalse(can_start(service,profiles[4]),'persisted reservations survive an ordinary restart')
        self.assertTrue(request_start(service,profiles[0]),'the same reserved profile may reconnect once after restart')
        self.assertFalse(request_start(service,profiles[0]))

    def test_definite_process_failure_releases_only_that_slot_and_is_not_retried_on_enqueue(self):
        service,profiles=self.configured()
        self.assertTrue(request_start(service,profiles[0]))
        launch_result(service,{'profile':profiles[0],'opened':False,'error':'launch fixture failed before process'})
        self.assertNotIn(profiles[0],occupied_profiles(service))
        service.launched.clear();self.assertFalse(can_start(service,profiles[0]))
        self.assertTrue(can_start(service,profiles[1]))
        # A late failure cannot release a profile which has already connected.
        client=self.browser(service,profiles[1]);seen(service,client)
        launch_result(service,{'profile':profiles[1],'opened':False})
        self.assertIn(profiles[1],occupied_profiles(service))

    def test_explicit_resume_can_retry_a_definite_launch_failure(self):
        service,profiles=self.configured();request_start(service,profiles[0])
        launch_result(service,{'profile':profiles[0],'opened':False,'error':'fixture could not start'})
        self.assertFalse(can_start(service,profiles[0]))
        service.execute({'action':'resume_browser','keys':['profile:'+profiles[0]]})
        self.assertTrue(can_start(service,profiles[0]))

    def test_pause_during_launch_probe_cancels_before_any_window_is_opened(self):
        dialog,launcher,commands=fixtures.setup_fixtures.SetupTests.ui(self)
        dialog.plugin.snapshot={'paused':True,'released':[], 'preferences':{
            'browser_slots':{'Profile 23':{'state':'opening'}}, 'profile_projects':{'Profile 23':fixtures.PROJECT}}}
        dialog.plugin._launch_checked({'profile':'Profile 23','status':'enabled',
            'version':dialog.plugin.snapshot.get('extension_version','1.1.26')})
        launcher.launch_profile.assert_not_called()
        self.assertTrue(commands[-1]['cancelled'])
        service,profiles=self.configured();request_start(service,profiles[0])
        launch_result(service,{'profile':profiles[0],'opened':False,'cancelled':True})
        self.assertTrue(can_start(service,profiles[0]))
        self.assertNotIn(profiles[0],service.preferences.get('browser_launch_failures',{}))

    def test_low_credit_closing_keeps_slot_until_disconnect_or_final_close_receipt(self):
        service,profiles=self.configured();client=self.browser(service,profiles[0],close=True);seen(service,client)
        self.low(service,client);client['credit_check']['at']-=31
        for profile in profiles[1:4]:self.assertTrue(request_start(service,profile))
        self.assertTrue(close_idle_exhausted(service))
        key=next(k for k,p in service.pending.items() if p['action']=='close_exhausted')
        service._response(client,{'request_id':key,'success':True,'data':{'closing':True}})
        self.assertIn(key,service.pending)
        self.assertFalse(can_start(service,profiles[4]))
        service._response(client,{'request_id':key,'success':False,'error':'native close failed'})
        self.assertNotIn(key,service.pending)
        self.assertEqual(client['credit_close']['state'],'failed')
        self.assertFalse(can_start(service,profiles[4]))
        client['credit_close']['next_at']=0
        self.assertTrue(close_idle_exhausted(service))
        key=next(k for k,p in service.pending.items() if p['action']=='close_exhausted')
        service._response(client,{'request_id':key,'success':True,'data':{'closed':True}})
        self.assertTrue(can_start(service,profiles[4]))
        self.assertFalse(close_idle_exhausted(service),'a background extension surviving window closure must not close/reopen repeatedly')

    def test_explicit_successful_idle_close_disconnect_releases_the_slot(self):
        service,profiles=self.configured();client=self.browser(service,profiles[0]);seen(service,client)
        for profile in profiles[1:4]:request_start(service,profile)
        client['credit_close']={'state':'closing'}
        service.clients.clear();disconnected(service,client)
        self.assertTrue(can_start(service,profiles[4]))

    def test_stale_page_readiness_does_not_prevent_the_extension_from_checking_low_credit_close(self):
        service,profiles=self.configured();client=self.browser(service,profiles[0],close=True)
        self.low(service,client);client['credit_check']['at']-=31
        client.update(page_ready=False,batch_running=True)
        self.assertTrue(close_idle_exhausted(service),'the extension must independently verify live idle/native/download state')
        self.assertEqual([p['action'] for p in service.pending.values()],['close_exhausted'])

    def test_small_balance_probe_cannot_bypass_full_browser_capacity(self):
        service,profiles=self.configured()
        rich=self.browser(service,profiles[0],points=1300)
        for profile in profiles[1:4]:self.browser(service,profile,points=1000)
        service.store.enqueue(self.jobs(duration=8,budget=0));row=service.store.rows()[0]
        launches=[];service.launch_requested.connect(launches.append)
        self.assertFalse(prefer_smaller(service,row,rich),'a blocked backup probe must allow funded work in an already open window')
        self.assertFalse(launches)
        self.assertFalse(can_start(service,profiles[4]))

    def test_preparation_recovery_repairs_an_online_profile_without_launching_chrome_again(self):
        service,profiles=self.configured();client=self.browser(service,profiles[0])
        service.paused=False
        client['page_ready']=False;client['connected']=time.time()-100
        service.store.enqueue(self.jobs(duration=8,budget=0));row=service.store.rows()[0]
        service.store.update(row['id'],'queued',remote={'prepare_retry':{'reopen':True,'profile':profiles[0],'next_at':0}})
        launches=[];service.launch_requested.connect(launches.append)
        self.assertTrue(watch_preparation(service));self.assertFalse(launches)
        self.assertTrue(any(p['action']=='repair_project' for p in service.pending.values()))

    def test_pause_stops_automatic_preparation_reopening(self):
        service,profiles=self.configured();service.store.enqueue(self.jobs(duration=8,budget=0))
        row=service.store.rows()[0]
        service.store.update(row['id'],'queued',remote={'prepare_retry':{'reopen':True,'profile':profiles[0],'next_at':0}})
        self.assertFalse(watch_preparation(service))
        self.assertFalse(service.preferences.get('browser_slots'))

    def test_upload_timeout_covers_reuse_fallback_without_retrying_the_live_original_operation(self):
        from app_plugins.builtin.flow_assistant.timing import request_timeout
        from app_plugins.builtin.flow_assistant.transfer_policy import retry_info
        service,profiles=self.configured();client=self.browser(service,profiles[0]);service.paused=False
        service.store.enqueue(self.jobs(duration=8,budget=0));row=service.store.rows()[0]
        service.store.update(row['id'],'uploading',attempt='a'*32,browser=client['id'],remote={
            'account':client['account'],'profile':client['profile'],'cost':12})
        service.request(client,'upload_images',job=row['id'])
        key=next(k for k,p in service.pending.items() if p['action']=='upload_images')
        timeout=request_timeout(service.preferences,'upload_images',client)
        self.assertGreater(timeout,120)
        service.pending[key]['at']=time.time()-125
        service._tick()
        self.assertIn(key,service.pending,'the old 120s boundary must not retire a still valid upload')
        service.pending[key]['at']=time.time()-timeout-1
        service._tick()
        self.assertNotIn(key,service.pending)
        self.assertTrue(retry_info(service.store.get(row['id']),'upload').get('next_at'))


if __name__=='__main__':unittest.main()
