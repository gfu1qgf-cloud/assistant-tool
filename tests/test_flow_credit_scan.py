"""Serial high-credit inventory with real policy/storage and simulated Chrome."""
import copy,time,unittest
from unittest.mock import patch
import test_flow_browser_lifecycle as fixtures
from app_plugins.builtin.flow_assistant import credit_scan as scan
from app_plugins.builtin.flow_assistant.high_credit import with_marker
from app_plugins.builtin.flow_assistant.runtime_close import PAUSED,CAPABILITY
from app_plugins.builtin.flow_assistant.browser_slots import live_starting_profiles


class CreditScanTests(unittest.TestCase):
    setUpClass=fixtures.BrowserLifecycleTests.__dict__['setUpClass']
    setUp=fixtures.BrowserLifecycleTests.setUp
    service=fixtures.BrowserLifecycleTests.service
    browser=fixtures.BrowserLifecycleTests.browser
    client=fixtures.BrowserLifecycleTests.client
    jobs=fixtures.BrowserLifecycleTests.jobs
    ready=fixtures.BrowserLifecycleTests.ready

    def account(self,service,profile='High',points=1000,permission='auto'):
        client=self.ready(service,profile,points)
        service.preferences=with_marker(service.preferences,client)
        service.preferences['high_credit_accounts'][client['account']]['permission']=permission
        return client

    def begin(self,service):
        service.timer.stop();scan.start(service);scan.watch(service)
        return service.credit_scan['entries'][0]

    def respond(self,service,client,points=700,**extra):
        key=next(k for k,p in service.pending.items() if p['client']==client['id'] and p['action']=='get_points')
        service._response(client,{'request_id':key,'success':True,'data':{'email':client['account'],'remaining_points':points},**extra})

    def test_online_inventory_updates_real_balance_without_changing_permission_or_closing_user_window(self):
        service=self.service();client=self.account(service,permission='deny')
        permissions=copy.deepcopy(service.preferences['high_credit_accounts'])
        entry=self.begin(service);self.assertEqual(entry['phase'],'reading')
        self.assertFalse(service.request(client,'get_points'),'automatic probe cannot race the scan')
        self.respond(service,client,640);scan.watch(service);scan.watch(service)
        self.assertEqual(entry['credits'],640);self.assertEqual(entry['result'],'已刷新')
        self.assertFalse(service.credit_scan['running']);self.assertNotIn('credit_scan',client)
        self.assertEqual(service.preferences['high_credit_accounts'][client['account']]['permission'],'deny')
        self.assertEqual(service.preferences['credit_balances'][client['account']]['balance'],640)
        self.assertFalse(any(p['action'] in {'close_browser','run_tasks'} for p in service.pending.values()))

    def test_historical_downgraded_accounts_are_included_and_same_account_is_not_repeated(self):
        service=self.service();one=self.account(service);one['credits']=5
        service.preferences=with_marker(service.preferences,one)
        two=self.account(service,'Other');two['account']=one['account']
        ordinary=self.ready(service,'Ordinary',50)
        scan.start(service)
        self.assertEqual(len(service.credit_scan['entries']),1)
        self.assertEqual(service.preferences['high_credit_accounts'][one['account']]['classification'],'ordinary')

    def test_busy_and_manually_disabled_accounts_are_skipped_without_touching_jobs(self):
        service=self.service();one=self.account(service,'Busy');two=self.account(service,'Manual')
        service.preferences.setdefault('browser_blocks',{})['profile:Manual']=PAUSED
        service.store.enqueue(self.jobs(budget=0));row=service.store.rows()[0]
        service.store.update(row['id'],'generating',attempt='a'*32,browser=one['id'],remote={'account':one['account'],'profile':'Busy'})
        before=copy.deepcopy(service.store.rows());self.begin(service);scan.watch(service);scan.watch(service)
        self.assertTrue(all(e['result']=='跳过' for e in service.credit_scan['entries']))
        self.assertEqual(service.store.rows(),before);self.assertFalse(service.pending)

    def test_failed_read_does_not_claim_old_credit_is_fresh_and_next_account_runs(self):
        service=self.service();one=self.account(service,'First');two=self.account(service,'Second')
        first=self.begin(service);key=first['request']
        service._response(one,{'request_id':key,'success':False,'error':'账号菜单未响应'})
        scan.watch(service);scan.watch(service)
        self.assertEqual(first['result'],'失败');self.assertIsNone(first['credits'])
        self.assertEqual(scan.current(service)['profile'],'Second')
        self.assertEqual(scan.current(service)['phase'],'reading')

    def test_offline_open_is_serial_and_counts_physical_windows_without_consuming_task_startup_lane(self):
        service=self.service();one=self.account(service,'Offline');two=self.account(service,'Online')
        # Preserve a known high account after disconnection.
        service._disconnected(one['socket']);opened=[];service.credit_scan_launch_requested.connect(opened.append)
        from app_plugins.builtin.flow_assistant.browser_slots import closed
        closed(service,'Offline')  # Fixture supplies an explicit native-exit receipt.
        scan.start(service)
        # Connected profiles are checked first.
        scan.watch(service);self.respond(service,two);scan.watch(service);scan.watch(service)
        entry=scan.current(service)
        self.assertEqual(entry['profile'],'Offline');self.assertEqual(len(opened),1)
        self.assertIn('Offline',service.preferences['browser_slots'])
        self.assertNotIn('Offline',live_starting_profiles(service))
        scan.launch_result(service,{**opened[0],'opened':False,'error':'启动失败'})
        self.assertNotIn('Offline',service.preferences['browser_slots']);self.assertEqual(entry['result'],'失败')

    def test_temporary_scan_window_close_does_not_add_a_manual_disable(self):
        service=self.service();client=self.account(service,'Temporary');self.begin(service)
        entry=scan.current(service);entry['opened']=True
        self.respond(service,client,420);scan.watch(service)
        self.assertEqual(entry['phase'],'closing');self.assertIn(CAPABILITY,client['capabilities'])
        self.assertNotEqual(service.preferences.get('browser_blocks',{}).get('profile:Temporary'),PAUSED)
        key=next(k for k,p in service.pending.items() if p['action']=='close_browser')
        service._response(client,{'request_id':key,'success':True,'data':{'closed':True}})
        scan.watch(service);self.assertEqual(entry['phase'],'finished')
        self.assertEqual(entry['credits'],420)

    def test_personal_tabs_prevent_close_but_do_not_lose_balance_or_block_the_rest(self):
        service=self.service();client=self.account(service);self.begin(service)
        entry=scan.current(service);entry['opened']=True
        self.respond(service,client,800);scan.watch(service)
        key=next(k for k,p in service.pending.items() if p['action']=='close_browser')
        service._response(client,{'request_id':key,'success':False,'error':'存在个人标签页，保留窗口'})
        scan.watch(service);scan.watch(service)
        self.assertEqual(entry['phase'],'finished');self.assertEqual(entry['credits'],800)
        self.assertIn('个人标签页',entry['note']);self.assertFalse(service.credit_scan['running'])

    def test_cancel_keeps_inflight_read_protected_and_never_starts_next_profile(self):
        service=self.service();one=self.account(service,'One');self.account(service,'Two')
        first=self.begin(service);scan.cancel(service);scan.watch(service)
        self.assertIn('credit_scan',one)
        self.respond(service,one);scan.watch(service);scan.watch(service)
        self.assertFalse(service.credit_scan['running'])
        self.assertEqual(service.credit_scan['entries'][1]['result'],'已停止')
        self.assertFalse(service.pending);self.assertNotIn('credit_scan',one)

    def test_unconnected_launch_timeout_records_failure_and_retains_unconfirmed_native_window(self):
        service=self.service();client=self.account(service);service._disconnected(client['socket'])
        entry=self.begin(service)
        with patch('time.time',return_value=entry['deadline']+1):scan.watch(service);scan.watch(service)
        self.assertFalse(service.credit_scan['running']);self.assertEqual(entry['result'],'失败')
        self.assertIn(client['profile'],service.preferences['browser_slots'])

    def test_new_connection_uses_existing_credit_protocol_and_stays_reserved_until_close_receipt(self):
        service=self.service();old=self.account(service,'Reopen',permission='deny');email=old['account']
        service._disconnected(old['socket'])
        from app_plugins.builtin.flow_assistant.browser_slots import closed
        closed(service,'Reopen');entry=self.begin(service)
        new=self.ready(service,'Reopen');new['account']=email
        scan.attach(service,new);scan.watch(service)
        self.assertEqual(entry['phase'],'reading');self.assertTrue(new['credit_observation_only'])
        self.respond(service,new,320);scan.watch(service)
        self.assertEqual(entry['phase'],'closing');self.assertIn('credit_scan',new)
        self.assertEqual(service.preferences['high_credit_accounts'][email]['permission'],'deny')
        key=next(k for k,p in service.pending.items() if p['action']=='close_browser')
        service._response(new,{'request_id':key,'success':True,'data':{'closed':True}});scan.watch(service)
        self.assertNotIn('credit_scan',new);self.assertNotIn('Reopen',service.preferences['browser_slots'])

    def test_full_window_budget_skips_more_opens_with_a_visible_reason(self):
        service=self.service();old=self.account(service);service._disconnected(old['socket'])
        service.preferences['browser_slots']={f'Busy {i}':{'state':'online'} for i in range(8)}
        opened=[];service.credit_scan_launch_requested.connect(opened.append)
        entry=self.begin(service);scan.watch(service)
        self.assertEqual(entry['result'],'跳过');self.assertIn('上限',entry['note']);self.assertFalse(opened)

    def test_credit_request_timeout_finishes_scan_instead_of_repeating_forever(self):
        service=self.service();client=self.account(service);entry=self.begin(service)
        with patch('time.time',return_value=entry['deadline']+1):service._tick();scan.watch(service)
        self.assertEqual(entry['result'],'失败');self.assertIn('超时',entry['note'])
        self.assertFalse(service.credit_scan['running']);self.assertNotIn('credit_scan',client)

    def test_cancelled_gui_launch_cannot_open_after_automatic_upload_releases_lock(self):
        import test_flow_launch_ui_mutex as launch
        launch.LocalLaunchMutexTests.setUpClass();case=launch.LocalLaunchMutexTests();case.setUp()
        try:
            service,clients,plugin,launcher,_=case.bound()
            case.page_request(service,clients[0],'1'*32,'upload_images')
            report={'id':'scan','running':True,'entries':[{'profile':'Profile 23','phase':'starting'}]}
            plugin.snapshot['credit_scan']=report
            plugin._launch_credit_scan({'scan_id':'scan','profile':'Profile 23','project':launch.setup.PROJECT})
            launcher.launch_profile.assert_not_called()
            report['cancelled']=True;case.release(service,clients[0])
            launcher.launch_profile.assert_not_called()
        finally:case.doCleanups()

    def test_unconfirmed_temporary_close_keeps_only_that_profile_blocked(self):
        from app_plugins.builtin.flow_assistant.runtime_close import SCAN_CLOSING
        from app_plugins.builtin.flow_assistant.policy import block_for
        service=self.service();client=self.account(service);self.begin(service)
        entry=scan.current(service);entry['opened']=True
        self.respond(service,client);scan.watch(service)
        with patch('time.time',return_value=entry['deadline']+1):service._tick();scan.watch(service)
        self.assertEqual(entry['phase'],'finished')
        self.assertEqual(block_for(client,service.preferences),SCAN_CLOSING)
        self.assertNotEqual(SCAN_CLOSING,PAUSED)

    def test_existing_disconnected_window_is_neither_freed_on_launch_failure_nor_auto_closed_after_read(self):
        service=self.service();old=self.account(service,'Existing');email=old['account']
        service.preferences['profiles']=['Existing']
        service._disconnected(old['socket']);prior=copy.deepcopy(service.preferences['browser_slots']['Existing'])
        entry=self.begin(service)
        scan.launch_result(service,{'scan_id':service.credit_scan['id'],'profile':'Existing','opened':False,'error':'未启动'})
        self.assertEqual(service.preferences['browser_slots']['Existing'],prior)
        scan.watch(service);scan.start(service);scan.watch(service)
        new=self.ready(service,'Existing');new['account']=email;scan.attach(service,new);scan.watch(service)
        self.respond(service,new,150);scan.watch(service)
        self.assertEqual(scan.current(service),None)
        self.assertFalse(any(p['action']=='close_browser' for p in service.pending.values()))

    def test_late_never_opened_receipt_releases_its_own_reservation_after_report_restart(self):
        from app_plugins.builtin.flow_assistant.browser_slots import closed
        service=self.service();old=self.account(service);service._disconnected(old['socket']);closed(service,old['profile'])
        entry=self.begin(service);run=service.credit_scan['id']
        with patch('time.time',return_value=entry['deadline']+1):scan.watch(service);scan.watch(service)
        scan.start(service)
        scan.launch_result(service,{'scan_id':run,'profile':old['profile'],'opened':False,'error':'已取消'})
        self.assertNotIn(old['profile'],service.preferences['browser_slots'])


class CreditScanUiTests(unittest.TestCase):
    def test_controls_filter_and_open_use_original_profile_identity(self):
        import test_flow_setup as setup
        from app_plugins.builtin.flow_assistant.credit_scan_ui import CreditScanDialog
        setup.SetupTests.setUpClass();case=setup.SetupTests();case.setUp()
        try:
            owner,_,commands=case.ui();owner.open_credit_scan();dialog=owner.credit_scan_dialog
            dialog.start.click();self.assertEqual(commands[-1]['action'],'start_credit_scan')
            dialog.apply({'running':True,'entries':[
                {'profile':'Profile 23','account':'high@example.test','credits':600,'phase':'finished','result':'已刷新','note':''},
                {'profile':'Profile 24','account':'low@example.test','credits':5,'phase':'finished','result':'已刷新','note':''}]})
            self.assertFalse(dialog.start.isEnabled());self.assertTrue(dialog.stop.isEnabled())
            dialog.funded.setChecked(True);self.assertEqual(dialog.table.rowCount(),1)
            dialog.table.setCurrentCell(0,0);dialog.open_selected()
            self.assertEqual(commands[-1],{'action':'open_browser','profile':'Profile 23'})
        finally:case.doCleanups()


if __name__=='__main__':unittest.main()
