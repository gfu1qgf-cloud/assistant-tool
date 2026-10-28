"""Real scheduler exits for stalled/no-send credit checks and UI leases."""
import copy
import json
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.browser_reuse import before_new_browser
from app_plugins.builtin.flow_assistant import ui_mutex


class ReadinessExitTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser
    jobs=fixtures.CreditCooldownTests.jobs

    def queued(self,count=1):
        service=self.service();service.timer.stop();service.paused=False
        service.preferences.update(concurrency=count,browser_options={'ui_mutex':True})
        service.store.enqueue(self.jobs(count=14,duration=8,budget=0))
        clients=[self.browser(service,'Existing '+str(i),points=50) for i in range(count)]
        for client in clients:
            client.update(credits_at=time.time()-21601,capabilities=['ui_mutex_v1','runtime_controls_v1'])
        service.preferences['profiles']=[c['profile'] for c in clients]+['Backup']
        service.preferences['profile_projects']['Backup']=fixtures.PROJECT
        launches=[];service.launch_requested.connect(launches.append)
        return service,clients,launches

    def test_missing_mutex_support_requests_upgrade_without_opening_more_old_bridges(self):
        service,clients,launches=self.queued(8)
        for client in clients:client['capabilities']=[]
        service.fill_dispatch_slots()
        self.assertFalse(launches)
        self.assertFalse(service.pending)
        for client in clients:
            self.assertNotEqual(client.get('reuse_recovery',{}).get('state'),'stopped')
        self.assertIn('扩展',service.browser_reuse_wait['message'])

    def test_readiness_total_deadline_survives_pending_timeout_renewal(self):
        service,clients,_=self.queued();client=clients[0];job=service.store.rows()[0]['payload']
        service.request(client,'get_points')
        request_id=next(iter(service.pending))
        self.assertTrue(before_new_browser(service,job))
        since=client['reuse_recovery']['since'];before=copy.deepcopy(service.store.rows())
        # The mutex waiter currently renews the ordinary request timer.
        for request in service.pending.values():request['at']=since+91
        with patch('time.time',return_value=since+91):
            self.assertFalse(before_new_browser(service,job))
            self.assertFalse(service.request(client,'get_points'))
        self.assertNotIn(request_id,service.pending)
        self.assertEqual(service.store.rows(),before)
        service._response(client,{'request_id':request_id,'success':True,
            'data':{'email':client['account'],'remaining_points':999}})
        self.assertEqual(client['credits'],50,'a retired read cannot resurrect spending authority')

    def test_credit_ui_lease_uses_credit_timeout_not_upload_timeout(self):
        service,clients,_=self.queued(2)
        for index,client in enumerate(clients):
            service.request(client,'get_points')
            ui_mutex.receive(service,client,{'type':'ui_mutex_request','token':str(index+1)*32,
                'url':fixtures.PROJECT,'operation':'get_points'})
        start=ui_mutex.state(service)['owner']['at']
        with patch('time.time',return_value=start+66):ui_mutex.watch(service)
        owner=ui_mutex.state(service)['owner']
        self.assertTrue(owner is None or owner['client']!=clients[0]['id'])
        messages=[json.loads(c.args[0]) for c in clients[0]['socket'].sendTextMessage.call_args_list]
        self.assertTrue(any(m.get('state')=='expired' for m in messages))

    def test_eight_stalled_reads_release_task_lanes_without_exceeding_window_cap(self):
        service,clients,launches=self.queued(8);service.fill_dispatch_slots()
        self.assertFalse(launches)
        since=min(c['reuse_recovery']['since'] for c in clients)
        # Preserve queued lock state and ordinary timers to reproduce the
        # screenshot: no paid jobs, eight accounts claim all eight vacancies.
        for index,client in enumerate(clients):
            ui_mutex.receive(service,client,{'type':'ui_mutex_request','token':f'{index+1:032x}',
                'url':fixtures.PROJECT,'operation':'get_points'})
        with patch('time.time',return_value=since+92):service.fill_dispatch_slots()
        self.assertEqual(launches,[],'eight unresolved physical windows require a confirmed safe close before another launch')
        from app_plugins.builtin.flow_assistant.browser_slots import summary
        self.assertIn('窗口上限',summary(service)['message'])
        self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))
        self.assertFalse(any(p['action']=='get_points' and p['client']==clients[0]['id'] for p in service.pending.values()))
        self.assertTrue(any(p['action']=='get_points' and p['client']==clients[1]['id'] for p in service.pending.values()),
            'the next valid waiter gets its own bounded read instead of being failed with the stalled owner')

    def test_failed_preflight_does_not_revoke_another_accounts_live_upload(self):
        service,clients,_=self.queued(2);upload,reader=clients
        row=service.store.rows()[0]
        service.store.update(row['id'],'uploading',attempt='a'*32,browser=upload['id'],
            remote={'account':upload['account'],'profile':upload['profile']})
        service.request(upload,'upload_images',job=row['id'])
        ui_mutex.receive(service,upload,{'type':'ui_mutex_request','token':'1'*32,'url':fixtures.PROJECT,'operation':'upload_images'})
        service.request(reader,'get_points')
        ui_mutex.receive(service,reader,{'type':'ui_mutex_request','token':'2'*32,'url':fixtures.PROJECT,'operation':'get_points'})
        job=service.store.rows()[1]['payload'];before=copy.deepcopy(service.store.get(row['id']))
        before_new_browser(service,job);since=reader['reuse_recovery']['since']
        with patch('time.time',return_value=since+91):before_new_browser(service,job)
        self.assertEqual(ui_mutex.state(service)['owner']['client'],upload['id'])
        self.assertEqual(service.store.get(row['id']),before)
        self.assertTrue(any(p['job']==row['id'] for p in service.pending.values()))

    def test_reconnection_cannot_reset_a_stopped_budget_but_manual_read_can(self):
        from app_plugins.builtin.flow_assistant.browser_reuse import abandon
        service,clients,_=self.queued();client=clients[0]
        abandon(service,client,'test timeout')
        replacement=self.browser(service,client['profile'],points=50)
        replacement['capabilities']=['ui_mutex_v1']
        self.assertFalse(service.request(replacement,'get_points'))
        self.assertTrue(service.request(replacement,'get_points',manual_credit=True))
        key=next(k for k,p in service.pending.items() if p['client']==replacement['id'])
        service._response(replacement,{'request_id':key,'success':True,
            'data':{'email':replacement['account'],'remaining_points':50}})
        self.assertNotIn('reuse_recovery',replacement)

    def test_an_available_account_still_dispatches_fifo_while_others_are_stopped(self):
        from app_plugins.builtin.flow_assistant.browser_reuse import abandon
        service,clients,launches=self.queued(2);abandon(service,clients[0],'test timeout')
        clients[1]['credits_at']=time.time()
        first=service.store.rows()[0]['id'];service.fill_dispatch_slots()
        self.assertEqual(service.store.get(first)['browser'],clients[1]['id'])
        self.assertEqual(service.store.get(first)['status'],'preparing')
        self.assertFalse(any(p['action']=='get_points' and p['client']==clients[0]['id'] for p in service.pending.values()))

    def test_uncertain_paid_result_is_never_released_or_resent_by_readiness_exit(self):
        service,clients,_=self.queued();client=clients[0];row=service.store.rows()[0]
        service.store.update(row['id'],'uncertain',attempt='a'*32,browser=client['id'],
            remote={'account':client['account'],'profile':client['profile'],'generation_response':{'submissionId':'original'}})
        before=copy.deepcopy(service.store.get(row['id']))
        with patch('time.time',return_value=time.time()+3600):before_new_browser(service,service.store.rows()[1]['payload'])
        self.assertEqual(service.store.get(row['id']),before)
        self.assertNotIn('reuse_recovery',client)
        self.assertFalse(service.pending)


if __name__=='__main__':unittest.main()
