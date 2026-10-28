"""UI queue expiry before process creation must never poison a funded profile."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_browser_slots as slots_fixture
import test_flow_launch_ui_mutex as ui_fixture
import test_flow_setup as setup
from app_plugins.builtin.flow_assistant import browser_slots, launch_retries, ui_mutex
from app_plugins.builtin.flow_assistant.policy import waiting_reason

TIMEOUT='启动浏览器等待UI互斥超过 90 秒，已跳过此配置；任务保留，继续其他账号。'
EXPIRED='启动浏览器的UI互斥等待超时或已取消；已跳过此配置，任务保留原队列。'


class UiLaunchRetryTests(unittest.TestCase):
    setUpClass=slots_fixture.BrowserSlotsTests.__dict__['setUpClass']
    setUp=slots_fixture.BrowserSlotsTests.setUp
    service=slots_fixture.BrowserSlotsTests.service
    configured=slots_fixture.BrowserSlotsTests.configured
    browser=slots_fixture.BrowserSlotsTests.browser
    client=slots_fixture.BrowserSlotsTests.client
    jobs=slots_fixture.BrowserSlotsTests.jobs

    def failed_ui(self,service,profile,**extra):
        browser_slots.launch_result(service,{'profile':profile,'opened':False,'error':TIMEOUT,**extra})

    def test_timeout_releases_reservations_then_becomes_startable_without_user_action(self):
        service,profiles=self.configured();profile=profiles[0]
        with patch('time.time',return_value=1000):
            self.assertTrue(browser_slots.request_start(service,profile))
            service.small_balance_probes[profile]=1090
            self.failed_ui(service,profile)
            self.assertFalse(browser_slots.can_start(service,profile))
            self.assertTrue(browser_slots.can_start(service,profiles[1]),'other profiles remain available')
        self.assertNotIn(profile,service.preferences['browser_slots'])
        self.assertNotIn(profile,service.preferences.get('browser_launch_failures',{}))
        self.assertNotIn(profile,service.launch_attempts)
        self.assertNotIn(profile,service.launched)
        self.assertNotIn(profile,service.launching)
        self.assertNotIn(profile,service.small_balance_probes)
        self.assertEqual(service.preferences['browser_ui_waits'][profile]['next_at'],1005)
        with patch('time.time',return_value=1005):
            self.assertTrue(browser_slots.request_start(service,profile))

    def test_repeated_ui_contention_never_becomes_account_failure(self):
        service,profiles=self.configured();profile=profiles[0]
        for index in range(5):
            with patch('time.time',return_value=1000+index*5):
                self.assertTrue(browser_slots.request_start(service,profile))
                self.failed_ui(service,profile,kind=launch_retries.KIND)
                self.assertNotIn(profile,service.preferences.get('browser_launch_failures',{}))
        self.assertEqual(service.preferences['browser_ui_waits'][profile]['count'],5)

    def test_backoff_survives_restart_and_expires_without_manual_resume(self):
        service,profiles=self.configured();profile=profiles[0]
        with patch('time.time',return_value=time.time()+100):
            browser_slots.request_start(service,profile);self.failed_ui(service,profile)
            due=service.preferences['browser_ui_waits'][profile]['next_at']
        restarted=self.service()
        with patch('time.time',return_value=due-1):self.assertFalse(browser_slots.can_start(restarted,profile))
        with patch('time.time',return_value=due):self.assertTrue(browser_slots.can_start(restarted,profile))

    def test_explicit_cancel_is_not_an_automatic_retry_and_actual_failures_stay_blocked(self):
        service,profiles=self.configured();cancelled,broken=profiles[:2]
        browser_slots.request_start(service,cancelled)
        self.failed_ui(service,cancelled,cancelled=True)
        self.assertTrue(browser_slots.can_start(service,cancelled))
        self.assertNotIn(cancelled,service.preferences.get('browser_ui_waits',{}))
        browser_slots.request_start(service,broken)
        self.failed_ui(service,broken,error='Chrome 可执行文件不存在')
        self.assertFalse(browser_slots.can_start(service,broken))
        self.assertNotIn(broken,service.preferences.get('browser_ui_waits',{}))
        self.assertEqual(service.preferences['browser_launch_failures'][broken]['kind'],'launch')

    def test_opened_or_already_online_receipts_cannot_release_a_browser(self):
        service,profiles=self.configured();profile=profiles[0]
        browser_slots.request_start(service,profile)
        self.failed_ui(service,profile,opened=True)
        self.assertIn('opened_at',service.preferences['browser_slots'][profile])
        self.assertNotIn(profile,service.preferences.get('browser_ui_waits',{}))
        self.failed_ui(service,profile)
        self.assertIn('opened_at',service.preferences['browser_slots'][profile])
        self.assertNotIn(profile,service.preferences.get('browser_ui_waits',{}))
        client=self.browser(service,profile);browser_slots.seen(service,client)
        self.failed_ui(service,profile)
        self.assertEqual(service.preferences['browser_slots'][profile]['state'],'online')

    def test_legacy_migration_only_removes_exact_prelaunch_ui_errors(self):
        preferences={'browser_launch_failures':{
            'ui':{'kind':'launch','message':TIMEOUT,'retryable':False},
            'expired':{'message':EXPIRED},
            'opened':{'kind':'launch','message':TIMEOUT},
            'real':{'kind':'launch','message':'Chrome 不存在'},
            'probe':{'kind':'extension_probe_v3','message':TIMEOUT},
            'similar':{'kind':'launch','message':'UI互斥超时，已启动窗口'},
        },'browser_slots':{'ui':{'state':'opening','at':100},
                          'opened':{'state':'opening','opened_at':101}},
           'account_blocks':{'ui':'手动禁用'},'credit_cooldowns':{'expired':{'until':9999}},
           'high_credit_accounts':{'ui':{'allowed':False}}}
        original=copy.deepcopy(preferences);recovered=launch_retries.recover_legacy(preferences)
        self.assertEqual(preferences,original,'migration is pure')
        self.assertEqual(set(recovered['browser_launch_failures']),{'opened','real','probe','similar'})
        self.assertNotIn('ui',recovered['browser_slots'])
        self.assertEqual(recovered['browser_slots']['opened'],preferences['browser_slots']['opened'])
        for key in ('account_blocks','credit_cooldowns','high_credit_accounts'):
            self.assertEqual(recovered[key],preferences[key])
        self.assertIs(launch_retries.recover_legacy(recovered),recovered,'migration is idempotent')

    def test_normal_service_start_clears_runtime_failures_and_preserves_user_marks(self):
        service,profiles=self.configured();recover,keep=profiles[:2]
        service.commit_preferences({**service.preferences,'browser_launch_failures':{
            recover:{'kind':'launch','message':EXPIRED},keep:{'kind':'launch','message':'找不到Chrome'}},
            'browser_slots':{recover:{'state':'opening','at':10}},
            'account_blocks':{'profile:'+keep:{'kind':'manual','reason':'用户手动标记账号不可用'}}})
        restarted=self.service()
        self.assertNotIn(recover,restarted.preferences['browser_launch_failures'])
        self.assertNotIn(recover,restarted.preferences['browser_slots'])
        self.assertNotIn(keep,restarted.preferences['browser_launch_failures'])
        self.assertEqual(restarted.preferences['account_blocks']['profile:'+keep]['kind'],'manual')
        self.assertEqual(restarted.store.settings()['browser_launch_failures'],restarted.preferences['browser_launch_failures'])

    def test_wait_reason_distinguishes_backup_retry_from_genuine_credit_shortage(self):
        service,profiles=self.configured();client=self.browser(service,profiles[0],points=10)
        row={'payload':self.jobs(duration=10,budget=0)[0],'status':'queued','remote':{},'id':'fixture'}
        def reason():return waiting_reason(row,[client],[],service.preferences,{},{})
        self.assertIn('积分不足以承担下一条任务',reason())
        browser_slots.request_start(service,profiles[1])
        self.assertIn('正在等待备用浏览器启动',reason())
        self.failed_ui(service,profiles[1])
        self.assertIn('自动重试',reason())
        self.assertIn(profiles[1],reason())

    def test_dispatch_refills_all_four_lanes_after_ui_backoff_without_resetting_jobs(self):
        service,profiles=self.configured();profiles=profiles[:4]
        service.preferences['profiles']=profiles
        service.store.enqueue(self.jobs(duration=10,count=8,budget=0));service.paused=False
        launches=[];service.launch_requested.connect(launches.append)
        start=time.time()
        with patch('time.time',return_value=start):
            service.fill_dispatch_slots()
            self.assertEqual(launches,profiles)
            for profile in profiles:self.failed_ui(service,profile)
            service.fill_dispatch_slots()
            self.assertEqual(launches,profiles,'no tight retry loop')
        with patch('time.time',return_value=start+5):service.fill_dispatch_slots()
        self.assertEqual(launches,profiles+profiles)
        self.assertTrue(all(row['status']=='queued' for row in service.store.rows()))
        self.assertFalse(service.preferences.get('browser_launch_failures'))


class UiLaunchCallbackTests(unittest.TestCase):
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

    def test_gui_timeout_restores_profile_without_touching_current_upload_or_replaying_a_launch(self):
        service,clients,plugin,launcher,_=self.bound()
        profile='Profile 23'
        self.page_request(service,clients[0],'1'*32,'upload_images')
        self.assertTrue(browser_slots.request_start(service,profile))
        plugin.snapshot['jobs']=[{'id':'queued-job','status':'queued'}]
        with patch.object(plugin,'_failed') as failure:
            plugin._launch_checked({'profile':profile,'status':'enabled','version':setup.EXTENSION_VERSION})
            since=plugin.launch_ui.active['ui_wait_started']
            before=copy.deepcopy(service.store.rows())
            token=plugin.launch_ui.active['ticket']
            with patch('time.time',return_value=since+91):ui_mutex.watch(service)
            failure.assert_not_called()
        launcher.launch_profile.assert_not_called()
        self.assertIsNone(plugin.launch_ui.active)
        self.assertFalse(service.ui_launch_waits)
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[0]['id'])
        self.assertEqual(service.store.rows(),before)
        self.assertNotIn(profile,service.preferences['browser_slots'])
        self.assertNotIn(profile,service.preferences.get('browser_launch_failures',{}))
        due=service.preferences['browser_ui_waits'][profile]['next_at']
        with patch('time.time',return_value=due):self.assertTrue(browser_slots.can_start(service,profile))
        plugin.launch_ui.result({'token':token,'state':'granted','expires_at':time.time()+30})
        launcher.launch_profile.assert_not_called()
        self.assertEqual(ui_mutex.state(service)['owner']['client'],clients[0]['id'])


if __name__=='__main__':unittest.main()
