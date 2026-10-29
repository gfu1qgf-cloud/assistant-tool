import copy,time,unittest
from unittest.mock import patch
import test_flow_browser_lifecycle as fixtures
import test_flow_launch_ui_mutex as launch
from app_plugins.builtin.flow_assistant import fault_rotation as rotation,browser_lifecycle as lifecycle,ui_mutex
from app_plugins.builtin.flow_assistant.page_readiness import defer_prepare

ERROR='设置面板正在重建，自动等待后重新核对；未提交生成。'


class FaultRotationTests(unittest.TestCase):
    setUpClass=fixtures.BrowserLifecycleTests.__dict__['setUpClass']
    setUp=fixtures.BrowserLifecycleTests.setUp
    service=fixtures.BrowserLifecycleTests.service
    browser=fixtures.BrowserLifecycleTests.browser
    client=fixtures.BrowserLifecycleTests.client
    jobs=fixtures.BrowserLifecycleTests.jobs
    ready=fixtures.BrowserLifecycleTests.ready
    confirm=fixtures.BrowserLifecycleTests.confirm
    messages=fixtures.BrowserLifecycleTests.messages

    def pool(self):
        service=self.service();service.timer.stop();service.paused=False
        bad=self.ready(service,'Bad');good=self.ready(service,'Good')
        service.preferences.update(profiles=['Bad','Good'],profile_projects={c['profile']:c['saved_project'] for c in (bad,good)})
        service.store.enqueue(self.jobs(duration=10,budget=0));row=service.store.rows()[0]
        service.store.update(row['id'],'preparing',attempt='a'*32,browser=bad['id'],remote={'profile':'Bad','account':bad['account']})
        return service,bad,good,service.store.get(row['id'])

    def test_first_settings_fault_closes_without_refresh_and_waits_for_healthy_pass(self):
        service,bad,good,row=self.pool()
        self.assertTrue(defer_prepare(service,bad,row,ERROR,'FLOW_SETTINGS_NOT_READY'))
        self.assertTrue(rotation.is_fault('Bad',service.preferences))
        self.assertTrue(lifecycle.watch(service))
        self.assertEqual(self.messages(bad)[-1]['action'],'close_browser')
        self.assertFalse(any(p['action']=='repair_project' for p in service.pending.values()))
        self.confirm(service,bad);service._disconnected(bad['socket'])
        self.assertTrue(rotation.healthy_remaining(service));lifecycle.watch(service)
        self.assertEqual(service.preferences['browser_lifecycle']['Bad']['state'],'cooldown')
        good['credits']=0
        lifecycle.watch(service)
        self.assertEqual(service.preferences['browser_lifecycle']['Bad']['state'],'eligible')
        self.assertNotIn('profile:Bad',service.preferences['browser_blocks'])
        self.assertFalse(rotation.is_fault('Bad',service.preferences))

    def test_unknown_unopened_healthy_profile_is_tried_before_faults(self):
        service,bad,good,row=self.pool()
        defer_prepare(service,bad,row,ERROR,'FLOW_SETTINGS_NOT_READY')
        service._disconnected(good['socket'])
        self.assertTrue(rotation.healthy_remaining(service))

    def test_user_disabled_and_future_credit_limits_survive_round_cleanup(self):
        service,bad,good,row=self.pool()
        defer_prepare(service,bad,row,ERROR,'FLOW_SETTINGS_NOT_READY')
        service.preferences['browser_blocks']['profile:Good']='用户手动标记账号不可用'
        service.preferences['credit_cooldowns']={'reserved@example.test':{'account':'reserved@example.test','balance':5,'at':time.time(),'until':time.time()+3600}}
        before=copy.deepcopy(service.preferences.get('credit_cooldowns',{}))
        lifecycle.watch(service);self.confirm(service,bad);service._disconnected(bad['socket'])
        lifecycle.watch(service)
        self.assertEqual(service.preferences['browser_blocks']['profile:Good'],'用户手动标记账号不可用')
        self.assertEqual(service.preferences.get('credit_cooldowns',{}),before)

    def test_paid_work_prevents_closing_but_does_not_hold_healthy_account(self):
        service,bad,good,row=self.pool()
        service.store.update(row['id'],'generating',remote={'profile':'Bad','upstream_task':'paid-original'})
        service.preferences=rotation.marked(service.preferences,bad,ERROR)
        self.assertFalse(lifecycle.watch(service));self.assertFalse(service.pending)

    def test_manually_reopened_verified_page_does_not_inherit_old_rotation_fault(self):
        service,bad,good,row=self.pool()
        defer_prepare(service,bad,row,ERROR,'FLOW_SETTINGS_NOT_READY')
        lifecycle.watch(service);self.confirm(service,bad);service._disconnected(bad['socket'])
        reopened=self.ready(service,'Bad');reopened['connected']=time.time()+1
        lifecycle.watch(service)
        self.assertFalse(rotation.is_fault('Bad',service.preferences))
        self.assertFalse(any(p['action']=='close_browser' for p in service.pending.values()))


class ManualOpenTests(unittest.TestCase):
    setUpClass=launch.LocalLaunchMutexTests.__dict__['setUpClass']
    setUp=launch.LocalLaunchMutexTests.setUp
    service=launch.LocalLaunchMutexTests.service
    browser=launch.LocalLaunchMutexTests.browser
    client=launch.LocalLaunchMutexTests.client
    jobs=launch.LocalLaunchMutexTests.jobs
    configured=launch.LocalLaunchMutexTests.configured
    page_request=launch.LocalLaunchMutexTests.page_request
    ui=launch.LocalLaunchMutexTests.ui
    create_dialog=launch.LocalLaunchMutexTests.create_dialog
    bound=launch.LocalLaunchMutexTests.bound

    def test_manual_cold_open_does_not_join_or_release_automatic_lock(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32,'upload_images')
        owner=ui_mutex.state(service)['owner']
        self.assertTrue(plugin.launch_browser('Profile 23',['https://flow.google.com/project/manual'],manual=True)[0])
        launcher.launch_profile.assert_called_once()
        self.assertNotIn('activate',launcher.launch_profile.call_args.kwargs)
        self.assertIs(ui_mutex.state(service)['owner'],owner)
        self.assertFalse(ui_mutex.state(service)['queue'])

    def test_manual_task_open_requests_only_focus_while_automatic_owner_stays(self):
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32,'upload_images')
        owner=ui_mutex.state(service)['owner']
        service.execute({'action':'open_browser','browser':clients[1]['id']})
        request=next(p for p in service.pending.values() if p['action']=='focus_window')
        self.assertTrue(request['manual_ui']);self.assertIs(service.ui_mutex['owner'],owner)

    def test_manual_windows_focus_validates_own_ticket_without_taking_other_lease(self):
        from app_plugins.builtin.flow_assistant.foreground import receive
        service,clients,plugin,launcher,_=self.bound()
        self.page_request(service,clients[0],'1'*32,'upload_images')
        owner=ui_mutex.state(service)['owner'];client=clients[1]
        client['capabilities'].append('native_foreground_v1')
        service.request(client,'focus_window',{'manual_ui':True})
        key,pending=next((k,p) for k,p in service.pending.items() if p['action']=='focus_window')
        message={'type':'foreground_request','request_id':key,'token':pending['foreground_token']}
        with patch('app_plugins.builtin.flow_assistant.foreground.native_activate',return_value='focused') as activate:
            receive(service,client,{**message,'token':'0'*32});activate.assert_not_called()
            receive(service,client,message);activate.assert_called_once()
        self.assertIs(ui_mutex.state(service)['owner'],owner)

    def test_waiting_note_is_replaced_when_start_finishes(self):
        from app_plugins.builtin.flow_assistant.launch_ui import WAIT_NOTE
        dialog,_,_=self.ui();gate=dialog.plugin.launch_ui
        dialog.note.setText(WAIT_NOTE);gate.finish()
        self.assertNotEqual(dialog.note.text(),WAIT_NOTE)
        dialog.note.setText('specific error');gate.finish();self.assertEqual(dialog.note.text(),'specific error')

    def test_immediate_grant_does_not_write_waiting_note_after_response(self):
        from types import SimpleNamespace
        from qt_compat import QtWidgets
        from app_plugins.builtin.flow_assistant.launch_ui import WAIT_NOTE,START_NOTE
        service,clients,plugin,launcher,_=self.bound()
        plugin.dialog=SimpleNamespace(note=QtWidgets.QLabel())
        try:
            plugin.launch_browser('Profile 23',['https://flow.google.com/project/manual'])
            self.assertEqual(plugin.dialog.note.text(),START_NOTE)
            plugin.launch_ui.finish();self.assertNotEqual(plugin.dialog.note.text(),WAIT_NOTE)
        finally:plugin.dialog=None


if __name__=='__main__':unittest.main()
