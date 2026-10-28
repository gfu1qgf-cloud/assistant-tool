"""Real local lease handoffs with simulated Chrome launches and native windows."""
import copy
import time
import unittest
from unittest.mock import MagicMock, patch

from app_plugins.builtin.flow_assistant import ui_mutex
from app_plugins.builtin.flow_assistant.foreground import receive as focus
import test_flow_ui_mutex as mutex_cases
import test_flow_launch_spacing as spacing
import test_flow_setup as setup
import test_chrome_profile_groups as groups


class LocalLaunchMutexTests(unittest.TestCase):
    setUpClass=mutex_cases.UiMutexTests.__dict__['setUpClass']
    setUp=mutex_cases.UiMutexTests.setUp
    service=mutex_cases.UiMutexTests.service
    browser=mutex_cases.UiMutexTests.browser
    client=mutex_cases.UiMutexTests.client
    jobs=mutex_cases.UiMutexTests.jobs
    configured=mutex_cases.UiMutexTests.configured
    page_request=mutex_cases.UiMutexTests.request
    ui=setup.SetupTests.ui
    create_dialog=groups.ChromeProfileGroupTests.create_dialog

    def bound(self):
        service,clients=self.configured()
        plugin,launcher,_=spacing.LaunchSpacingTests.fixture(self)
        plugin.snapshot['preferences']['browser_options']={'ui_mutex':True}
        plugin.service=service;plugin.thread=MagicMock()
        plugin.thread.isRunning.return_value=True
        plugin.command.connect(service.execute)
        service.native_ui_result.connect(plugin.launch_ui.result)
        windows=patch('app_plugins.builtin.flow_assistant.launch_ui.chrome_window_state',return_value=((10,),10))
        native=windows.start();self.addCleanup(windows.stop)
        return service,clients,plugin,launcher,native

    def release(self,service,client,token='1'*32):
        ui_mutex.receive(service,client,{'type':'ui_mutex_release','token':token})

    def test_upload_finishes_before_native_launch_and_duplicate_start_is_coalesced(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32,'upload_images')
        receipts=[]
        for _ in range(3):
            self.assertTrue(plugin.launch_browser('Profile 23',[setup.PROJECT],completed=lambda *r:receipts.append(r))[0])
        launcher.launch_profile.assert_not_called();self.assertFalse(receipts)
        self.assertEqual(len(ui_mutex.state(service)['queue']),1)
        self.assertFalse(plugin.launch_timer.isActive(),'waiting for a UI lease does not start the browser spacing timer')
        self.release(service,clients[0]);plugin.launch_ui.timer.stop()
        self.assertEqual(launcher.launch_profile.call_count,1)
        self.assertEqual(receipts,[(True,None)]*3)
        self.assertTrue(ui_mutex.state(service)['owner']['host'])
        self.assertTrue(plugin.launch_timer.isActive())

    def test_lock_covers_delayed_window_appearance_and_quiet_period_then_hands_back(self):
        service,clients,plugin,launcher,native=self.bound()
        with patch('time.monotonic',return_value=0) as clock:
            plugin.launch_browser('Profile 23',[setup.PROJECT]);plugin.launch_ui.timer.stop()
            self.page_request(service,clients[0],'1'*32)
            clock.return_value=5;plugin.launch_ui.poll()
            self.assertTrue(ui_mutex.state(service)['owner']['host'],'Popen returning alone cannot release UI protection')
            native.return_value=((10,20),20);plugin.launch_ui.poll()
            clock.return_value=5.5;plugin.launch_ui.poll()
            self.assertTrue(ui_mutex.state(service)['owner']['host'])
            clock.return_value=6.1;plugin.launch_ui.poll()
            self.assertTrue(ui_mutex.state(service)['owner']['host'],'a different manually opened Chrome is not proof of this Flow profile')
            plugin.snapshot['clients']=[{'profile':'Profile 23','project':setup.PROJECT,
                'page_loaded':True,'page_ready':False,'page_at':time.time()+1}]
            plugin.launch_ui.poll()
            self.assertIsNone(plugin.launch_ui.active)
            self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[0]['id'])
        self.assertEqual(launcher.launch_profile.call_count,1)

    def test_waiting_for_upload_does_not_consume_startup_window_or_change_jobs(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32,'upload_images')
        service.launching['Profile 23']=time.time()-500
        before=copy.deepcopy(service.store.rows())
        plugin.launch_browser('Profile 23',[setup.PROJECT])
        ui_mutex.watch(service)
        self.assertLess(time.time()-service.launching['Profile 23'],1)
        self.assertEqual(service.store.rows(),before);launcher.launch_profile.assert_not_called()

    def test_guard_is_rechecked_after_wait_and_cancel_does_not_open_a_window(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32)
        needed=[True];receipts=[]
        plugin.launch_browser('Profile 23',[setup.PROJECT],guard=lambda:needed[0],completed=lambda *r:receipts.append(r))
        needed[0]=False;self.release(service,clients[0])
        launcher.launch_profile.assert_not_called()
        self.assertEqual(receipts,[(False,'已取消待启动浏览器')])
        self.assertIsNone(ui_mutex.state(service)['owner'])

    def test_start_failure_and_guard_exception_release_without_imposing_browser_gap(self):
        for broken_guard in (False,True):
            service,clients,plugin,launcher,_=self.bound();receipts=[]
            if broken_guard:
                guard=MagicMock(side_effect=ValueError('bad guard'))
            else:
                guard=None;launcher.launch_profile.return_value=(False,'cannot start')
            plugin.launch_browser('Profile 23',[setup.PROJECT],guard=guard,completed=lambda *r:receipts.append(r))
            self.assertIsNone(ui_mutex.state(service)['owner']);self.assertIsNone(plugin.launch_ui.active)
            self.assertFalse(plugin.launch_timer.isActive());self.assertEqual(len(receipts),1)
            self.assertFalse(receipts[0][0])

    def test_stopping_cancels_queued_launch_and_stale_grant_cannot_reopen_chrome(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32);receipts=[]
        plugin.launch_browser('Profile 23',[setup.PROJECT],completed=lambda *r:receipts.append(r))
        token=plugin.launch_ui.active['ticket'];plugin.stop()
        plugin.launch_ui.result({'token':token,'state':'granted','expires_at':time.time()+30})
        launcher.launch_profile.assert_not_called();self.assertEqual(len(receipts),1)
        self.assertFalse(receipts[0][0]);self.assertFalse(ui_mutex.state(service)['queue'])

    def test_turning_mutex_off_continues_once_without_replaying_native_launch(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32)
        plugin.launch_browser('Profile 23',[setup.PROJECT]);token=plugin.launch_ui.active['ticket']
        service.execute({'action':'preferences','value':{'browser_options':{'ui_mutex':False}}})
        self.assertEqual(launcher.launch_profile.call_count,1)
        self.assertIsNone(plugin.launch_ui.active);self.assertIsNone(ui_mutex.state(service)['owner'])
        plugin.launch_ui.result({'token':token,'state':'granted','expires_at':time.time()+30})
        self.assertEqual(launcher.launch_profile.call_count,1)

    def test_unobserved_window_times_out_without_retrying_process_or_failing_jobs(self):
        service,clients,plugin,launcher,native=self.bound();native.return_value=None;receipts=[]
        before=copy.deepcopy(service.store.rows())
        with patch('time.monotonic',return_value=0) as clock:
            plugin.launch_browser('Profile 23',[setup.PROJECT],completed=lambda *r:receipts.append(r));plugin.launch_ui.timer.stop()
            clock.return_value=31;plugin.launch_ui.poll()
        self.assertIsNone(ui_mutex.state(service)['owner']);self.assertEqual(receipts,[(True,None)])
        self.assertEqual(launcher.launch_profile.call_count,1);self.assertEqual(service.store.rows(),before)

    def test_native_focus_message_without_owning_mutex_cannot_interrupt_native_start(self):
        service,clients,plugin,_,_=self.bound()
        plugin.launch_browser('Profile 23',[setup.PROJECT]);plugin.launch_ui.timer.stop()
        service.preferences['browser_options']['focus_before_action']=True
        service.request(clients[0],'prepare')
        request_id,pending=next((k,p) for k,p in service.pending.items() if p['action']=='prepare')
        with patch('app_plugins.builtin.flow_assistant.foreground.native_activate') as activate:
            focus(service,clients[0],{'type':'foreground_request','request_id':request_id,
                'token':pending['foreground_token'],'window_id':17})
            activate.assert_not_called()
        ui_mutex.receive(service,clients[0],{'type':'ui_mutex_release','token':plugin.launch_ui.active['ticket']})
        self.assertTrue(ui_mutex.state(service)['owner']['host'],'browser messages cannot release a main-process lease')

    def test_removed_queue_cancels_a_flow_start_that_was_waiting_for_ui(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32,'upload_images')
        plugin.snapshot['jobs']=[{'id':'queued-job','status':'queued'}]
        plugin._launch_checked({'profile':'Profile 23','status':'enabled','version':setup.EXTENSION_VERSION})
        launcher.launch_profile.assert_not_called()
        self.assertIsNotNone(plugin.launch_ui.active)
        plugin.snapshot['jobs']=[];self.release(service,clients[0])
        launcher.launch_profile.assert_not_called();self.assertIsNone(plugin.launch_ui.active)

    def test_force_delete_publishes_first_and_does_not_unlock_an_active_upload(self):
        service,clients,plugin,launcher,_=self.bound()
        service.store.enqueue(self.jobs(count=2,budget=0));rows=service.store.rows()
        row=rows[0];service.store.update(row['id'],'uploading',attempt='a'*32,browser=clients[0]['id'])
        self.page_request(service,clients[0],'1'*32,'upload_images')
        pending=next(p for p in service.pending.values() if p['action']=='upload_images')
        pending.update(job=row['id'],attempt='a'*32)
        service.preferences['profile_projects'].update(plugin.snapshot['preferences']['profile_projects'])
        plugin.snapshot['jobs']=service.store.rows()
        service.changed.connect(plugin._changed)
        plugin._launch_checked({'profile':'Profile 23','status':'enabled','version':setup.EXTENSION_VERSION})
        service.execute({'action':'delete_records','ids':[r['id'] for r in rows],'force':True})
        self.assertFalse(service.store.rows());self.assertFalse(plugin.snapshot['jobs'])
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[0]['id'])
        launcher.launch_profile.assert_not_called()
        self.release(service,clients[0])
        launcher.launch_profile.assert_not_called();self.assertIsNone(plugin.launch_ui.active)

    def test_all_waiting_profiles_keep_startup_reservations_only_within_wait_deadline(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32,'upload_images')
        for number in (23,24,25,26):
            profile=f'Profile {number}';service.launching[profile]=time.time()-500
            plugin.launch_browser(profile,[setup.PROJECT])
        with patch('time.time',return_value=time.time()+60) as clock:
            ui_mutex.watch(service)
            self.assertTrue(all(at==clock.return_value for at in service.launching.values()))
        launcher.launch_profile.assert_not_called()
        self.assertEqual(len(plugin.pending_browser_launches),3)
        self.assertEqual(len(service.ui_launch_waits),4)

    def test_expired_gui_queue_never_opens_a_browser_late_or_interrupts_upload(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32,'upload_images')
        for number in (23,24,25):plugin.launch_browser(f'Profile {number}',[setup.PROJECT])
        since=plugin.launch_ui.active['ui_wait_started']
        with patch('time.time',return_value=since+91):
            ui_mutex.watch(service)
            while plugin.pending_browser_launches:plugin._next_browser_launch()
        launcher.launch_profile.assert_not_called()
        self.assertIsNone(plugin.launch_ui.active)
        self.assertFalse(service.ui_launch_waits)
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[0]['id'])

    def test_manual_chrome_iterator_stays_outside_flow_lock(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32,'upload_images')
        plugin.launch_browser('Profile 23',[setup.PROJECT]);launcher.launch_profile.assert_not_called()
        manager=self.create_dialog(self.root/'manual-config.json');self.addCleanup(manager.close)
        manager.iterator_profile_directories=['Profile 1'];manager.launch_profile=MagicMock(return_value=(True,None))
        self.assertEqual(manager.launch_next_profile()['directory'],'Profile 1')
        manager.launch_profile.assert_called_once()
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[0]['id'])
        launcher.launch_profile.assert_not_called()

    def test_expired_host_receipt_cannot_erase_the_next_page_owner(self):
        service,clients,plugin,launcher,_=self.bound()
        plugin.launch_browser('Profile 23',[setup.PROJECT])
        plugin.launch_ui.timer.stop()
        self.assertTrue(ui_mutex.state(service)['owner']['host'])
        self.page_request(service,clients[0],'1'*32,'prepare')
        since=ui_mutex.state(service)['owner']['at']
        with patch('time.time',return_value=since+ui_mutex.host_limit(service)+1):ui_mutex.watch(service)
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[0]['id'])
        self.assertFalse(ui_mutex.state(service)['owner'].get('host'))


if __name__=='__main__':unittest.main()
