import time
import unittest

import test_flow_setup as setups
import test_flow_manual_parallelism as manual
from app_plugins.builtin.flow_assistant.connection_recovery import repair_idle, watch_connections


class IdleRecoveryTests(unittest.TestCase):
    setUpClass = manual.ManualParallelismTests.__dict__['setUpClass']
    setUp = manual.ManualParallelismTests.setUp
    service = manual.ManualParallelismTests.service
    jobs = manual.ManualParallelismTests.jobs
    client = manual.ManualParallelismTests.client
    browser = manual.ManualParallelismTests.browser
    ui = setups.SetupTests.ui

    def cold(self, service, index=1):
        client = self.browser(service, index, version='1.1.18')
        client.update(connected=time.time()-80, page_ready=False, page_error='Flow 项目页面尚未就绪')
        return client

    def test_four_cold_browsers_recover_independently_without_queued_work(self):
        service = self.service(); clients = [self.cold(service, i) for i in range(4)]
        self.assertTrue(watch_connections(service))
        self.assertEqual({item['client'] for item in service.pending.values()}, {c['id'] for c in clients})
        self.assertEqual({item['action'] for item in service.pending.values()}, {'repair_project'})
        self.assertTrue(service.paused); self.assertFalse(service.released)
        self.assertFalse(watch_connections(service), 'pending recovery cannot be duplicated')

    def test_every_in_flight_and_unresolved_state_prevents_page_refresh(self):
        service = self.service(); client = self.cold(service)
        service.store.enqueue(self.jobs(duration=8, budget=0)); row = service.store.rows()[0]
        for status in ('preparing','uploading','submitting','generating','downloading','archiving','uncertain','download_failed'):
            service.store.update(row['id'], status, browser='other-window', remote={'account':client['account'].upper()})
            self.assertFalse(repair_idle(service, client), status)
        self.assertFalse(service.pending)
        service.store.update(row['id'], 'completed'); client['batch_running'] = True
        self.assertFalse(repair_idle(service, client))

    def test_repair_is_bounded_survives_reconnect_and_rearms_only_initial_credits(self):
        service = self.service(); client = self.cold(service)
        for count in range(1,4):
            if count > 1: service.connection_recoveries[client['profile']]['next_at'] = 0
            self.assertTrue(repair_idle(service, client))
            key = next(key for key,item in service.pending.items() if item['action']=='repair_project')
            service._response(client, {'request_id':key,'success':True,'data':{'reloaded':True}})
            self.assertEqual(service.connection_recoveries[client['profile']]['count'],count)
            self.assertTrue(client['credits_waiting_ready'])
            service.pending.clear()
        replacement = self.cold(service, 1)
        self.assertFalse(repair_idle(service, replacement))
        self.assertEqual(replacement['connection_recovery']['state'],'stopped')
        replacement.update(page_ready=True,credits_local=True)
        watch_connections(service)
        self.assertNotIn(replacement['profile'],service.connection_recoveries)

    def test_wrong_project_old_extension_and_isolated_account_cannot_be_reloaded(self):
        service=self.service(); client=self.cold(service)
        client['saved_project']='https://labs.google/project/other'
        self.assertFalse(repair_idle(service,client))
        client['saved_project']=client['project']; client['extension_version']='1.1.17'
        self.assertFalse(repair_idle(service,client))
        client['extension_version']='1.1.18'
        service.execute({'action':'mark_account_unavailable','keys':['profile:'+client['profile']]})
        self.assertFalse(repair_idle(service,client)); self.assertFalse(service.pending)

    def test_automatic_setup_rechecks_busy_state_in_worker(self):
        service=self.service(); client=self.cold(service)
        service.store.enqueue(self.jobs(duration=8,budget=0)); row=service.store.rows()[0]
        service.store.update(row['id'],'generating',remote={'account':client['account']})
        replies=[]; service.setup_ready.connect(replies.append)
        service.execute({'action':'setup_profile','automatic':True,'request_id':'retry',
            'profile':client['profile'],'project':client['saved_project']})
        self.assertTrue(replies[-1]['deferred']); self.assertFalse(service.pending)
        self.assertFalse(service.setup_requests)

    def test_four_initializing_pages_use_startup_slots_instead_of_opening_fifth(self):
        service=self.service(); service.preferences['concurrency']=4
        clients=[self.cold(service,i) for i in range(4)]
        service.preferences['profiles']=[c['profile'] for c in clients]+['Backup']
        service.preferences['profile_projects']['Backup']='https://labs.google/project/backup'
        service.store.enqueue(self.jobs(duration=8,budget=0)); service.paused=False
        launches=[]; service.launch_requested.connect(launches.append)
        service._tick()
        self.assertFalse(launches)
        self.assertEqual(sum(p['action']=='repair_project' for p in service.pending.values()),4)

    def test_repair_in_progress_holds_same_account_dispatch(self):
        from app_plugins.builtin.flow_assistant.store import choose_client
        service=self.service(); client=self.cold(service); other=self.browser(service,2)
        other['account']=client['account']
        self.assertTrue(repair_idle(service,client))
        client.update(page_ready=True)
        self.assertIsNone(choose_client([client,other],[],4,self.jobs(duration=8)[0],service.preferences,12))

    def test_three_ui_retries_wait_for_host_and_success_cancels_timer(self):
        dialog,launcher,commands=self.ui(); setup=dialog.setup
        setup.project.setText(setups.PROJECT); setup.begin()
        setup.request_id=''; setup.phase='auto_pair'
        for count in range(1,4):
            setup.connection_expired()
            self.assertTrue(commands[-1]['automatic']); self.assertEqual(setup.auto_retries,count)
            setup.prepared({'request_id':commands[-1]['request_id'],'success':True,'connected':True,
                'repaired_pending':True,'project':setups.PROJECT})
        setup.connection_expired()
        self.assertIn('已重试3次',setup.status.text()); launcher.launch_profile.assert_not_called()
        setup.phase='auto_pair'; setup.render_connection({'preferences':{},'clients':[{
            'profile':'Profile 23','id':'ready','saved_project':setups.PROJECT,'project':setups.PROJECT,
            'extension_version':'1.1.18','page_ready':True}]})
        self.assertFalse(setup.connection_timer.isActive()); self.assertEqual(setup.auto_retries,0)

    def test_ui_busy_account_and_close_prevent_automatic_reconfiguration(self):
        dialog,launcher,commands=self.ui(); setup=dialog.setup
        setup.project.setText(setups.PROJECT); setup.begin(); setup.request_id=''; setup.phase='auto_pair'
        dialog.state={'preferences':{},'clients':[{'profile':'Profile 23','id':'one','account':'a@example.test'}],
            'jobs':[{'status':'uncertain','browser':'other','remote':{'account':'A@example.test'}}]}
        before=len(commands); setup.connection_expired()
        self.assertEqual(len(commands),before); self.assertEqual(setup.auto_retries,0)
        dialog.close(); dialog.state={'preferences':{},'clients':[],'jobs':[]}; setup.connection_expired()
        self.assertEqual(len(commands),before); launcher.launch_profile.assert_not_called()


if __name__=='__main__': unittest.main()
