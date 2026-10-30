"""Error loops must not impersonate progress or hold healthy-pass admission."""
import copy,time,unittest
from unittest.mock import patch
import test_flow_stall_recovery as stall
import test_flow_fault_rotation as fault
from app_plugins.builtin.flow_assistant import scheduler_recovery as recovery,fault_rotation as rotation


class ErrorProgressTests(unittest.TestCase):
    setUpClass=stall.StallRecoveryTests.__dict__['setUpClass']
    setUp=stall.StallRecoveryTests.setUp
    service=stall.StallRecoveryTests.service
    browser=stall.StallRecoveryTests.browser
    client=stall.StallRecoveryTests.client
    jobs=stall.StallRecoveryTests.jobs
    ready=stall.StallRecoveryTests.ready

    def incident(self,status='queued'):
        service=self.service();service.timer.stop();service.paused=False;self.ready(service)
        service.store.enqueue(self.jobs(count=2,duration=8,budget=0));row=service.store.rows()[0]
        if status!='queued':service.store.update(row['id'],status,attempt='a'*32)
        start=time.time()
        with patch('time.time',return_value=start):recovery.watch(service)
        with patch('time.time',return_value=start+31),patch.object(recovery,'recover',return_value=(0,0)):recovery.watch(service)
        return service,service.store.get(row['id']),start

    def test_unconfirmed_submission_attempt_does_not_restart_stall_clock(self):
        service,row,start=self.incident();before=copy.deepcopy(service.scheduler_recovery)
        service.store.update(row['id'],'submitting',attempt='b'*32)
        with patch('time.time',return_value=start+40):recovery.watch(service)
        self.assertEqual(service.scheduler_recovery,before)

    def test_download_retry_status_changes_do_not_restart_stall_clock(self):
        service,row,start=self.incident('generating');before=copy.deepcopy(service.scheduler_recovery)
        service.store.update(row['id'],'downloading')
        with patch('time.time',return_value=start+40):recovery.watch(service)
        self.assertEqual(service.scheduler_recovery,before)
        service.store.update(row['id'],'generating')
        with patch('time.time',return_value=start+41):recovery.watch(service)
        self.assertEqual(service.scheduler_recovery,before)

    def test_repeated_unconfirmed_submissions_eventually_report_attention(self):
        service,row,start=self.incident();notices=[];service.scheduler_attention.connect(notices.append)
        for seconds in (40,77,123,200):
            service.store.update(row['id'],'submitting',attempt=f'{seconds:032x}')
            with patch('time.time',return_value=start+seconds),patch.object(recovery,'recover',return_value=(0,0)):
                recovery.watch(service)
        self.assertEqual(len(notices),1)
        self.assertTrue(service.scheduler_recovery['attention'])


class FaultCandidateTests(unittest.TestCase):
    setUpClass=fault.FaultRotationTests.__dict__['setUpClass']
    setUp=fault.FaultRotationTests.setUp
    service=fault.FaultRotationTests.service
    browser=fault.FaultRotationTests.browser
    client=fault.FaultRotationTests.client
    jobs=fault.FaultRotationTests.jobs
    ready=fault.FaultRotationTests.ready
    pool=fault.FaultRotationTests.pool

    def offline_start(self,age):
        service,bad,good,row=self.pool();service._disconnected(good['socket'])
        from app_plugins.builtin.flow_assistant.page_readiness import defer_prepare
        defer_prepare(service,bad,row,fault.ERROR,'FLOW_SETTINGS_NOT_READY')
        at=time.time()-age
        service.preferences['browser_slots']={'Good':{'state':'opening','at':at,'opened_at':at}}
        service.launch_attempts.add('Good');service.launched.add('Good');service.launching['Good']=at
        return service

    def test_expired_unconnected_start_is_not_a_healthy_candidate_forever(self):
        service=self.offline_start(1000);before=copy.deepcopy(service.preferences['browser_slots'])
        self.assertFalse(rotation.healthy_remaining(service))
        self.assertEqual(service.preferences['browser_slots'],before,'lack of connection is not proof the native window closed')

    def test_live_startup_still_counts_as_a_healthy_candidate(self):
        service=self.offline_start(2)
        self.assertTrue(rotation.healthy_remaining(service))

    def test_closed_fault_rejoins_while_unconnected_native_reservation_is_retained(self):
        from app_plugins.builtin.flow_assistant import browser_lifecycle as lifecycle
        service=self.offline_start(1000)
        bad=next(c for c in service.clients.values() if c.get('profile')=='Bad')
        self.assertTrue(lifecycle.watch(service));fault.FaultRotationTests.confirm(self,service,bad)
        service._disconnected(bad['socket']);lifecycle.watch(service)
        self.assertEqual(service.preferences['browser_lifecycle']['Bad']['state'],'eligible')
        self.assertIn('Good',service.preferences['browser_slots'])
        self.assertIn('Good',recovery.details(service))
        self.assertIn('启动后未建立扩展连接',recovery.details(service))


if __name__=='__main__':unittest.main()
