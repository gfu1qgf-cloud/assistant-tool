"""Cross-browser UI phases serialize; cloud work and the Qt worker remain free."""
import copy
import json
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant import ui_mutex
from app_plugins.builtin.flow_assistant.foreground import receive as foreground_receive


class UiMutexTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    jobs=fixtures.CreditCooldownTests.jobs
    client=fixtures.CreditCooldownTests.client
    browser=fixtures.CreditCooldownTests.browser

    def configured(self):
        service=self.service();service.timer.stop()
        service.preferences['browser_options']={'ui_mutex':True,'focus_before_action':False}
        clients=[self.browser(service,'Mutex '+str(i)) for i in range(3)]
        for client in clients:client['capabilities']=['ui_mutex_v1','native_foreground_v1']
        return service,clients

    def request(self,service,client,token,operation='prepare',**extra):
        if operation!='submit':service.request(client,operation)
        ui_mutex.receive(service,client,{'type':'ui_mutex_request','token':token,
            'url':fixtures.PROJECT,'operation':operation,**extra})

    def messages(self,client):
        return [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list]

    def test_fifo_handoff_does_not_wait_for_cloud_jobs(self):
        service,clients=self.configured()
        for i,client in enumerate(clients):self.request(service,client,str(i+1)*32)
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[0]['id'])
        self.assertEqual([e['client'] for e in ui_mutex.state(service)['queue']],[c['id'] for c in clients[1:]])
        ui_mutex.receive(service,clients[0],{'type':'ui_mutex_release','token':'1'*32})
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[1]['id'])
        ui_mutex.receive(service,clients[1],{'type':'ui_mutex_release','token':'2'*32})
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[2]['id'])

    def test_foreign_release_and_duplicate_requests_do_not_steal_a_lease(self):
        service,clients=self.configured();self.request(service,clients[0],'1'*32)
        ui_mutex.receive(service,clients[1],{'type':'ui_mutex_release','token':'1'*32})
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[0]['id'])
        ui_mutex.receive(service,clients[0],{'type':'ui_mutex_request','token':'1'*32,
            'url':fixtures.PROJECT,'operation':'prepare'})
        self.assertFalse(ui_mutex.state(service)['queue'])

    def test_waiting_does_not_consume_the_page_operation_timeout(self):
        service,clients=self.configured();self.request(service,clients[0],'1'*32)
        self.request(service,clients[1],'2'*32)
        pending=next(p for p in service.pending.values() if p['client']==clients[1]['id'])
        with patch('time.time',return_value=time.time()+100):ui_mutex.watch(service)
        self.assertLess(time.time()+100-pending['at'],1)
        self.assertEqual(clients[1]['ui_mutex']['state'],'waiting')

    def test_live_disable_releases_owner_and_all_waiters_without_touching_jobs(self):
        service,clients=self.configured()
        for i,client in enumerate(clients):self.request(service,client,str(i+1)*32)
        before=copy.deepcopy(service.store.rows())
        service.execute({'action':'preferences','value':{'browser_options':{'ui_mutex':False}}})
        self.assertIsNone(ui_mutex.state(service)['owner']);self.assertFalse(ui_mutex.state(service)['queue'])
        for client in clients:self.assertTrue(any(m.get('state')=='disabled' for m in self.messages(client)))
        self.assertEqual(service.store.rows(),before)

    def test_disconnect_and_expiry_release_the_next_browser(self):
        service,clients=self.configured();self.request(service,clients[0],'1'*32)
        self.request(service,clients[1],'2'*32)
        ui_mutex.disconnected(service,clients[0])
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[1]['id'])
        with patch('time.time',return_value=time.time()+ui_mutex.phase_limit(service)+1):ui_mutex.watch(service)
        self.assertIsNone(ui_mutex.state(service)['owner'])
        self.assertTrue(any(m.get('state')=='expired' for m in self.messages(clients[1])))

    def test_reset_or_retired_unpaid_operation_cannot_start_later_from_wait_queue(self):
        service,clients=self.configured();self.request(service,clients[0],'1'*32)
        self.request(service,clients[1],'2'*32)
        for pending in service.pending.values():
            if pending['client']==clients[1]['id']:pending['retired']=True
        ui_mutex.receive(service,clients[0],{'type':'ui_mutex_release','token':'1'*32})
        self.assertIsNone(ui_mutex.state(service)['owner'])
        self.assertTrue(any(m.get('state')=='rejected' for m in self.messages(clients[1])))

    def test_generation_phase_releases_without_finishing_or_replaying_cloud_job(self):
        service,clients=self.configured();service.store.enqueue(self.jobs(budget=0))
        row=service.store.rows()[0];service.store.update(row['id'],'generating',attempt='a'*32,browser=clients[0]['id'])
        self.request(service,clients[0],'1'*32,'submit',batch_id='a'*32)
        self.assertIsNotNone(ui_mutex.state(service)['owner'])
        self.request(service,clients[1],'2'*32)
        ui_mutex.receive(service,clients[0],{'type':'ui_mutex_release','token':'1'*32})
        self.assertEqual(service.store.get(row['id'])['status'],'generating')
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[1]['id'])
        self.assertFalse(any(p['action']=='run_tasks' for p in service.pending.values()))

    def test_stale_paid_attempt_and_background_download_cannot_claim_ui(self):
        service,clients=self.configured()
        self.request(service,clients[0],'1'*32,'submit',batch_id='a'*32)
        self.assertIsNone(ui_mutex.state(service)['owner'])
        self.request(service,clients[0],'2'*32,'snapshot')
        self.assertIsNone(ui_mutex.state(service)['owner'])

    def test_download_preview_requires_the_exact_pending_attempt(self):
        service,clients=self.configured();client=clients[0]
        service.pending['preview']={'client':client['id'], 'action':'retry_download',
            'attempt':'a'*32, 'job':None, 'at':time.time()}
        request={'type':'ui_mutex_request','token':'1'*32,'url':fixtures.PROJECT,
                 'operation':'retry_download','batch_id':'b'*32}
        ui_mutex.receive(service,client,request)
        self.assertIsNone(ui_mutex.state(service)['owner'])
        self.assertEqual(self.messages(client)[-1]['state'],'rejected')
        ui_mutex.receive(service,client,{**request,'token':'2'*32,'batch_id':'a'*32})
        self.assertEqual(ui_mutex.state(service)['owner']['operation'],'retry_download')
        self.assertEqual(ui_mutex.state(service)['owner']['batch_id'],'a'*32)
        service.pending.clear();ui_mutex.watch(service)
        self.assertIsNone(ui_mutex.state(service)['owner'])
        self.assertEqual(self.messages(client)[-1]['state'],'expired')

    def test_download_preview_does_not_extend_other_attempts_or_join_a_paid_pack(self):
        from app_plugins.builtin.flow_assistant import pack_mutex
        service,clients=self.configured();client=clients[0]
        for attempt in ('a'*32,'b'*32):
            service.pending[attempt]={'client':client['id'], 'action':'retry_download',
                'attempt':attempt, 'job':None, 'at':0}
        entry={'client':client['id'],'operation':'retry_download','batch_id':'a'*32}
        self.assertEqual([p['attempt'] for p in pack_mutex.requests(service,entry)],['a'*32])
        self.assertFalse(pack_mutex.packed_request(service,entry))

    def test_windows_activation_requires_the_current_lease_and_focus_option_on(self):
        service,clients=self.configured();self.request(service,clients[0],'1'*32)
        service.preferences['browser_options']['focus_before_action']=True
        with patch('app_plugins.builtin.flow_assistant.foreground.native_activate',return_value='focused') as activate:
            foreground_receive(service,clients[0],{'type':'foreground_request','request_id':'1'*32,'token':'1'*32,'window_id':17})
            activate.assert_called_once()
        self.assertEqual(clients[0]['foreground']['state'],'focused')

    def test_old_bridge_cannot_silently_bypass_enabled_mutex(self):
        from app_plugins.builtin.flow_assistant.policy import capable
        service,clients=self.configured();clients[0]['capabilities']=[]
        self.assertFalse(service.request(clients[0],'get_points'))
        self.assertFalse(capable(clients[0],self.jobs(budget=0)[0],service.preferences,10))
        self.assertIn(fixtures.EXTENSION_VERSION,clients[0]['runtime_note'])
        service.preferences['browser_options']['ui_mutex']=False
        self.assertTrue(service.request(clients[0],'get_points'))

    def test_application_stop_revokes_without_granting_a_new_browser(self):
        service,clients=self.configured();self.request(service,clients[0],'1'*32)
        self.request(service,clients[1],'2'*32)
        service.closing=True
        ui_mutex.receive(service,clients[0],{'type':'ui_mutex_release','token':'1'*32})
        self.assertIsNone(ui_mutex.state(service)['owner']);self.assertFalse(ui_mutex.state(service)['queue'])
        self.assertFalse(any(m.get('state')=='granted' for m in self.messages(clients[1])))


if __name__=='__main__':unittest.main()
