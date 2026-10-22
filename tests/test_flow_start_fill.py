"""Immediate nonblocking FIFO admission, with one recovery reservation per lane."""
import copy
import json
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.store import TRANSIENT
from app_plugins.builtin.flow_assistant.image_affinity import remember_owner


class StartFillTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    jobs=fixtures.CreditCooldownTests.jobs
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser

    def queued(self,count=8):
        service=self.service();service.timer.stop()
        service.preferences.update(concurrency=4,timing={'queue_tick_seconds':600})
        service.store.enqueue(self.jobs(duration=8,count=count,budget=0))
        return service

    def backups(self,service,count=5):
        service.preferences['profiles']=['Backup '+str(n) for n in range(count)]
        service.preferences.setdefault('profile_projects',{}).update({p:fixtures.PROJECT for p in service.preferences['profiles']})
        launches=[];service.launch_requested.connect(launches.append)
        return launches

    def process(self):
        self.app.processEvents()

    def test_start_fills_four_ready_browsers_in_one_wakeup_without_waiting_for_checks(self):
        service=self.queued();clients=[self.browser(service,'P'+str(n)) for n in range(4)]
        service.preferences['profiles']=[c['profile'] for c in clients]
        service.execute({'action':'start'})
        self.assertTrue(service.dispatch_timer.isActive())
        self.process()
        rows=service.store.rows()
        self.assertEqual([r['status'] for r in rows],['preparing']*4+['queued']*4)
        self.assertEqual([r['browser'] for r in rows[:4]],[c['id'] for c in clients])
        self.assertEqual(sum(p['action']=='prepare' for p in service.pending.values()),4)
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def test_one_hung_preflight_does_not_hold_the_other_three_slots(self):
        service=self.queued();clients=[self.browser(service,'P'+str(n)) for n in range(4)]
        row=service.store.rows()[0]
        service.store.update(row['id'],'preparing',attempt='a'*32,browser=clients[0]['id'],
            remote={'account':clients[0]['account'],'profile':clients[0]['profile'],'activity_at':time.time()})
        service.request(clients[0],'prepare',job=row['id']);original=service.store.get(row['id'])
        service.execute({'action':'start'});self.process()
        self.assertEqual(sum(r['status']=='preparing' for r in service.store.rows()),4)
        self.assertEqual(service.store.get(row['id']),original)

    def test_paid_generation_and_unpaid_upload_wait_each_leave_other_three_launch_slots(self):
        for status in ('generating','uploading'):
            with self.subTest(status=status):
                self.setUp();service=self.queued();client=self.browser(service,'Profile 25')
                row=service.store.rows()[0];remote={'account':client['account'],'profile':client['profile'],'activity_at':time.time()}
                if status=='uploading': remote['transfer_retries']={'upload':{'count':2,'next_at':time.time()+300}}
                service.store.update(row['id'],status,attempt='a'*32,browser=client['id'],remote=remote)
                original=service.store.get(row['id']);launches=self.backups(service)
                service.execute({'action':'start'});self.process()
                self.assertEqual(len(launches),3)
                self.assertEqual(service.store.get(row['id']),original)
                self.assertFalse(any(p['action'] in {'run_tasks','repair_project'} for p in service.pending.values()))

    def test_start_reserves_only_four_profiles_and_one_task_never_opens_four(self):
        for count,expected in ((8,4),(1,1)):
            with self.subTest(count=count):
                self.setUp();service=self.queued(count);launches=self.backups(service,10)
                service.execute({'action':'start'});self.process()
                for _ in range(8):service.fill_dispatch_slots()
                self.assertEqual(len(launches),expected)
                self.assertEqual(launches,service.preferences['profiles'][:expected])
                self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))

    def test_open_unknown_accounts_are_probed_in_parallel_only_for_empty_lanes(self):
        service=self.queued();busy=self.browser(service,'Profile 25')
        row=service.store.rows()[0]
        service.store.update(row['id'],'generating',attempt='a'*32,browser=busy['id'],
            remote={'account':busy['account'],'profile':busy['profile'],'activity_at':time.time()})
        unknown=[self.browser(service,'Open '+str(n)) for n in range(10)]
        for client in unknown:client.update(credits=None,credits_local=False,credits_at=0)
        launches=self.backups(service)
        service.execute({'action':'start'});self.process()
        reads=[(k,p) for k,p in service.pending.items() if p['action']=='get_points']
        self.assertEqual(len(reads),3);self.assertFalse(launches)
        for key,request in reads:
            client=next(c for c in unknown if c['id']==request['client'])
            service._message(client['socket'],json.dumps({'request_id':key,'success':True,
                'data':{'email':client['account'],'remaining_points':50}}))
        self.process()
        self.assertEqual(sum(r['status'] in TRANSIENT for r in service.store.rows()),4)
        self.assertFalse(any(p['action']=='get_points' for p in service.pending.values()),'ready ordinary accounts must be consumed before probing more')
        self.assertFalse(launches)

    def test_backoff_moves_the_fifo_head_to_another_browser_without_later_jobs_jumping(self):
        service=self.queued();one=self.browser(service,'P1');two=self.browser(service,'P2')
        first=service.store.rows()[0]
        retry={'browser':one['id'],'profile':one['profile'],'count':2,'next_at':time.time()+300}
        one['prepare_wait']=copy.deepcopy(retry)
        service.store.update(first['id'],'queued',remote={'prepare_retry':retry})
        service.paused=False;service.fill_dispatch_slots()
        rows=service.store.rows()
        self.assertEqual(rows[0]['browser'],two['id']);self.assertEqual(rows[0]['status'],'preparing')
        self.assertTrue(all(r['status']=='queued' for r in rows[1:]))
        self.assertEqual(one['prepare_wait'],retry)

    def test_retained_but_underfunded_accounts_do_not_veto_a_useful_backup(self):
        service=self.queued();clients=[self.browser(service,'Owner '+str(n),points=11) for n in range(4)]
        row=service.store.rows()[0]
        for client in clients:remember_owner(service,row,client)
        # They can fund a later 6-second variant, but not this FIFO head.
        service.store.enqueue(self.jobs(duration=6,budget=0))
        launches=self.backups(service)
        service.execute({'action':'start'});self.process()
        self.assertEqual(len(launches),4)
        self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))

    def test_ready_accounts_do_not_trigger_unnecessary_point_reads_of_spares(self):
        service=self.queued();ready=[self.browser(service,'Ready '+str(n)) for n in range(4)]
        for n in range(10):self.browser(service,'Unknown '+str(n)).update(credits=None,credits_local=False,credits_at=0)
        service.execute({'action':'start'});self.process()
        self.assertEqual(sum(r['status']=='preparing' for r in service.store.rows()),4)
        self.assertFalse(any(p['action']=='get_points' for p in service.pending.values()))

    def test_stop_cancels_a_queued_dispatch_wakeup(self):
        service=self.queued();self.browser(service)
        service.execute({'action':'start'});service.execute({'action':'stop'});self.process()
        self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))

    def test_safety_pause_and_budget_remain_effective_during_fast_fill(self):
        for mode in ('safety','budget'):
            with self.subTest(mode=mode):
                self.setUp();service=self.queued();[self.browser(service,'P'+str(n)) for n in range(4)]
                service.paused=False
                if mode=='safety':service.safety_paused=True
                else:service.store.set_batch_limit(service.store.rows()[0]['payload']['batch_id'],1)
                service.wake_dispatch();self.process()
                self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))
                self.assertFalse(any(p['action'] in {'prepare','run_tasks'} for p in service.pending.values()))

if __name__=='__main__':unittest.main()
