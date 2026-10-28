"""Offline pack optimization, per-send charging, FIFO fallback and lock exits."""
import copy
import itertools
import time
import unittest
from unittest.mock import patch

import test_flow_transfer_recovery as fixtures
from flow_reference_fixtures import upload_receipt
from app_plugins.builtin.flow_assistant import credit_packs as packs, ui_mutex
from app_plugins.builtin.flow_assistant.browser_options import normalize
from app_plugins.builtin.flow_assistant.policy import admitted_queue
from app_plugins.builtin.flow_assistant.ordered_dispatch import queue_head


class PackTests(unittest.TestCase):
    setUpClass = fixtures.TransferTests.__dict__['setUpClass']
    setUp = fixtures.TransferTests.setUp
    service = fixtures.TransferTests.service
    jobs = fixtures.TransferTests.jobs
    client = fixtures.TransferTests.client
    actions = fixtures.TransferTests.actions

    def setup_pack(self, durations=(10, 10, 6, 6), balance=50):
        service = self.service()
        jobs = self.jobs(count=len(durations), budget=0)
        for job, duration in zip(jobs, durations):job['duration'] = duration
        service.store.enqueue(jobs)
        client = self.client(service, credits=balance)
        client.update(capabilities=['credit_pack_submit_v1','ui_mutex_v1'], credit_pack_submit=True,
                      page_ready=True, page_at=time.time(), connected=time.time(), credits_local=True, reference_sha256_names=True,
                      reference_stable_sha256_names=True)
        service.preferences['profile_projects'] = {client['profile']:client['saved_project']}
        service.preferences['concurrency'] = 1
        service.paused = False
        return service, client

    def dispatch(self, service):
        rows = service.store.rows(); batches = service.store.batches()
        admitted = admitted_queue(rows,batches,service.observed_costs,paused=service.paused,released=service.released)
        return packs.dispatch(service,rows,batches,admitted)

    def finish_submit(self, service, client):
        request = next(k for k,p in service.pending.items() if p['action']=='prepare')
        job_id = service.pending[request]['job']
        service._response(client, {'request_id':request,'success':True,'data':{
            'verified':True,'model':'omni_1_1_flash','email':client['account']}})
        upload = next(k for k,p in service.pending.items() if p['action']=='upload_images')
        service._response(client,{'request_id':upload,'success':True,'data':upload_receipt(service,job_id)})
        run = next(k for k,p in service.pending.items() if p['action']=='run_tasks')
        row = service.store.get(job_id)
        binding = {'schema':2,'source':'generation_response','taskId':row['attempt'],'submissionId':'sub-'+row['attempt'],
                   'mediaIds':['media-'+row['attempt']],'readyIds':[]}
        service.store.update(job_id,'submitting',remote={**row['remote'],'upstream_task':row['attempt'],'bindings':[binding]})
        service._response(client,{'request_id':run,'success':True,'data':{'packed':True}})
        return service.store.get(job_id)

    def test_optimum_mixed_pack_spends_50_and_respects_same_image(self):
        service,client = self.setup_pack((10,10,10,8,8,8,8,6,6))
        rows = service.store.rows()
        chosen = packs.optimize(rows,50,service.store.batches(),{},anchor=rows[0],client=client,preferences=service.preferences)
        self.assertEqual([r['payload']['duration'] for r in chosen],[10,10,6,6])
        self.assertEqual([r['id'] for r in chosen],[rows[i]['id'] for i in (0,1,7,8)])
        other = copy.deepcopy(rows[-1]);other['payload']['image_identity']['sha256']='b'*64
        chosen = packs.optimize(rows[:3]+[other],50,service.store.batches(),{},anchor=rows[0],client=client,preferences=service.preferences)
        self.assertEqual(len(chosen),3)

    def test_dp_matches_bruteforce_with_remaining_batch_budget(self):
        service,client = self.setup_pack((10,8,6,8,10,6))
        rows=service.store.rows();key=rows[0]['payload']['batch_id']
        for balance,budget in itertools.product((10,12,15,22,25,30,37,48,50),(0,24,36,50)):
            batches={key:{'limit_total':budget,'reserved':0}}
            chosen=packs.optimize(rows,balance,batches,{},anchor=rows[0],client=client,preferences=service.preferences)
            costs=[{6:10,8:12,10:15}[r['payload']['duration']] for r in rows]
            candidates=[(sum(costs[i] for i in ids),ids) for n in range(7) for ids in itertools.combinations(range(6),n)
                        if sum(costs[i] for i in ids)<=min(balance,budget or 50)]
            best=min(candidates,key=lambda item:(-item[0],item[1]))
            self.assertEqual([r['id'] for r in chosen],[rows[i]['id'] for i in best[1]])

    def test_multiple_user_batches_have_independent_budget_limits(self):
        service,client=self.setup_pack((10,10,6,6))
        rows=service.store.rows()
        for row in rows[2:]:row['payload']['batch_id']='other'
        batches={rows[0]['payload']['batch_id']:{'limit_total':15,'reserved':0},'other':{'limit_total':20,'reserved':10}}
        chosen=packs.optimize(rows,50,batches,{},anchor=rows[0],client=client,preferences=service.preferences)
        self.assertEqual([r['payload']['duration'] for r in chosen],[10,6])

    def test_package_does_not_charge_until_each_submission_and_cloud_wait_is_overlapped(self):
        service,client=self.setup_pack()
        self.assertTrue(self.dispatch(service))
        self.assertEqual(service.store.batches()[service.store.rows()[0]['payload']['batch_id']]['reserved'],0)
        first=self.finish_submit(service,client)
        self.assertEqual(first['status'],'generating');self.assertEqual(client['credits'],35)
        self.assertFalse(self.dispatch(service),'random gap must be enforced')
        with patch('time.time',return_value=first['remote']['pack_next_at']+.01):
            self.assertTrue(self.dispatch(service))
        self.assertEqual(packs.active_count(service.store.rows()),1)
        self.assertEqual(len([r for r in service.store.rows() if r['status']=='generating']),1)
        with patch('time.time',return_value=first['remote']['pack_next_at']+.02):
            second=self.finish_submit(service,client)
        self.assertEqual(client['credits'],20)
        for _ in range(2):
            with patch('time.time',return_value=second['remote']['pack_next_at']+.01):
                self.assertTrue(self.dispatch(service))
                second=self.finish_submit(service,client)
        self.assertEqual(client['credits'],0)
        self.assertEqual([r['status'] for r in service.store.rows()],['generating']*4)
        self.assertEqual(len({r['attempt'] for r in service.store.rows()}),4)
        runs=[a for a in self.actions(client) if a.get('action')=='run_tasks']
        self.assertEqual(len(runs),4)
        self.assertTrue(all(r['params']['packed'] and not r['params']['clear_existing'] for r in runs))
        self.assertFalse(self.dispatch(service))

    def test_unconfirmed_media_does_not_advance_or_hold_unsent_members_forever(self):
        service,client=self.setup_pack();self.dispatch(service)
        first=service.store.rows()[0]
        service.pending.clear()
        service.store.update(first['id'],'generating',remote={**first['remote'],'activity_at':time.time()})
        self.assertFalse(self.dispatch(service))
        with patch('time.time',return_value=time.time()+21):self.dispatch(service)
        self.assertTrue(all(not r['remote'].get('credit_pack') for r in service.store.rows()[1:]))
        self.assertEqual(service.store.get(first['id'])['status'],'generating')

    def test_live_capability_and_default_switch_then_off_restores_fifo(self):
        self.assertTrue(normalize({})['credit_packs'])
        service,client=self.setup_pack();client['credit_pack_submit']=False
        self.assertFalse(self.dispatch(service));client['credit_pack_submit']=True
        self.assertTrue(self.dispatch(service))
        service.preferences['browser_options']={'credit_packs':False}
        self.assertFalse(self.dispatch(service))
        self.assertEqual(queue_head(service,service.store.rows(),service.store.batches())[0]['id'],service.store.rows()[1]['id'])
        self.assertTrue(all(not r['remote'].get('credit_pack') for r in service.store.rows()[1:]))

    def test_actual_balance_budget_and_manual_fixed_lane_are_respected(self):
        service,client=self.setup_pack((10,8,6,6),balance=22);rows=service.store.rows()
        chosen=packs.optimize(rows,22,{}, {},anchor=rows[0],client=client,preferences=service.preferences)
        self.assertEqual([r['payload']['duration'] for r in chosen],[8,6])
        prefs={**service.preferences,'auto_lane':False,'profile_lanes':{client['profile']:10}}
        self.assertEqual(len(packs.optimize(rows,50,{}, {},anchor=rows[0],client=client,preferences=prefs)),1)
        self.assertFalse(packs.optimize(rows,None,{}, {},anchor=rows[0],client=client,preferences=prefs))

    def test_interval_randomizes_within_one_to_three_seconds(self):
        service,client=self.setup_pack();self.dispatch(service);row=service.store.rows()[0]
        for random_value,expected in [(0,1),(2,3)]:
            with patch('random.uniform',return_value=random_value),patch('time.time',return_value=123):
                remote=packs.submitted(service,row,{'packed':True})
            self.assertEqual(remote['pack_next_at'],123+expected)

    def test_pre_submit_lock_retains_pack_without_starting_submit_clock(self):
        service,client=self.setup_pack();service.preferences['browser_options']={'ui_mutex':True}
        self.dispatch(service);now=time.time()
        ui_mutex.receive(service,client,{'type':'ui_mutex_request','token':'a'*32,'operation':'prepare','url':client['saved_project']})
        state=ui_mutex.state(service);self.assertEqual(state['owner']['client'],client['id'])
        ui_mutex.host_receive(service,{'action':'ui_native_request','token':'b'*32,'profile':'Other'})
        with patch('time.time',return_value=now+46):ui_mutex.watch(service)
        self.assertEqual(state['owner']['client'],client['id'])
        self.assertIsNone(state['burst']['started_at'])
        self.assertEqual(state['burst']['elapsed'],0)
        self.assertTrue(all(r['remote'].get('credit_pack') for r in service.store.rows()))

    def test_snapshot_keeps_each_pack_output_bound_to_its_attempt(self):
        service,client=self.setup_pack((10,10));rows=service.store.rows()
        for index,row in enumerate(rows):
            service.store.update(row['id'],'generating',attempt=str(index+1)*32,browser=client['id'],remote={'account':client['account']})
        tasks=[]
        for row in service.store.rows():
            tasks.append({'taskId':row['attempt'],'download_path':service.download_name(row),'status':['generating'],
                'mediaBindings':[{'schema':2,'source':'generation_response','taskId':row['attempt'],'submissionId':'s'+row['attempt'],
                                  'mediaIds':['m'+row['attempt']],'readyIds':[]}]})
        service._snapshot(client,{'tasks':tasks,'running':False,'credit_pack_submit':True,'url':client['saved_project']})
        for row in service.store.rows():
            self.assertEqual(row['remote']['bindings'][0]['mediaIds'],['m'+row['attempt']])
            self.assertEqual(row['remote']['upstream_task'],row['attempt'])

    def test_tick_continues_a_pack_at_full_account_capacity(self):
        service,client=self.setup_pack();service._tick()
        self.assertTrue(service.store.rows()[0]['remote'].get('credit_pack'))
        first=self.finish_submit(service,client)
        with patch('time.time',return_value=first['remote']['pack_next_at']+.01):service._tick()
        self.assertEqual(len([r for r in service.store.rows() if r['status']=='preparing']),1)
        self.assertEqual(packs.active_count(service.store.rows()),1)

    def test_ui_burst_ends_when_only_cloud_work_remains_or_switch_is_off(self):
        service,client=self.setup_pack((10,));service.preferences['browser_options']={'ui_mutex':True}
        self.dispatch(service)
        ui_mutex.receive(service,client,{'type':'ui_mutex_request','token':'a'*32,'operation':'prepare','url':client['saved_project']})
        state=ui_mutex.state(service);self.assertIn('burst',state)
        row=service.store.rows()[0]
        service.store.update(row['id'],'generating');service.pending.clear()
        ui_mutex.watch(service)
        self.assertNotIn('burst',state);self.assertIsNone(state['owner'])

    def test_settings_pack_switch_saves_and_cancel_preserves_it(self):
        from app_plugins.builtin.flow_assistant.browser_options_ui import BrowserOptionsSettings
        from app_plugins.builtin.flow_assistant.browser_options import CONFIG_KEY
        panel=BrowserOptionsSettings();self.addCleanup(panel.close)
        self.assertTrue(panel.credit_packs.isChecked())
        panel.credit_packs.setChecked(False);config={};panel.update_config(config)
        self.assertFalse(config[CONFIG_KEY]['credit_packs'])
        panel.load_config({CONFIG_KEY:{'credit_packs':True}})
        self.assertTrue(panel.credit_packs.isChecked())


if __name__=='__main__':unittest.main()
