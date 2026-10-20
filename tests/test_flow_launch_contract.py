"""Exercise real browser command construction; never start a real browser."""
import unittest
from unittest.mock import MagicMock, patch

import test_flow_setup as fixtures
import test_flow_assistant as flow_fixtures
from qt_compat import QtCore, QtTest
from PYUI.chrome_runner_pyui import ChromeRunnerDialog
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION, OPTIONS_URL, setup_project_entry


class FlowLaunchContractTests(unittest.TestCase):
    setUpClass = fixtures.SetupTests.__dict__['setUpClass']
    setUp = fixtures.SetupTests.setUp
    service = fixtures.SetupTests.service
    ui = fixtures.SetupTests.ui
    preferences = fixtures.SetupTests.preferences
    installed = fixtures.SetupTests.installed

    def test_old_live_extension_update_launches_https_not_an_internal_options_url(self):
        service = self.service()
        service.pair['port'] = 18189
        service.clients['live'] = {'id': 'live', 'socket': MagicMock(),
            'authorized': True, 'profile': 'Profile 23', 'setup_pending': False,
            'extension_version': '1.1.4', 'saved_project': fixtures.PROJECT}
        replies = []; service.setup_ready.connect(replies.append)
        service.execute({'action': 'setup_profile', 'request_id': 'open',
            'profile': 'Profile 23', 'project': fixtures.PROJECT})
        self.assertTrue(replies[-1]['success'])
        self.assertTrue(replies[-1]['project_claim_url'].startswith(fixtures.PROJECT+'#lzx_setup='))
        self.assertIn('lzx_action=options', replies[-1]['project_claim_url'])
        self.assertEqual(setup_project_entry(replies[-1]['pairing'], replies[-1]['project_claim_url']), replies[-1]['project_claim_url'])
        self.assertNotIn(service.pair['token'], replies[-1]['project_claim_url'])

    def test_explicit_options_enters_through_project_not_chrome_command_line(self):
        self.preferences(self.installed())
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        setup.project.setText(fixtures.PROJECT); setup.begin('options')
        request = commands[-1]
        setup.prepared({'request_id': request['request_id'], 'success': True,
            'profile': 'Profile 23', 'claim_url': OPTIONS_URL+'#setup='+fixtures.TICKET+'&port=18189',
            'project_claim_url': fixtures.PROJECT_ENTRY,
            'pairing': {'profile': 'Profile 23', 'project': fixtures.PROJECT,
                        'setup_id': fixtures.TICKET, 'port': 18189, 'token': 't'*40}})
        deadline = QtCore.QDeadlineTimer(3000)
        while setup.probe and not deadline.hasExpired(): QtTest.QTest.qWait(10)
        urls = launcher.launch_profile.call_args.kwargs['urls']
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith(fixtures.PROJECT+'#lzx_setup='))
        self.assertIn('lzx_action=options', urls[0])

    def test_real_launcher_rejects_an_explicit_empty_target(self):
        launcher = type('Launcher', (), {
            'chrome_path': 'chrome.exe', 'get_web_list': lambda _: ['https://example.com/']})()
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            ok, error = ChromeRunnerDialog.launch_profile(launcher, {'directory': 'Profile 23'}, urls=['  ', ''])
        self.assertFalse(ok); self.assertTrue(error); popen.assert_not_called()

    def test_flow_reuses_window_while_manual_next_browser_keeps_its_launch_flags(self):
        launcher = type('Launcher', (), {'chrome_path': r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            'get_web_list': lambda _: [fixtures.PROJECT]})()
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            ChromeRunnerDialog.launch_profile(launcher, {'directory': 'Profile 23'})
            ChromeRunnerDialog.launch_profile(launcher, {'directory': 'Profile 23'}, urls=[fixtures.PROJECT_ENTRY], new_window=False)
        normal, flow = (call.args[0] for call in popen.call_args_list)
        self.assertEqual([arg for arg in normal[:-1] if arg != '--new-window'], flow[:-1])
        self.assertEqual(flow[-1], fixtures.PROJECT_ENTRY)
        self.assertTrue(all(flow)); self.assertNotIn('--new-window', flow)
        self.assertEqual(normal.count('--new-window'), 1)

    def test_already_connected_setup_ack_never_launches_chrome_again(self):
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        setup.project.setText(fixtures.PROJECT); setup.begin()
        setup.prepared({'request_id': commands[-1]['request_id'], 'success': True,
            'connected': True, 'profile': 'Profile 23', 'project': fixtures.PROJECT})
        launcher.launch_profile.assert_not_called()

    def test_queue_open_browser_activates_online_project_without_desktop_launch(self):
        service = self.service()
        client = flow_fixtures.FlowTests.client(self, service, 'Profile 23')
        client['saved_project'] = fixtures.PROJECT
        launches = []; service.launch_requested.connect(launches.append)
        service.execute({'action': 'open_browser', 'profile': 'Profile 23'})
        self.assertFalse(launches)
        self.assertTrue(any(request['action'] == 'open_project' for request in service.pending.values()))

    def test_live_bridge_overrides_stale_install_record_in_queue_launcher(self):
        dialog, launcher, commands = self.ui(); plugin = dialog.plugin
        plugin.snapshot = {'preferences': {'profile_projects': {'Profile 23': fixtures.PROJECT}},
            'clients': [{'profile': 'Profile 23', 'extension_version': EXTENSION_VERSION}]}
        plugin._launch_checked({'profile': 'Profile 23', 'status': 'missing', 'version': ''})
        launcher.launch_profile.assert_not_called()
        self.assertEqual(commands[-1], {'action': 'open_browser', 'profile': 'Profile 23'})

    def test_automatic_entry_needs_no_long_term_key_or_clipboard_operation(self):
        bundle = {'profile': 'Profile 23', 'project': fixtures.PROJECT,
                  'setup_id': fixtures.TICKET, 'port': 18189, 'token': 't'*40}
        self.assertEqual(setup_project_entry(bundle), fixtures.PROJECT_ENTRY)
        self.assertNotIn(bundle['token'], setup_project_entry(bundle))
        with self.assertRaises(ValueError):
            setup_project_entry(bundle, fixtures.PROJECT_ENTRY.replace(fixtures.TICKET, 'b'*32))
        dialog, _, _ = self.ui()
        self.assertIn('无需复制 Token 或配对信息', dialog.setup.hint.text())


if __name__ == '__main__': unittest.main()
