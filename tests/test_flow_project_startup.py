"""Trace connected background -> missing project -> native original-profile open."""
import copy
import time
import unittest
from unittest.mock import patch

import test_flow_credit_cooldown as fixtures
from PYUI.chrome_runner_pyui import ChromeRunnerDialog
from app_plugins.builtin.flow_assistant.connection_recovery import repair_idle
from app_plugins.builtin.flow_assistant.project_startup import MISSING_PROJECT, note_missing, watch, launch_allowed
from app_plugins.builtin.flow_assistant.browser_slots import occupied_profiles


class ProjectStartupTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    ui = fixtures.setup_fixtures.SetupTests.ui

    def cold(self):
        service = self.service(); client = self.browser(service)
        project = fixtures.setup_fixtures.PROJECT
        client.update(saved_project=project, project=project, page_ready=False,
            connected=time.time()-100, page_at=time.time())
        service.preferences.update(profiles=[client['profile']], profile_projects={client['profile']:project})
        service.store.enqueue(self.jobs(duration=10, budget=0)); service.paused=False
        return service, client

    def missing_response(self, service, client):
        self.assertTrue(repair_idle(service,client))
        key=next(key for key,r in service.pending.items() if r['action']=='repair_project')
        service._response(client,{'request_id':key,'success':False,'error':MISSING_PROJECT})

    def test_connected_background_missing_project_actually_requests_original_browser_open(self):
        service,client=self.cold(); opens=[];service.project_open_requested.connect(opens.append)
        self.missing_response(service,client)
        self.assertEqual(len(opens),1)
        self.assertEqual(opens[0]['profile'],client['profile'])
        self.assertEqual(opens[0]['project'],client['saved_project'])
        self.assertEqual(service.store.rows()[0]['status'],'queued')
        self.assertEqual(occupied_profiles(service),{client['profile']})
        self.assertFalse(any(r['action']=='run_tasks' for r in service.pending.values()))

    def test_whole_host_to_gui_path_reuses_online_tab_without_native_launch(self):
        service,client=self.cold(); opens=[];snapshots=[]
        service.project_open_requested.connect(opens.append);service.changed.connect(snapshots.append)
        self.missing_response(service,client)
        dialog,launcher,commands=self.ui();plugin=dialog.plugin
        plugin.snapshot=snapshots[-1]
        plugin.command.connect(service.execute);service.tab_reused.connect(plugin._tab_reused)
        launcher.chrome_path='chrome.exe'
        launcher.launch_profile=lambda *args,**kwargs:ChromeRunnerDialog.launch_profile(launcher,*args,**kwargs)
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            plugin._open_missing_project(opens[0])
            plugin._open_missing_project(opens[0])
        self.assertEqual(popen.call_count,0)
        reuse=[(key,p) for key,p in service.pending.items() if p.get('tab_reuse_request')]
        self.assertEqual(len(reuse),1)
        self.assertEqual(reuse[0][1]['action'],'reuse_project_tab')
        service._response(client,{'request_id':reuse[0][0],'success':True,'data':{'tab_id':7,'reused':True}})
        self.assertTrue(commands[-1]['opened'])
        self.assertIn(client['profile'],occupied_profiles(service))

    def test_generic_unready_page_never_triggers_native_reopen(self):
        service,client=self.cold();opens=[];service.project_open_requested.connect(opens.append)
        self.assertFalse(note_missing(service,client,'页面控件还未就绪','FLOW_PAGE_NOT_READY'))
        self.assertFalse(opens);self.assertFalse(client.get('project_missing'))

    def test_no_connected_browser_reopens_original_reserved_profile_after_restart(self):
        service,client=self.cold();profile=client['profile'];project=client['saved_project']
        service.clients.clear()
        service.preferences['browser_slots']={p:{'state':'online','at':time.time()-10000} for p in
            (profile,'Profile 24','Profile 25','Profile 26')}
        opens=[];snapshots=[]
        service.launch_requested.connect(opens.append);service.changed.connect(snapshots.append)
        service._tick()
        self.assertEqual(opens,[profile])
        dialog,launcher,commands=self.ui();plugin=dialog.plugin;plugin.snapshot=snapshots[-1]
        plugin._launch_checked({'profile':profile,'status':'enabled','version':client['extension_version']})
        launcher.launch_profile.assert_called_once_with(launcher.profiles[0],urls=[project],new_window=False)

    def test_native_open_receives_startup_time_before_any_further_page_repair(self):
        service,client=self.cold();note_missing(service,client,MISSING_PROJECT)
        service.connection_recoveries[client['profile']]={'count':1,'next_at':0}
        self.assertFalse(repair_idle(service,client))
        self.assertFalse(any(r['action']=='repair_project' for r in service.pending.values()))

    def test_previous_app_session_lease_does_not_permanently_prevent_original_open(self):
        service,client=self.cold();note_missing(service,client,MISSING_PROJECT)
        old=copy.deepcopy(service.preferences['browser_slots'][client['profile']]['project_startup'])
        service.project_startup_session='a-new-application-session'
        service.connection_recoveries[client['profile']]={'count':1,'next_at':0}
        self.assertTrue(repair_idle(service,client),'a previous process lease cannot block the fresh read-only probe')
        self.assertTrue(note_missing(service,client,MISSING_PROJECT))
        current=service.preferences['browser_slots'][client['profile']]['project_startup']
        self.assertNotEqual(current['request_id'],old['request_id'])
        self.assertFalse(note_missing(service,client,MISSING_PROJECT),'one native request per missing episode/session')

    def test_any_in_flight_paid_or_unknown_result_prevents_native_navigation(self):
        service,client=self.cold();row=service.store.rows()[0]
        opens=[];service.project_open_requested.connect(opens.append)
        for status in ('preparing','uploading','submitting','generating','downloading','archiving','uncertain','download_failed'):
            service.store.update(row['id'],status,browser=client['id'],remote={
                'account':client['account'],'profile':client['profile']})
            self.assertFalse(note_missing(service,client,MISSING_PROJECT),status)
        self.assertFalse(opens)

    def test_repeated_missing_notices_only_open_once_and_failure_keeps_slot(self):
        service,client=self.cold();opens=[];service.project_open_requested.connect(opens.append)
        self.assertTrue(note_missing(service,client,MISSING_PROJECT))
        for _ in range(5):
            self.assertFalse(note_missing(service,client,MISSING_PROJECT));self.assertFalse(watch(service))
        service.execute({**opens[0],'opened':False,'error':'fixture could not start'})
        self.assertFalse(watch(service));self.assertEqual(len(opens),1)
        self.assertIn(client['profile'],occupied_profiles(service))
        service.execute({'action':'resume_browser','keys':['profile:'+client['profile']]})
        self.assertTrue(watch(service));self.assertEqual(len(opens),2)

    def test_pause_at_gui_cancels_open_then_resume_retries_without_losing_slot(self):
        service,client=self.cold();opens=[];snapshots=[]
        service.project_open_requested.connect(opens.append);service.changed.connect(snapshots.append)
        note_missing(service,client,MISSING_PROJECT)
        dialog,launcher,commands=self.ui();plugin=dialog.plugin
        plugin.snapshot={**snapshots[-1],'paused':True,'released':[]}
        plugin._open_missing_project(opens[0]);launcher.launch_profile.assert_not_called()
        self.assertTrue(commands[-1]['cancelled'])
        service.execute(commands[-1]);service.paused=True
        self.assertFalse(watch(service));service.paused=False
        self.assertTrue(watch(service));self.assertEqual(len(opens),2)

    def test_live_ready_project_or_new_work_cancels_stale_gui_request(self):
        service,client=self.cold();opens=[];snapshots=[]
        service.project_open_requested.connect(opens.append);service.changed.connect(snapshots.append)
        note_missing(service,client,MISSING_PROJECT)
        snapshot=snapshots[-1];command=opens[0]
        ready=copy.deepcopy(snapshot);ready['clients'][0]['page_ready']=True
        self.assertFalse(launch_allowed(ready,command))
        busy=copy.deepcopy(snapshot);busy['jobs'][0].update(status='generating',browser=client['id'])
        self.assertFalse(launch_allowed(busy,command))
        wrong=copy.deepcopy(command);wrong['project']='https://flow.google.com/project/other'
        self.assertFalse(launch_allowed(snapshot,wrong))
        changed=copy.deepcopy(snapshot);changed['clients'][0]['saved_project']=wrong['project']
        self.assertFalse(launch_allowed(changed,command))
        old_result={**command,'request_id':'old-request','opened':False}
        before=copy.deepcopy(service.preferences)
        service.execute(old_result)
        self.assertEqual(service.preferences,before)

    def test_exact_page_response_clears_missing_state_and_rearms_future_recovery(self):
        service,client=self.cold();note_missing(service,client,MISSING_PROJECT)
        service._snapshot(client,{'url':client['saved_project'],'controls_ready':True,'running':False})
        self.assertFalse(client.get('project_missing'))
        self.assertNotIn('project_startup',service.preferences['browser_slots'][client['profile']])
        client['page_ready']=False
        self.assertTrue(note_missing(service,client,MISSING_PROJECT))

    def test_pause_cooldown_account_hold_and_reset_wait_do_not_open(self):
        for mode in ('pause','cooldown','account','reset'):
            with self.subTest(mode=mode):
                self.setUp();service,client=self.cold()
                if mode=='pause':service.paused=True
                elif mode=='cooldown':
                    service.preferences['credit_cooldowns']={client['account']:{'at':time.time(),'until':time.time()+21600,'balance':0}}
                elif mode=='account':service.execute({'action':'mark_account_unavailable','keys':['profile:'+client['profile']]})
                else:client['reset_wait_until']=time.time()+60
                self.assertFalse(note_missing(service,client,MISSING_PROJECT))


if __name__=='__main__':unittest.main()
