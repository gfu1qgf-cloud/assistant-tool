"""Actual idle reload tickets, bounded fallback, and independent focus controls."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
import test_flow_launch_ui_mutex as ui_fixture
from app_plugins.builtin.flow_assistant import bridge_recovery as recovery, browser_lifecycle, ui_mutex
from app_plugins.builtin.flow_assistant.foreground import receive, description
from app_plugins.builtin.flow_assistant.policy import capable
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION


class AutoBridgeTests(unittest.TestCase):
    setUpClass=fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp=fixtures.CreditCooldownTests.setUp
    service=fixtures.CreditCooldownTests.service
    browser=fixtures.CreditCooldownTests.browser
    client=fixtures.CreditCooldownTests.client
    jobs=fixtures.CreditCooldownTests.jobs

    def configured(self):
        service=self.service();service.timer.stop();service.paused=False
        client=self.browser(service,'Profile 23')
        client.update(extension_version='1.1.45',capabilities=['ui_mutex_v1'],connected=time.time()-10)
        service.preferences.update(profiles=[client['profile']],browser_options={'focus_before_action':False})
        service.store.enqueue(self.jobs(duration=10,budget=0))
        return service,client

    def test_idle_old_bridge_gets_reload_ticket_and_cannot_take_paid_work_until_confirmed(self):
        service,client=self.configured();results=[];service.setup_ready.connect(results.append)
        before=copy.deepcopy(service.store.rows())
        self.assertTrue(recovery.watch(service))
        result=results[-1];entry=service.preferences['bridge_recoveries'][client['profile']]
        self.assertTrue(result['success']);self.assertIn('lzx_action=options',result['project_claim_url'])
        self.assertEqual(service.setup_requests[client['profile']]['mode'],'reload_only')
        self.assertFalse(capable(client,self.jobs(duration=10)[0],service.preferences,15))
        recovery.launch_result(service,{'profile':client['profile'],'request_id':entry['request'],'opened':True})
        self.assertEqual(service.store.rows(),before)
        client['extension_version']=EXTENSION_VERSION
        self.assertTrue(recovery.watch(service))
        self.assertTrue(capable(client,self.jobs(duration=10)[0],service.preferences,15))
        self.assertEqual(service.preferences['bridge_recoveries'][client['profile']]['state'],'done')

    def test_paused_manual_observation_disabled_and_fresh_connections_are_left_alone(self):
        for variant in ('paused','observation','disabled','fresh','current','future','unselected','blocked'):
            with self.subTest(variant=variant):
                self.setUp();service,client=self.configured()
                if variant=='paused':service.paused=True
                elif variant=='observation':client['credit_observation_only']=True
                elif variant=='disabled':service.preferences['browser_options']['auto_bridge_recovery']=False
                elif variant=='fresh':client['connected']=time.time()
                elif variant=='current':client['extension_version']=EXTENSION_VERSION
                elif variant=='future':client['extension_version']='1.1.999'
                elif variant=='unselected':service.preferences['profiles']=[]
                else:service.preferences['browser_blocks']={'profile:'+client['profile']:'手动不可用'}
                self.assertFalse(recovery.watch(service))
                self.assertFalse(service.preferences.get('bridge_recoveries'))

    def test_upload_generation_unknown_results_downloads_and_reset_holds_prevent_reload(self):
        for phase in ('uploading','generating','uncertain','download_failed','native','pending','reset'):
            with self.subTest(phase=phase):
                self.setUp();service,client=self.configured()
                row=service.store.rows()[0]
                if phase=='native':client['native_busy']=True
                elif phase=='pending':service.request(client,'snapshot')
                elif phase=='reset':client['reset_wait_until']=time.time()+30
                else:service.store.update(row['id'],phase,browser=client['id'],remote={'profile':client['profile']})
                self.assertFalse(recovery.watch(service))
                self.assertFalse(service.preferences.get('bridge_recoveries'))

    def test_ui_queue_timeout_does_not_spend_a_reload_or_restart_a_browser(self):
        service,client=self.configured();recovery.watch(service)
        entry=service.preferences['bridge_recoveries'][client['profile']]
        with patch('time.time',return_value=entry['deadline']+1),patch('app_plugins.builtin.flow_assistant.runtime_close.request_close') as close:
            recovery.watch(service);close.assert_not_called()
        entry=service.preferences['bridge_recoveries'][client['profile']]
        self.assertEqual(entry['state'],'waiting');self.assertEqual(entry['count'],0)

    def test_two_confirmed_open_attempts_then_only_one_protected_restart(self):
        service,client=self.configured();now=time.time()
        for index in range(2):
            with patch('time.time',return_value=now+index*31):
                recovery.watch(service);entry=service.preferences['bridge_recoveries'][client['profile']]
                recovery.launch_result(service,{'profile':client['profile'],'request_id':entry['request'],'opened':True})
        with patch('time.time',return_value=now+63),patch('app_plugins.builtin.flow_assistant.runtime_close.request_close',return_value=True) as close:
            recovery.watch(service);recovery.watch(service)
            close.assert_called_once_with(service,client,automatic_reason='bridge_update')
        self.assertEqual(service.preferences['bridge_recoveries'][client['profile']]['state'],'restarting')

    def test_no_close_capability_keeps_window_and_explains_what_prevented_restart(self):
        service,client=self.configured()
        recovery.save(service,client['profile'],{'target':EXTENSION_VERSION,'state':'awaiting','count':2,'deadline':1})
        recovery.watch(service)
        entry=service.preferences['bridge_recoveries'][client['profile']]
        self.assertEqual(entry['state'],'stopped');self.assertIn('保护其他标签页',entry['error'])

    def test_reload_callback_from_an_old_ticket_cannot_change_current_recovery(self):
        service,client=self.configured();recovery.watch(service)
        before=copy.deepcopy(service.preferences['bridge_recoveries'])
        recovery.launch_result(service,{'profile':client['profile'],'request_id':'old','opened':True})
        self.assertEqual(service.preferences['bridge_recoveries'],before)

    def test_delayed_reload_authorization_rechecks_switch_and_same_account_work(self):
        from app_plugins.builtin.flow_assistant.setup_pairing import check_reload
        service,client=self.configured();recovery.watch(service)
        setup=service.setup_requests[client['profile']]
        transport={'socket':client['socket']}
        self.assertTrue(check_reload(service,transport,{'ticket':setup['id']}))
        service.preferences['browser_options']['auto_bridge_recovery']=False
        self.assertFalse(check_reload(service,transport,{'ticket':setup['id']}))
        self.assertFalse(recovery.holding(client,service.preferences))
        service.preferences['browser_options']['auto_bridge_recovery']=True
        other=self.browser(service,'Another profile');other['account']=client['account']
        row=service.store.rows()[0]
        service.store.update(row['id'],'generating',browser=other['id'],remote={'account':client['account']})
        self.assertFalse(check_reload(service,transport,{'ticket':setup['id']}))

    def test_bridge_restart_requeues_only_after_confirmed_safe_close(self):
        service,client=self.configured()
        browser_lifecycle.reserve(service,client,'bridge_update')
        self.assertEqual(service.preferences['browser_lifecycle'][client['profile']]['state'],'closing')
        browser_lifecycle.finish(service,client,True)
        entry=service.preferences['browser_lifecycle'][client['profile']]
        self.assertAlmostEqual(entry['reopen_at']-time.time(),5,delta=1)
        with patch('time.time',return_value=entry['reopen_at']+1):browser_lifecycle.watch(service)
        self.assertEqual(service.preferences['browser_lifecycle'][client['profile']]['state'],'eligible')


class AutoBridgeGuiTests(unittest.TestCase):
    setUpClass=ui_fixture.LocalLaunchMutexTests.__dict__['setUpClass']
    setUp=ui_fixture.LocalLaunchMutexTests.setUp
    service=ui_fixture.LocalLaunchMutexTests.service
    browser=ui_fixture.LocalLaunchMutexTests.browser
    client=ui_fixture.LocalLaunchMutexTests.client
    jobs=ui_fixture.LocalLaunchMutexTests.jobs
    configured=ui_fixture.LocalLaunchMutexTests.configured
    page_request=ui_fixture.LocalLaunchMutexTests.page_request
    ui=ui_fixture.LocalLaunchMutexTests.ui
    bound=ui_fixture.LocalLaunchMutexTests.bound

    def test_foreground_off_keeps_mutex_but_automatic_windows_activation_is_suppressed(self):
        service,clients,_,_,_=self.bound();client=clients[0]
        service.preferences['browser_options']['focus_before_action']=False
        self.page_request(service,client,'1'*32,'prepare')
        with patch('app_plugins.builtin.flow_assistant.foreground.native_activate') as activate:
            receive(service,client,{'type':'foreground_request','request_id':'1'*32,'token':'1'*32,'window_id':10})
            activate.assert_not_called()
        self.assertEqual(client['foreground']['state'],'disabled')
        self.assertEqual(ui_mutex.state(service)['owner']['client'],client['id'])
        self.assertIn('已关闭',description(client,service.preferences))

    def test_explicit_manual_focus_still_works_when_automatic_focus_is_off(self):
        service,clients,_,_,_=self.bound();client=clients[0]
        service.preferences['browser_options']['focus_before_action']=False
        self.page_request(service,client,'1'*32,'focus_window')
        with patch('app_plugins.builtin.flow_assistant.foreground.native_activate',return_value='focused') as activate:
            receive(service,client,{'type':'foreground_request','request_id':'1'*32,'token':'1'*32,'window_id':10})
            activate.assert_called_once()

    def test_flow_launch_requests_background_start_when_automatic_focus_is_off(self):
        _,_,plugin,launcher,_=self.bound()
        plugin.snapshot['preferences']['browser_options']={'ui_mutex':False,'focus_before_action':False}
        plugin.launch_browser('Profile 23',['https://flow.google.com/project/reload'])
        self.assertFalse(launcher.launch_profile.call_args.kwargs['activate'])

    def test_native_background_start_does_not_change_manual_chrome_launch_defaults(self):
        from types import SimpleNamespace
        from PYUI.chrome_runner_pyui import ChromeRunnerDialog
        launcher=SimpleNamespace(chrome_path='chrome.exe')
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            self.assertTrue(ChromeRunnerDialog.launch_profile(launcher,{'directory':'Profile 23'},
                urls=['https://flow.google.com/project/background'],activate=False)[0])
            self.assertIn('--start-minimized',popen.call_args.args[0])
            info=popen.call_args.kwargs.get('startupinfo')
            if info is not None:self.assertEqual(info.wShowWindow,7)
            self.assertTrue(ChromeRunnerDialog.launch_profile(launcher,{'directory':'Profile 23'},
                urls=['https://example.test'])[0])
            self.assertIn('--start-maximized',popen.call_args.args[0])
            self.assertNotIn('startupinfo',popen.call_args.kwargs)

    def test_automatic_setup_result_opens_bound_reload_entry_and_waits_for_version(self):
        service,_,plugin,launcher,_=self.bound()
        service.paused=False
        service.preferences['browser_options']={'ui_mutex':False,'focus_before_action':False}
        old=self.browser(service,'Profile 23')
        old.update(extension_version='1.1.45',connected=time.time()-10,capabilities=['ui_mutex_v1'])
        service.preferences['profiles']=['Profile 23']
        service.store.enqueue(self.jobs(duration=10,budget=0))
        service.changed.connect(lambda state:setattr(plugin,'snapshot',state))
        service.setup_ready.connect(plugin._setup_ready)
        self.assertTrue(recovery.watch(service))
        self.assertEqual(launcher.launch_profile.call_count,1)
        from app_plugins.builtin.flow_assistant.setup_support import OPTIONS_URL
        self.assertTrue(launcher.launch_profile.call_args.kwargs['urls'][0].startswith(OPTIONS_URL+'#setup='))
        self.assertFalse(any(url.startswith('https://') for url in launcher.launch_profile.call_args.kwargs['urls']))
        self.assertFalse(launcher.launch_profile.call_args.kwargs['activate'])
        self.assertEqual(service.preferences['bridge_recoveries']['Profile 23']['state'],'awaiting')
        old['extension_version']=EXTENSION_VERSION
        recovery.watch(service)
        self.assertEqual(service.preferences['bridge_recoveries']['Profile 23']['state'],'done')


if __name__=='__main__':unittest.main()
