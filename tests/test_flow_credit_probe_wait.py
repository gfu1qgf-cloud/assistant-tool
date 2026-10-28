"""Verified UI queue time is not a broken browser or a failed credit read."""
import copy,time,unittest
from unittest.mock import patch
import test_flow_readiness_exit as readiness
from app_plugins.builtin.flow_assistant import ui_mutex
from app_plugins.builtin.flow_assistant.browser_reuse import before_new_browser
from app_plugins.builtin.flow_assistant.page_readiness import retry_initial_credits
import test_flow_credit_cooldown as fixtures

class CreditProbeWaitTests(unittest.TestCase):
    setUpClass=readiness.ReadinessExitTests.__dict__['setUpClass']
    setUp=readiness.ReadinessExitTests.setUp
    service=readiness.ReadinessExitTests.service
    jobs=readiness.ReadinessExitTests.jobs
    client=readiness.ReadinessExitTests.client
    browser=readiness.ReadinessExitTests.browser
    queued=readiness.ReadinessExitTests.queued

    def setup_wait(self,manual=False):
        service,clients,_=self.queued(2);owner,reader=clients
        service.request(owner,'upload_images')
        ui_mutex.receive(service,owner,{'type':'ui_mutex_request','token':'1'*32,'url':fixtures.PROJECT,'operation':'upload_images'})
        service.request(reader,'get_points',manual_credit=manual);request_id=next(k for k,p in service.pending.items() if p['client']==reader['id'])
        job=service.store.rows()[0]['payload'];before_new_browser(service,job)
        since=reader['reuse_recovery']['since']
        return service,owner,reader,request_id,job,since

    def test_late_queued_credit_read_does_not_consume_active_preparation_timeout(self):
        service,owner,reader,key,job,since=self.setup_wait()
        with patch('time.time',return_value=since+70):
            ui_mutex.receive(service,reader,{'type':'ui_mutex_request','token':'2'*32,'url':fixtures.PROJECT,'operation':'get_points'})
        with patch('time.time',return_value=since+100):
            before_new_browser(service,job)
            self.assertEqual(reader['credit_check']['state'],'waiting')
            self.assertNotEqual(reader['reuse_recovery'].get('state'),'stopped')
            self.assertIn(key,service.pending)
            ui_mutex.receive(service,owner,{'type':'ui_mutex_release','token':'1'*32})
            self.assertEqual(reader['credit_check']['state'],'checking')
            self.assertAlmostEqual(reader['reuse_recovery']['since'],since+30,delta=.1)
            before_new_browser(service,job)
            self.assertNotEqual(reader['reuse_recovery'].get('state'),'stopped')
        with patch('time.time',return_value=since+191):
            before_new_browser(service,job)
            self.assertEqual(reader['reuse_recovery']['state'],'stopped','real unresponsive read still exits')

    def test_wait_errors_retry_without_changing_credit_or_page_evidence(self):
        service,owner,reader,key,job,since=self.setup_wait();original=copy.deepcopy(service.store.rows())
        original_points=reader['credits'];reader['credits_ready_failures']=2
        for _ in range(7):
            service._response(reader,{'request_id':key,'success':False,'code':'FLOW_UI_MUTEX_UNAVAILABLE',
                'error':'前台 UI 操作互斥申请未完成：rejected'})
            self.assertEqual(reader['credits_ready_failures'],2)
            self.assertEqual(reader['credits'],original_points);self.assertTrue(reader['page_ready'])
            self.assertEqual(reader['credit_check']['state'],'waiting')
            self.assertEqual(ui_mutex.state(service)['owner']['client'],owner['id'])
            with patch('time.time',return_value=reader['credits_retry_at']+1):self.assertTrue(retry_initial_credits(service,reader))
            key=next(k for k,p in service.pending.items() if p['client']==reader['id'])
        self.assertEqual(service.store.rows(),original)

    def test_permission_fault_is_not_treated_as_queue_wait(self):
        service,owner,reader,key,job,since=self.setup_wait()
        service._response(reader,{'request_id':key,'success':False,'code':'FLOW_UI_MUTEX_UNAVAILABLE',
            'error':'前台 UI 操作互斥申请未完成：wrong project'})
        self.assertEqual(reader['credit_check']['state'],'failed')
        self.assertFalse(reader['credits_waiting_ready'])

    def test_manual_check_keeps_dispatch_hold_through_queue_and_grant(self):
        service,owner,reader,key,job,since=self.setup_wait(manual=True)
        ui_mutex.receive(service,reader,{'type':'ui_mutex_request','token':'2'*32,'url':fixtures.PROJECT,'operation':'get_points'})
        self.assertTrue(reader['credit_check']['hold_dispatch'])
        ui_mutex.receive(service,owner,{'type':'ui_mutex_release','token':'1'*32})
        self.assertEqual(reader['credit_check']['state'],'checking')
        self.assertTrue(reader['credit_check']['hold_dispatch'],'do not spend old cached credit while a manual recheck is active')

if __name__=='__main__':unittest.main()
