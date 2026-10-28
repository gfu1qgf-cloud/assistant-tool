"""GUI launch recovery routes through the existing online profile/tab."""
import copy,json,time,unittest
from unittest.mock import patch

import test_flow_transfer_recovery as fixtures
from app_plugins.builtin.flow_assistant import tab_reuse,ui_mutex
from app_plugins.builtin.flow_assistant.setup_support import OPTIONS_URL

PROJECT='https://flow.google.com/project/reuse'


class TabReuseTests(unittest.TestCase):
    setUpClass=fixtures.TransferTests.__dict__['setUpClass']
    setUp=fixtures.TransferTests.setUp
    service=fixtures.TransferTests.service
    jobs=fixtures.TransferTests.jobs
    client=fixtures.TransferTests.client
    ui=fixtures.TransferTests.ui
    actions=fixtures.TransferTests.actions

    def bound(self,version='1.1.54'):
        service=self.service();client=self.client(service,'Profile 23')
        client.update(project=PROJECT,saved_project=PROJECT,extension_version=version)
        service.preferences.update(browser_options={'ui_mutex':False},profile_projects={client['profile']:PROJECT})
        dialog,launcher,commands=self.ui();plugin=dialog.plugin
        plugin.snapshot={'clients':[client],'preferences':copy.deepcopy(service.preferences)}
        plugin.command.connect(service.execute);service.tab_reused.connect(plugin._tab_reused)
        return service,client,plugin,launcher,commands

    def test_online_launch_uses_existing_tab_and_delivers_ack_without_cli(self):
        service,client,plugin,launcher,commands=self.bound();done=[]
        plugin.launch_browser(client['profile'],[PROJECT],completed=lambda ok,error:done.append((ok,error)))
        launcher.launch_profile.assert_not_called()
        pending=next(k for k,p in service.pending.items() if p.get('tab_reuse_request'))
        self.assertEqual(service.pending[pending]['action'],'reuse_project_tab');self.assertFalse(done)
        service._response(client,{'request_id':pending,'success':True,'data':{'reused':True,'tab_id':7}})
        self.assertEqual(done,[(True,'')]);launcher.launch_profile.assert_not_called()

    def test_failure_or_timeout_never_falls_back_to_native_flow_url(self):
        for mode in ('failure','timeout','disconnect'):
            with self.subTest(mode=mode):
                self.setUp();service,client,plugin,launcher,commands=self.bound();done=[]
                plugin.launch_browser(client['profile'],[PROJECT],completed=lambda ok,error:done.append((ok,error)))
                key=next(k for k,p in service.pending.items() if p.get('tab_reuse_request'))
                if mode=='failure':service._response(client,{'request_id':key,'success':False,'error':'No matching tab'})
                elif mode=='timeout':
                    service.pending[key]['at']=time.time()-21
                    with patch('model.OfflineMode.is_offline',return_value=False):service._tick()
                else:service._disconnected(client['socket'])
                self.assertTrue(done);self.assertFalse(done[-1][0]);launcher.launch_profile.assert_not_called()

    def test_old_bridge_update_opens_options_instead_of_another_flow_page(self):
        service,client,plugin,launcher,commands=self.bound('1.1.53')
        target=PROJECT+'#lzx_setup='+'a'*32+'&lzx_port=18189&lzx_action=options'
        plugin.launch_browser(client['profile'],[target])
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'],[OPTIONS_URL+'#setup='+'a'*32+'&port=18189'])
        self.assertFalse(any(p.get('tab_reuse_request') for p in service.pending.values()))

    def test_older_plain_recovery_declines_without_deadlocking_on_second_ui_lock(self):
        service,client,plugin,launcher,commands=self.bound('1.1.53');done=[]
        plugin.launch_browser(client['profile'],[PROJECT],completed=lambda ok,error:done.append((ok,error)))
        self.assertFalse(done[-1][0]);self.assertIn('1.1.54',done[-1][1]);self.assertFalse(service.pending)
        launcher.launch_profile.assert_not_called()

    def test_busy_or_wrong_project_command_cannot_navigate(self):
        service,client,plugin,launcher,commands=self.bound()
        service.store.enqueue(self.jobs());row=service.store.rows()[0]
        service.store.update(row['id'],'generating',browser=client['id'])
        result=[];service.tab_reused.connect(result.append)
        service.execute({'action':'reuse_project_tab','request_id':'busy','profile':client['profile'],'browser':client['id'],'url':PROJECT})
        self.assertFalse(result[-1]['success']);self.assertFalse(self.actions(client))
        service.execute({'action':'reuse_project_tab','request_id':'wrong','profile':client['profile'],'browser':client['id'],'url':PROJECT+'-other'})
        self.assertFalse(result[-1]['success']);self.assertFalse(self.actions(client))

    def test_mutex_requires_exact_owned_host_lease_and_passes_it_once(self):
        service,client,plugin,launcher,commands=self.bound()
        service.preferences['browser_options']['ui_mutex']=True
        results=[];service.tab_reused.connect(results.append)
        command={'action':'reuse_project_tab','request_id':'request','profile':client['profile'],'browser':client['id'],'url':PROJECT,'host_token':'b'*32}
        service.execute(command);self.assertFalse(results[-1]['success']);self.assertFalse(service.pending)
        owner={'host':True,'profile':client['profile'],'token':'b'*32,'at':time.time()}
        ui_mutex.state(service)['owner']=owner
        service.execute(command)
        sent=self.actions(client)[-1]
        self.assertEqual(sent['action'],'reuse_project_tab');self.assertTrue(sent['params']['host_ui_lease'])
        self.assertIs(ui_mutex.state(service)['owner'],owner)

    def test_first_offline_browser_launch_remains_single_native_open(self):
        service,client,plugin,launcher,commands=self.bound();plugin.snapshot['clients']=[]
        plugin.launch_browser(client['profile'],[PROJECT]);plugin.launch_browser(client['profile'],[PROJECT])
        self.assertEqual(launcher.launch_profile.call_count,1)

    def test_automatic_cold_start_opens_saved_project_instead_of_settings(self):
        service,client,plugin,launcher,commands=self.bound();plugin.snapshot['clients']=[]
        plugin.launch_browser(client['profile'],[PROJECT],bootstrap_existing=True)
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'],[PROJECT])

    def test_offline_pairing_ticket_opens_one_saved_https_project_not_internal_only_tab(self):
        service,client,plugin,launcher,commands=self.bound();plugin.snapshot['clients']=[]
        target=PROJECT+'#lzx_setup='+'a'*32+'&lzx_port=18189'
        plugin.launch_browser(client['profile'],[target])
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'],[target])
        self.assertEqual(launcher.launch_profile.call_count,1)


if __name__=='__main__':unittest.main()
