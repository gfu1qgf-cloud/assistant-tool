"""Exercise browser-opening entry points without touching real Chrome windows."""
import time
import unittest
from unittest.mock import MagicMock, patch

from PYUI.chrome_runner_pyui import ChromeRunnerDialog
import test_flow_setup as setups
import test_flow_assistant as flow
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION


class WindowReuseTests(unittest.TestCase):
    setUpClass = setups.SetupTests.__dict__['setUpClass']
    setUp = setups.SetupTests.setUp
    service = setups.SetupTests.service
    jobs = setups.SetupTests.jobs
    ui = setups.SetupTests.ui
    preferences = setups.SetupTests.preferences
    installed = setups.SetupTests.installed

    def test_open_online_original_browser_activates_existing_project_without_launching(self):
        service = self.service()
        client = flow.FlowTests.client(self, service, 'Profile 23')
        client.update(saved_project=setups.PROJECT, project=setups.PROJECT, page_ready=True)
        service.preferences['profile_projects'] = {'Profile 23': setups.PROJECT}
        launches = []; service.launch_requested.connect(launches.append)
        service.execute({'action': 'open_browser', 'profile': 'Profile 23'})
        self.assertFalse(launches)
        self.assertEqual([r['action'] for r in service.pending.values()], ['open_project'])

    def test_cold_online_page_uses_idle_repair_on_same_project(self):
        service = self.service()
        client = flow.FlowTests.client(self, service, 'Profile 23')
        client.update(saved_project=setups.PROJECT, project=setups.PROJECT, page_ready=False)
        service.preferences['profile_projects'] = {'Profile 23': setups.PROJECT}
        launches = []; service.launch_requested.connect(launches.append)
        service.execute({'action': 'open_browser', 'profile': 'Profile 23'})
        self.assertFalse(launches)
        self.assertEqual({r['action'] for r in service.pending.values()}, {'open_project', 'repair_project'})

    def test_open_during_paid_work_never_repairs_or_refreshes_page(self):
        service = self.service()
        client = flow.FlowTests.client(self, service, 'Profile 23')
        client.update(saved_project=setups.PROJECT, project=setups.PROJECT, page_ready=False)
        service.preferences['profile_projects'] = {'Profile 23': setups.PROJECT}
        service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', browser=client['id'],
                             remote={'profile': 'Profile 23', 'account': client['account']})
        launches = []; service.launch_requested.connect(launches.append)
        service.execute({'action': 'open_browser', 'job': row['id']})
        self.assertFalse(launches)
        self.assertEqual([r['action'] for r in service.pending.values()], ['open_project'])
        self.assertEqual(service.store.get(row['id'])['status'], 'generating')

    def test_connected_setup_ack_does_not_start_a_second_browser(self):
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        setup.project.setText(setups.PROJECT); setup.begin()
        setup.prepared({'request_id': commands[-1]['request_id'], 'success': True,
                        'connected': True, 'project': setups.PROJECT})
        launcher.launch_profile.assert_not_called()

    def test_missing_worker_retries_preserve_entry_and_never_open_more_windows(self):
        self.preferences(self.installed())
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        setups.SetupTests.prepare(self, setup, commands)
        self.assertEqual(launcher.launch_profile.call_count, 1)
        initial = list(commands)
        for count in range(1, 4):
            setup.connection_expired()
            self.assertEqual(setup.auto_retries, count)
            self.assertEqual(commands, initial, 'an undeliverable new ticket must not replace the original entry')
            self.assertEqual(launcher.launch_profile.call_count, 1)
            self.assertTrue(setup.connection_timer.isActive())
        setup.connection_expired()
        self.assertFalse(setup.connection_timer.isActive())

    def test_queue_first_open_does_not_force_new_window_and_deduplicates_pending_launch(self):
        dialog, launcher, _ = self.ui(); plugin = dialog.plugin
        launcher.chrome_path = 'chrome.exe'; launcher.get_web_list = MagicMock(return_value=['https://unrelated.test/'])
        launcher.launch_profile = lambda *args, **kwargs: ChromeRunnerDialog.launch_profile(launcher, *args, **kwargs)
        plugin.snapshot = {'preferences': {'profile_projects': {'Profile 23': setups.PROJECT}}}
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            for _ in range(3):
                plugin._launch_checked({'profile': 'Profile 23', 'status': 'enabled', 'version': EXTENSION_VERSION})
        self.assertEqual(popen.call_count, 1)
        self.assertNotIn('--new-window', popen.call_args.args[0])
        self.assertEqual(popen.call_args.args[0][-1], setups.PROJECT)
        launcher.get_web_list.assert_not_called()

    def test_live_connection_arriving_during_probe_reuses_original_window(self):
        dialog, launcher, commands = self.ui(); plugin = dialog.plugin
        plugin.snapshot = {'preferences': {'profile_projects': {'Profile 23': setups.PROJECT}},
                           'clients': [{'id': 'live', 'profile': 'Profile 23', 'saved_project': setups.PROJECT,
                                        'extension_version': EXTENSION_VERSION}]}
        plugin._launch_checked({'profile': 'Profile 23', 'status': 'missing', 'version': ''})
        launcher.launch_profile.assert_not_called()
        self.assertEqual(commands[-1], {'action': 'open_browser', 'profile': 'Profile 23'})

    def test_queue_and_connection_guide_share_pending_startup(self):
        dialog, launcher, _ = self.ui(); plugin = dialog.plugin
        plugin.snapshot = {'preferences': {'profile_projects': {'Profile 23': setups.PROJECT}}}
        plugin._launch_checked({'profile': 'Profile 23', 'status': 'enabled', 'version': EXTENSION_VERSION})
        self.assertTrue(dialog.setup.launch_urls([setups.PROJECT]))
        launcher.launch_profile.assert_called_once_with(launcher.profiles[0], urls=[setups.PROJECT], new_window=False)

    def test_separate_profiles_keep_independent_startup_and_do_not_force_windows(self):
        dialog, launcher, _ = self.ui(); plugin = dialog.plugin
        for profile, url in (('Profile 23', setups.PROJECT), ('Profile 24', setups.SECOND)):
            plugin.snapshot = {'preferences': {'profile_projects': {profile: url}}}
            for _ in range(2):
                plugin._launch_checked({'profile': profile, 'status': 'enabled', 'version': EXTENSION_VERSION})
        self.assertEqual(launcher.launch_profile.call_count, 1)
        plugin.launch_timer.stop(); plugin._next_browser_launch()
        self.assertEqual(launcher.launch_profile.call_count, 2)
        self.assertEqual({call.args[0]['directory'] for call in launcher.launch_profile.call_args_list},
                         {'Profile 23', 'Profile 24'})
        self.assertTrue(all(call.kwargs['new_window'] is False for call in launcher.launch_profile.call_args_list))

    def test_failed_process_launch_can_retry_without_a_false_startup_lock(self):
        dialog, launcher, _ = self.ui(); plugin = dialog.plugin
        launcher.chrome_path = 'chrome.exe'
        launcher.launch_profile = lambda *args, **kwargs: ChromeRunnerDialog.launch_profile(launcher, *args, **kwargs)
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen', side_effect=[OSError('fixture launch denied'), MagicMock()]) as popen:
            self.assertFalse(plugin.launch_browser('Profile 23', [setups.PROJECT])[0])
            self.assertTrue(plugin.launch_browser('Profile 23', [setups.PROJECT])[0])
        self.assertEqual(popen.call_count, 2)

    def test_closed_browser_can_reopen_after_a_successful_connection(self):
        dialog, launcher, _ = self.ui(); plugin = dialog.plugin
        self.assertTrue(plugin.launch_browser('Profile 23', [setups.PROJECT])[0])
        plugin.dialog = None
        plugin._changed({'clients': [{'profile': 'Profile 23'}]})
        plugin._changed({'clients': []})
        self.assertTrue(plugin.launch_browser('Profile 23', [setups.PROJECT])[0])
        plugin.launch_timer.stop(); plugin._next_browser_launch()
        self.assertEqual(launcher.launch_profile.call_count, 2)

    def test_manual_connect_uses_existing_idle_page_repair_and_returns_pending_ack(self):
        service = self.service()
        client = flow.FlowTests.client(self, service, 'Profile 23')
        client.update(saved_project=setups.PROJECT, project=setups.PROJECT, page_ready=False,
                      setup_pending=False, credits_local=True)
        service.preferences['profile_projects'] = {'Profile 23': setups.PROJECT}
        replies = []; service.setup_ready.connect(replies.append)
        service.execute({'action': 'setup_profile', 'request_id': 'connect',
                         'profile': 'Profile 23', 'project': setups.PROJECT})
        self.assertTrue(replies[-1]['connected']); self.assertTrue(replies[-1]['repaired_pending'])
        self.assertEqual({r['action'] for r in service.pending.values()}, {'open_project', 'repair_project', 'snapshot'})
        self.assertFalse(service.setup_requests)


if __name__ == '__main__': unittest.main()
