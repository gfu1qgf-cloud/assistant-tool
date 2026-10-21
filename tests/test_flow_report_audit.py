"""Verify each external report claim against behavior before changing policy."""
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from flow_reference_fixtures import upload_receipt
from app_plugins.builtin.flow_assistant.browser_slots import request_start, occupied_profiles
from app_plugins.builtin.flow_assistant.connection_recovery import repair_idle
from app_plugins.builtin.flow_assistant.setup_support import compatible_extension


class ReportAuditTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    jobs = fixtures.CreditCooldownTests.jobs
    observe = fixtures.CreditCooldownTests.observe

    def queue(self, service, **values):
        service.store.enqueue(self.jobs(budget=0, **values)); service.paused = False
        return service.store.rows()[-1]

    def test_c_prepare_cost_failure_must_not_block_a_cheaper_job_for_same_account(self):
        service=self.service(); client=self.browser(service,points=15)
        row=self.queue(service,duration=4); service._tick()
        key=next(k for k,p in service.pending.items() if p['action']=='prepare')
        service._response(client,{'request_id':key,'success':True,'data':{
            'verified':True,'model':'omni_1_1_flash','cost':20}})
        self.assertEqual(service.store.get(row['id'])['status'],'queued')
        cheap=self.queue(service,duration=6); service._tick()
        self.assertEqual(service.store.get(cheap['id'])['status'],'preparing')
        self.assertEqual(service.store.get(cheap['id'])['browser'],client['id'])

    def test_c_upload_cost_failure_must_not_block_a_cheaper_job_for_same_account(self):
        service=self.service(); client=self.browser(service,points=20)
        row=self.queue(service,duration=8); service._tick()
        key=next(k for k,p in service.pending.items() if p['action']=='prepare')
        service._response(client,{'request_id':key,'success':True,'data':{
            'verified':True,'model':'omni_1_1_flash'}})
        key=next(k for k,p in service.pending.items() if p['action']=='upload_images')
        client['credits']=10
        service._response(client,{'request_id':key,'success':True,'data':upload_receipt(service,row['id'])})
        cheap=self.queue(service,duration=6); service._tick()
        self.assertEqual(service.store.get(cheap['id'])['status'],'preparing')
        self.assertFalse(service.store.attempt_charged(row['attempt']))

    def test_c_legacy_insufficient_flag_recovers_without_clearing_other_account_holds(self):
        service=self.service(); client=self.browser(service,points=15); client['blocked']='积分不足'
        row=self.queue(service,duration=6); service._tick()
        self.assertEqual(service.store.get(row['id'])['status'],'preparing')

    def test_b_temporary_sqlite_lock_keeps_manual_releases_and_recovers_on_next_tick(self):
        service=self.service(); self.browser(service,points=40)
        row=self.queue(service,duration=8); service.paused=True; service.released={row['id']}
        errors=[];service.failed.connect(errors.append)
        with patch.object(service.store,'rows',side_effect=sqlite3.OperationalError('database is locked')):
            for _ in range(3): service._tick()
        self.assertFalse(service.safety_paused)
        self.assertTrue(service.paused)
        self.assertEqual(service.released,{row['id']})
        self.assertEqual(len(errors),1)
        service._tick()
        self.assertEqual(service.store.get(row['id'])['status'],'preparing')

    def test_b_lock_does_not_repeat_an_already_sent_paid_request(self):
        service=self.service(); client=self.browser(service,points=40)
        row=self.queue(service,duration=8)
        service.store.update(row['id'],'submitting',attempt='a'*32,browser=client['id'],
            remote={'profile':client['profile'],'account':client['account'],'activity_at':time.time()})
        service.store.reserve(row['id'],'a'*32,12,'duration_rule')
        service.request(client,'run_tasks',{'batch_id':'a'*32},row['id'])
        with patch.object(service.store,'rows',side_effect=sqlite3.OperationalError('database table is locked')):
            service._tick()
        service._tick()
        actions=[json.loads(call.args[0]).get('action') for call in client['socket'].sendTextMessage.call_args_list]
        self.assertEqual(actions.count('run_tasks'),1)
        self.assertEqual(service.store.get(row['id'])['status'],'submitting')
        self.assertFalse(service.safety_paused)

    def test_b_unexpected_exception_and_actual_site_warning_keep_safety_pause(self):
        service=self.service(); client=self.browser(service,points=40)
        self.queue(service,duration=8)
        with patch.object(service.store,'rows',side_effect=ValueError('invalid queue data')): service._tick()
        self.assertTrue(service.safety_paused)
        service._message(client['socket'],json.dumps({'type':'site_pause'}))
        with patch.object(service.store,'rows',side_effect=sqlite3.OperationalError('database is locked')): service._tick()
        self.assertTrue(service.safety_paused)

    def test_d_a_new_reviewed_patch_needs_only_the_central_supported_version_updated(self):
        service=self.service(); client=self.browser(service,points=40)
        client.update(extension_version='1.1.28',page_ready=False,connected=time.time()-200)
        with patch('app_plugins.builtin.flow_assistant.setup_support.EXTENSION_VERSION','1.1.28'):
            self.assertTrue(compatible_extension('1.1.28'))
            self.assertTrue(repair_idle(service,client))
        self.assertFalse(compatible_extension('1.1.99'))

    def test_e_repeated_launch_candidates_already_emit_one_native_launch(self):
        service=self.service();service.preferences.update(profiles=['Probe'],profile_projects={'Probe':fixtures.PROJECT})
        launches=[];service.launch_requested.connect(launches.append)
        self.assertTrue(request_start(service,'Probe'))
        for _ in range(5): self.assertFalse(request_start(service,'Probe'))
        self.assertEqual(launches,['Probe'])

    def test_f_successful_current_account_read_invalidates_an_old_probe_deadline(self):
        service=self.service(); client=self.browser(service,points=40)
        service.small_balance_probes={client['profile']:time.time()-200}
        self.observe(service,client,50,email='new-account@example.test')
        self.assertNotIn(client['profile'],service.small_balance_probes)

    def test_f_unrelated_new_queue_does_not_inherit_completed_queue_probe_failures(self):
        service=self.service(); first=self.queue(service,duration=8)
        service.small_balance_probes={'Old':time.time()-200};service._tick()
        service.store.update(first['id'],'completed')
        self.queue(service,duration=10);service._tick()
        self.assertNotIn('Old',service.small_balance_probes)

    def test_f_adding_work_to_an_existing_queue_keeps_bounded_probe_failures(self):
        service=self.service();self.queue(service,duration=8)
        service.small_balance_probes={'Old':time.time()-200};service._tick()
        self.queue(service,duration=10);service._tick()
        self.assertIn('Old',service.small_balance_probes)

    def test_g_actual_site_pause_text_already_resets_after_explicit_user_action(self):
        service=self.service();client=self.browser(service,points=40)
        service._message(client['socket'],json.dumps({'type':'site_pause'}))
        self.assertTrue(client['blocked'])
        service.execute({'action':'reset_account_state','keys':['profile:'+client['profile']]})
        self.assertFalse(client['blocked'])
        self.assertTrue(service.safety_paused)
        service.execute({'action':'start'})
        self.assertFalse(service.safety_paused)

    def test_a_one_unconfirmed_paid_account_leaves_other_three_online_accounts_parallel(self):
        service=self.service();clients=[self.browser(service,'Account '+str(i),points=40+i) for i in range(4)]
        service.preferences['concurrency']=4
        self.queue(service,duration=8,count=6);rows=service.store.rows()
        service.store.update(rows[0]['id'],'uncertain',browser=clients[0]['id'],
            remote={'profile':clients[0]['profile'],'account':clients[0]['account']})
        for _ in range(4): service._tick()
        active=[row for row in service.store.rows() if row['status']=='preparing']
        self.assertEqual({row['browser'] for row in active},{client['id'] for client in clients[1:]})
        self.assertEqual(service.store.get(rows[0]['id'])['status'],'uncertain')
        self.assertEqual(len(occupied_profiles(service)),4)


if __name__=='__main__':unittest.main()
