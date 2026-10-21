"""Spend recoverable existing accounts before admitting another Chrome profile."""
import time
import unittest
from unittest.mock import patch
import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.browser_slots import request_start
from app_plugins.builtin.flow_assistant.browser_reuse import before_new_browser


class CautiousLaunchTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser
    jobs=fixtures.CreditCooldownTests.jobs
    observe=fixtures.CreditCooldownTests.observe
    low=fixtures.CreditCooldownTests.low

    def queue(self, *, points=50):
        service=self.service();client=self.browser(service,'Existing',points=points)
        client.update(connected=time.time()-3600,capabilities=['runtime_controls_v1'])
        service.preferences.update(concurrency=4,profiles=['Existing','Backup'],
            profile_projects={'Existing':fixtures.PROJECT,'Backup':fixtures.PROJECT})
        service.store.enqueue(self.jobs(duration=8,count=4,budget=0));service.paused=False
        launches=[];service.launch_requested.connect(launches.append)
        return service,client,launches

    def test_repairing_funded_window_does_not_open_three_extra_windows(self):
        service,client,launches=self.queue();client['page_ready']=False
        service._tick();service._tick()
        self.assertTrue(any(p['action']=='repair_project' for p in service.pending.values()))
        self.assertFalse(launches)

    def test_pending_credit_read_blocks_new_profiles_even_with_spare_parallel_lanes(self):
        service,client,launches=self.queue();client['credits_at']-=21601
        service._tick();self.assertFalse(launches)
        self.assertTrue(any(p['action']=='get_points' for p in service.pending.values()))

    def test_normal_inflight_funded_account_is_reused_instead_of_filling_with_new_profiles(self):
        service,client,launches=self.queue();service._tick()
        first=service.store.rows()[0];service.store.update(first['id'],'generating')
        service.pending.clear();service._tick();self.assertFalse(launches)
        service.store.update(first['id'],'completed');service._tick()
        self.assertEqual(service.store.rows()[1]['browser'],client['id'])

    def test_direct_scheduling_launch_also_obeys_existing_browser_gate(self):
        service,client,launches=self.queue();client['native_busy']=True
        self.assertFalse(request_start(service,'Backup',service.store.rows()[0]['payload']))
        self.assertFalse(launches)

    def test_transient_failed_credit_read_is_retried_before_a_backup(self):
        service,client,launches=self.queue();client['credits_at']-=21601;service._tick()
        key=next(k for k,p in service.pending.items() if p['action']=='get_points')
        service._response(client,{'request_id':key,'success':False,'error':'temporary network failure'})
        service._tick();self.assertFalse(launches)
        with patch('time.time',return_value=time.time()+10):service._tick()
        self.assertTrue(any(p['action']=='get_points' for p in service.pending.values()))

    def test_four_existing_accounts_still_fill_four_lanes(self):
        service,client,launches=self.queue()
        for n in range(3):self.browser(service,'Existing '+str(n),points=50)
        for _ in range(4):service._tick()
        self.assertEqual(sum(r['status']=='preparing' for r in service.store.rows()),4)
        self.assertFalse(launches)

    def test_unusable_existing_accounts_allow_a_backup_without_resetting_holds(self):
        for kind in ('low','insufficient','high','blocked','fixed_lane','account_ban'):
            with self.subTest(kind=kind):
                self.setUp();service,client,launches=self.queue()
                if kind=='low':self.low(service,client,5)
                elif kind=='insufficient':client['credits']=10  # Head costs 12, so this is not a useful spare.
                elif kind=='high':client['credits']=1300
                elif kind=='blocked':client['blocked']='explicit failure requiring user correction'
                elif kind=='fixed_lane':service.preferences.update(profile_lanes={'Existing':6},profile_lane_modes={'Existing':'fixed'})
                else:service.preferences['account_blocks']={client['account']:{'kind':'disabled','reason':'test ban'}}
                service._tick();self.assertEqual(launches,['Backup'])
                self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))

    def test_failed_page_recovery_releases_only_after_the_configured_limit(self):
        service,client,launches=self.queue();client['page_ready']=False
        limit=service.preferences.get('timing',{}).get('connection_repair_attempts',3)
        service.connection_recoveries['Existing']={'count':limit,'next_at':0}
        service._tick();self.assertEqual(launches,['Backup'])
        self.assertEqual(client['reuse_recovery']['state'],'stopped')

    def test_unowned_native_busy_wait_is_bounded_without_refreshing_an_unknown_operation(self):
        service,client,launches=self.queue();client['native_busy']=True;service._tick()
        self.assertFalse(launches);since=client['reuse_recovery']['since']
        with patch('time.time',return_value=since+91):service._tick()
        self.assertEqual(launches,['Backup'])
        self.assertFalse(any(p['action']=='repair_project' for p in service.pending.values()))

    def test_credit_retries_use_one_f5_then_eventually_admit_backup(self):
        service,client,launches=self.queue();client['credits_at']-=21601
        now=time.time()
        for attempt in range(6):
            with patch('time.time',return_value=now+attempt*10):
                service._tick()
                key=next(k for k,p in service.pending.items() if p['action']=='get_points')
                service._response(client,{'request_id':key,'success':False,'error':'temporary read failure'})
                service._tick()
                if attempt==2:
                    repairs=[k for k,p in service.pending.items() if p['action']=='repair_project']
                    self.assertEqual(len(repairs),1);self.assertFalse(launches)
                    service._response(client,{'request_id':repairs[0],'success':True,'data':{'reloaded':True}})
                    service._snapshot(client,{'url':fixtures.PROJECT,'controls_ready':True,'running':False,'tasks':[]})
                elif attempt<5:self.assertFalse(launches)
        self.assertEqual(launches,['Backup'])
        self.assertTrue(client['reuse_recovery']['refreshed'])
        self.assertEqual(client['reuse_recovery']['state'],'stopped')

    def test_waiting_for_a_paid_result_never_refreshes_or_resubmits_it(self):
        service,client,launches=self.queue();service._tick()
        row=service.store.rows()[0];service.store.update(row['id'],'generating');service.pending.clear()
        before=service.store.get(row['id']);service._tick()
        self.assertFalse(launches);self.assertEqual(service.store.get(row['id']),before)
        self.assertFalse(any(p['action'] in {'repair_project','run_tasks'} for p in service.pending.values()))

    def test_manual_credit_check_resets_exhausted_reuse_state(self):
        service,client,launches=self.queue();client['reuse_recovery']={'state':'stopped','since':time.time(),'credit_count':3}
        service.request(client,'get_points',manual_credit=True)
        self.assertNotIn('reuse_recovery',client)

    def test_automatic_connection_confirmation_waits_before_opening_more(self):
        service,client,launches=self.queue()
        client.update(setup_pending=True,setup_blocked='正在自动确认原项目',page_ready=False,connected=time.time())
        service._tick();self.assertFalse(launches)
        self.assertTrue(before_new_browser(service,service.store.rows()[0]['payload']))

    def test_pause_message_is_not_overridden_by_the_previous_reuse_wait(self):
        service,client,launches=self.queue();client['native_busy']=True;service._tick()
        events=[];service.changed.connect(events.append);service.execute({'action':'pause'})
        self.assertIn('队列已暂停',events[-1]['jobs'][0]['wait_reason'])

    def test_new_start_is_confirmed_before_queueing_more_profile_launches(self):
        service,client,launches=self.queue();service.clients.clear()
        service.preferences['profiles']=['Existing','Backup','Third','Fourth']
        service.preferences['profile_projects'].update(Third=fixtures.PROJECT,Fourth=fixtures.PROJECT)
        for _ in range(8):service._tick()
        self.assertEqual(launches,['Existing'])
        self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))

    def test_explicit_resume_resets_reuse_and_page_repair_without_releasing_low_credit_hold(self):
        service,client,launches=self.queue();self.low(service,client,5)
        client['reuse_recovery']={'state':'stopped','since':time.time(),'credit_count':3}
        service.connection_recoveries['Existing']={'count':3,'state':'stopped'}
        service.execute({'action':'resume_browser','keys':['profile:Existing']})
        self.assertNotIn('reuse_recovery',client)
        self.assertNotIn('Existing',service.connection_recoveries)
        from app_plugins.builtin.flow_assistant.credit_cooldown import credit_hold
        self.assertTrue(credit_hold(client,service.preferences))

    def test_stale_legacy_insufficient_flag_waits_for_actual_read_without_overriding_cooldown(self):
        service,client,launches=self.queue(points=0)
        client.update(credits_at=time.time()-21601,blocked='积分不足')
        service._tick();self.assertFalse(launches)
        key=next(k for k,p in service.pending.items() if p['action']=='get_points')
        service._response(client,{'request_id':key,'success':True,'data':{'email':client['account'],'remaining_points':50}})
        service._tick();self.assertEqual(service.store.rows()[0]['browser'],client['id'])
        self.assertFalse(launches)

    def test_missing_original_window_retry_uses_ready_existing_account_without_reopening(self):
        service,client,launches=self.queue()
        service.preferences['profiles'].append('Legacy')
        service.preferences['profile_projects']['Legacy']=fixtures.PROJECT
        row=service.store.rows()[0]
        service.store.update(row['id'],'queued',remote={'prepare_retry':{'reopen':True,'profile':'Legacy','next_at':0}})
        service._tick()
        self.assertFalse(launches)
        self.assertEqual(service.store.get(row['id'])['browser'],client['id'])


if __name__=='__main__':unittest.main()
