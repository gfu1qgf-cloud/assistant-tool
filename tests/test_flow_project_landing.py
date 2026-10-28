"""Cold project launch and recovery from an extension-management landing page."""
import time,unittest
from unittest.mock import patch
import test_flow_project_startup as fixtures
from PYUI.chrome_runner_pyui import ChromeRunnerDialog
from app_plugins.builtin.flow_assistant import project_startup,tab_reuse


class ProjectLandingTests(unittest.TestCase):
    setUpClass=fixtures.ProjectStartupTests.__dict__['setUpClass']
    setUp=fixtures.ProjectStartupTests.setUp
    service=fixtures.ProjectStartupTests.service
    jobs=fixtures.ProjectStartupTests.jobs
    client=fixtures.ProjectStartupTests.client
    browser=fixtures.ProjectStartupTests.browser
    ui=fixtures.ProjectStartupTests.ui
    cold=fixtures.ProjectStartupTests.cold

    def host(self):
        service,client=self.cold();service.timer.stop();service.preferences['browser_options']={'ui_mutex':False}
        opens=[];snapshots=[];service.project_open_requested.connect(opens.append);service.changed.connect(snapshots.append)
        self.assertTrue(project_startup.note_missing(service,client,project_startup.MISSING_PROJECT))
        dialog,launcher,commands=self.ui();plugin=dialog.plugin;plugin.snapshot=snapshots[-1]
        plugin.command.connect(service.execute);service.tab_reused.connect(plugin._tab_reused)
        launcher.chrome_path='chrome.exe'
        launcher.launch_profile=lambda *a,**kw:ChromeRunnerDialog.launch_profile(launcher,*a,**kw)
        return service,client,plugin,launcher,opens,commands

    def test_extension_landing_uses_one_original_https_open_after_verified_missing_tabs(self):
        service,client,plugin,launcher,opens,commands=self.host()
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            plugin._open_missing_project(opens[0]);self.assertEqual(popen.call_count,0)
            key=next(k for k,p in service.pending.items() if p.get('tab_reuse_request'))
            service._response(client,{'request_id':key,'success':False,'error':tab_reuse.NO_SAFE_PROJECT_TAB})
            self.assertEqual(popen.call_count,1)
            args=popen.call_args.args[0];self.assertIn(client['saved_project'],args)
            self.assertIn('--profile-directory='+client['profile'],args)
            self.assertFalse(any('options.html' in arg or 'chrome://extensions' in arg for arg in args))
            service._response(client,{'request_id':key,'success':False,'error':tab_reuse.NO_SAFE_PROJECT_TAB})
            self.assertEqual(popen.call_count,1,'late reply cannot open another page')
        self.assertTrue(commands[-1]['opened']);self.assertFalse(client['page_ready'])
        self.assertIn('尚未确认页面就绪',client['connection_recovery']['message'])
        service._snapshot(client,{'url':client['saved_project'],'controls_ready':True,'running':False})
        self.assertFalse(client.get('project_missing'));self.assertFalse(project_startup.watch(service))

    def test_cold_start_real_command_contains_original_https_project(self):
        service,client,plugin,launcher,opens,commands=self.host();plugin.snapshot['clients']=[]
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            plugin.launch_browser(client['profile'],[client['saved_project']],bootstrap_existing=True)
            self.assertEqual(popen.call_count,1)
            args=popen.call_args.args[0]
            self.assertIn(client['saved_project'],args);self.assertIn('--profile-directory='+client['profile'],args)
            self.assertFalse(any(arg.startswith('chrome-extension://') for arg in args))

    def test_paid_work_arriving_during_missing_tab_query_prevents_native_fallback(self):
        service,client,plugin,launcher,opens,commands=self.host()
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            plugin._open_missing_project(opens[0]);key=next(k for k,p in service.pending.items() if p.get('tab_reuse_request'))
            row=service.store.rows()[0];service.store.update(row['id'],'generating',browser=client['id'],remote={'profile':client['profile']})
            service._response(client,{'request_id':key,'success':False,'error':tab_reuse.NO_SAFE_PROJECT_TAB})
            popen.assert_not_called();self.assertFalse(commands[-1]['opened'])

    def test_generic_page_or_socket_failure_cannot_authorize_new_project_tab(self):
        for error in ('No current window','原项目仍有未确认提交','连接超时'):
            self.setUp();service,client,plugin,launcher,opens,commands=self.host()
            with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
                plugin._open_missing_project(opens[0]);key=next(k for k,p in service.pending.items() if p.get('tab_reuse_request'))
                service._response(client,{'request_id':key,'success':False,'error':error})
                popen.assert_not_called()

    def test_command_success_without_page_response_gets_bounded_confirmation_retries(self):
        service,client=self.cold();service.timer.stop();opens=[];service.project_open_requested.connect(opens.append)
        start=time.time()
        with patch('time.time',return_value=start):
            project_startup.note_missing(service,client,project_startup.MISSING_PROJECT)
            service.execute({**opens[-1],'opened':True})
        for index in (1,2):
            with patch('time.time',return_value=start+index*9):
                self.assertTrue(project_startup.watch(service));service.execute({**opens[-1],'opened':True})
        with patch('time.time',return_value=start+28):self.assertFalse(project_startup.watch(service))
        self.assertEqual(len(opens),3);self.assertEqual(client['connection_recovery']['state'],'stopped')
        self.assertTrue(all(r['status']=='queued' for r in service.store.rows()))


if __name__=='__main__':unittest.main()
