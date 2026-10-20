"""Guided connection is automatic after the user's explicit project copy."""
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from qt_compat import QtWidgets, QtTest
import test_flow_assistant as fixtures
import test_flow_setup as setups
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION, OPTIONS_URL, EXTENSIONS_URL
from app_plugins.builtin.flow_assistant.setup_pairing import claim_setup


class ConnectionAutomationTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    ui = setups.SetupTests.ui
    preferences = setups.SetupTests.preferences
    installed = setups.SetupTests.installed

    def probe_done(self, setup):
        deadline = time.monotonic() + 3
        while setup.probe and time.monotonic() < deadline: QtTest.QTest.qWait(10)
        self.assertIsNone(setup.probe)

    def test_installed_empty_project_opens_flow_then_copy_finishes_setup_for_same_profile(self):
        self.preferences(self.installed()); dialog, launcher, commands = self.ui()
        QtWidgets.QApplication.clipboard().setText(setups.SECOND)  # old clipboard is not auto-selected
        dialog.setup.begin(); self.probe_done(dialog.setup)
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], ['https://flow.google.com/'])
        self.assertEqual(dialog.setup.waiting_profile, 'Profile 23'); self.assertFalse(commands)
        QtWidgets.QApplication.clipboard().setText('https://example.test/project/not-flow')
        self.assertFalse(commands)
        QtWidgets.QApplication.clipboard().setText(setups.PROJECT)
        self.assertEqual(commands[-1]['action'], 'setup_profile')
        self.assertEqual(commands[-1]['profile'], 'Profile 23')
        self.assertEqual(commands[-1]['project'], setups.PROJECT)
        self.assertEqual(dialog.setup.waiting_profile, '')

    def test_missing_extension_guides_install_before_project_capture(self):
        self.preferences({}); dialog, launcher, commands = self.ui()
        with patch('app_plugins.builtin.flow_assistant.ui.open_path') as folder:
            dialog.setup.begin(); self.probe_done(dialog.setup)
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], ['https://flow.google.com/'])
        self.assertIn('chrome://extensions/', dialog.setup.status.text())
        folder.assert_called_once(); self.assertFalse(commands)
        self.assertEqual(dialog.setup.phase, 'await_install')
        dialog.setup.continue_button.click()
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], ['https://flow.google.com/'])
        self.assertEqual(dialog.setup.waiting_profile, 'Profile 23')

    def test_unknown_install_record_first_opens_saved_project_not_extension_manager(self):
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        setup.project.setText(setups.PROJECT); setup.begin(); request = commands[-1]
        entry = setups.PROJECT + '#lzx_setup=' + 'a'*32 + '&lzx_port=18189'
        with patch('app_plugins.builtin.flow_assistant.setup_ui.inspect_extension', return_value={
                'profile': 'Profile 23', 'status': 'unknown', 'version': '', 'reason': '记录尚未落盘'}), \
                patch('app_plugins.builtin.flow_assistant.ui.open_path') as folder:
            setup.prepared({'request_id': request['request_id'], 'success': True,
                'pairing': {'profile': 'Profile 23', 'project': setups.PROJECT},
                'project_claim_url': entry})
            self.probe_done(setup)
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [entry])
        folder.assert_not_called()
        self.assertTrue(setup.connection_timer.isActive())
        setup.connection_expired()
        self.assertIn('自动等待 1/3', setup.status.text())
        self.assertEqual(len(commands), 1, 'offline wait keeps the original claim entry and ticket')
        self.assertEqual(launcher.launch_profile.call_count, 1, 'retry must await the host guard before opening the configured project')

    def test_late_unknown_probe_uses_new_live_extension_connection(self):
        dialog, launcher, _ = self.ui(); setup = dialog.setup
        setup.project.setText(setups.PROJECT)
        setup.bundle = {'profile': 'Profile 23', 'project': setups.PROJECT}
        setup.project_claim_url = setups.PROJECT + '#lzx_setup=' + 'a'*32 + '&lzx_port=18189'
        setup.phase = 'connect'
        dialog.state = {'clients': [{'profile': 'Profile 23', 'id': 'live',
            'extension_version': EXTENSION_VERSION}], 'preferences': {}}
        with patch('app_plugins.builtin.flow_assistant.ui.open_path') as folder:
            setup.probed({'profile': 'Profile 23', 'status': 'unknown', 'version': '', 'reason': 'stale'})
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [setup.project_claim_url])
        folder.assert_not_called()

    def test_successful_auto_connection_stops_timeout_and_does_not_overwrite_success(self):
        dialog, _, _ = self.ui(); setup = dialog.setup
        setup.project.setText(setups.PROJECT); setup.phase = 'auto_pair'
        setup.connection_timer.start()
        setup.render_connection({'clients': [{'profile': 'Profile 23', 'id': 'live',
            'page_ready': True, 'extension_version': EXTENSION_VERSION, 'saved_project': setups.PROJECT,
            'project': setups.PROJECT}], 'preferences': {}})
        self.assertFalse(setup.connection_timer.isActive())
        self.assertIn('已自动连接成功', setup.status.text())
        setup.connection_expired()
        self.assertIn('已自动连接成功', setup.status.text())

    def test_switch_profile_close_or_timeout_stops_clipboard_capture(self):
        dialog, _, commands = self.ui(); setup = dialog.setup
        for stop in ('profile', 'timeout', 'close'):
            setup.waiting_profile = setup.profile.currentData()
            if stop == 'profile': setup.profile.setCurrentIndex(1)
            elif stop == 'timeout': setup.capture_expired()
            else: dialog.close()
            QtWidgets.QApplication.clipboard().setText(setups.PROJECT + '?case=' + stop)
            self.assertFalse(commands)

    def test_prepared_setup_opens_one_time_configuration_without_copying_key(self):
        self.preferences(self.installed()); dialog, launcher, commands = self.ui(); setup = dialog.setup
        setup.project.setText(setups.PROJECT); setup.begin(); request = commands[-1]
        ticket = 'a'*32; url = OPTIONS_URL + '#setup=' + ticket + '&port=18189'
        bundle = {'token': 't'*40, 'port': 18189, 'profile': 'Profile 23', 'project': setups.PROJECT, 'setup_id': ticket}
        QtWidgets.QApplication.clipboard().setText('unrelated')
        setup.prepared({'request_id': request['request_id'], 'success': True, 'claim_url': url, 'pairing': bundle})
        self.probe_done(setup)
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [setups.PROJECT+'#lzx_setup='+ticket+'&lzx_port=18189'])
        self.assertEqual(QtWidgets.QApplication.clipboard().text(), 'unrelated')
        self.assertNotIn(bundle['token'], url); self.assertEqual(setup.phase, 'auto_pair')
        self.assertTrue(setup.copy_button.isEnabled()); setup.copy_button.click()
        self.assertEqual(json.loads(QtWidgets.QApplication.clipboard().text())['profile'], 'Profile 23')
        setup.project.clear(); self.assertFalse(setup.copy_button.isEnabled())
        self.assertFalse(setup.options_button.isEnabled())

    def test_double_click_browser_row_runs_connection_guide(self):
        dialog, _, _ = self.ui()
        dialog.update_state({'jobs': [], 'clients': [], 'paused': True, 'preferences': {
            'profiles': ['Profile 24'], 'profile_projects': {'Profile 24': setups.SECOND}}, 'spaces': []})
        with patch.object(dialog.setup, 'begin') as begin:
            dialog.browsers.cellDoubleClicked.emit(0, 0)
        begin.assert_called_once(); self.assertEqual(dialog.setup.profile.currentData(), 'Profile 24')
        self.assertEqual(dialog.tabs.currentWidget(), dialog.settings_page)

    def test_visible_copy_button_requires_project_and_can_prepare_manual_fallback(self):
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        self.assertFalse(setup.copy_button.isEnabled())
        setup.project.setText('https://flow.google.com/'); self.assertFalse(setup.copy_button.isEnabled())
        setup.project.setText(setups.PROJECT); self.assertTrue(setup.copy_button.isEnabled())
        setup.copy_button.click(); request = commands[-1]
        self.assertEqual(request['action'], 'setup_profile'); self.assertEqual(setup.phase, 'copy')
        bundle = {'token': 't'*40, 'port': 18189, 'profile': 'Profile 23', 'project': setups.PROJECT}
        setup.prepared({'request_id': request['request_id'], 'success': True, 'pairing': bundle})
        self.assertEqual(json.loads(QtWidgets.QApplication.clipboard().text())['project'], setups.PROJECT)
        launcher.launch_profile.assert_not_called()
        self.assertTrue(setup.options_button.isEnabled())

    def test_periodic_state_does_not_overwrite_an_unsaved_project_edit(self):
        dialog, _, _ = self.ui(); setup = dialog.setup
        state = {'clients': [], 'preferences': {'profile_projects': {'Profile 23': setups.PROJECT}}}
        setup.apply_state(state)
        setup.project.setText(setups.SECOND); setup.project.textEdited.emit(setups.SECOND)
        setup.apply_state(state)
        self.assertEqual(setup.project.text(), setups.SECOND)

    def test_open_options_prepares_fresh_ticket_instead_of_opening_an_inert_page(self):
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        self.assertFalse(setup.options_button.isEnabled())
        setup.project.setText(setups.PROJECT)
        self.assertTrue(setup.options_button.isEnabled())
        setup.options_button.click()
        self.assertEqual(commands[-1]['action'], 'setup_profile')
        self.assertTrue(commands[-1]['force_pair'])
        self.assertEqual(commands[-1]['project'], setups.PROJECT)
        launcher.launch_profile.assert_not_called()

    def ticket(self, service):
        replies = []; service.setup_ready.connect(replies.append)
        service.execute({'action': 'setup_profile', 'request_id': 'setup', 'profile': 'Profile 23', 'project': setups.PROJECT})
        self.assertTrue(replies[-1]['success'])
        self.assertNotIn(service.pair['token'], replies[-1]['claim_url'])
        return service.setup_requests['Profile 23']['id']

    def test_local_ticket_is_single_use_expires_and_does_not_authorize_generation_socket(self):
        service = self.service(); ticket = self.ticket(service)
        socket = MagicMock(); client = {'socket': socket, 'authorized': False}
        self.assertTrue(claim_setup(service, client, {'ticket': ticket}))
        reply = json.loads(socket.sendTextMessage.call_args.args[0])
        self.assertEqual(reply['pairing']['profile'], 'Profile 23')
        self.assertEqual(reply['pairing']['project'], setups.PROJECT)
        self.assertFalse(client['authorized'])
        self.assertFalse(claim_setup(service, client, {'ticket': ticket}))
        self.assertFalse(claim_setup(service, client, {'ticket': 'b'*32}))
        service.setup_requests['Profile 23'].update(claimed=False, expires=time.time()-1)
        self.assertFalse(claim_setup(service, client, {'ticket': ticket}))

    def test_late_claim_cannot_rebind_a_profile_with_new_busy_work(self):
        service = self.service(); ticket = self.ticket(service)
        service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'download_failed', remote={'profile': 'Profile 23'})
        socket = MagicMock(); client = {'socket': socket, 'authorized': False}
        self.assertFalse(claim_setup(service, client, {'ticket': ticket}))
        self.assertEqual(json.loads(socket.sendTextMessage.call_args.args[0])['type'], 'setup_error')


if __name__ == '__main__': unittest.main()
