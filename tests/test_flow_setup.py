"""Isolated extension setup tests: no real browser, profile or clipboard secret."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import MagicMock, patch

from qt_compat import QtCore, QtTest, QtWidgets
import test_flow_assistant as fixtures
from app_plugins.builtin.flow_assistant.setup_support import (
    EXTENSION_ID, EXTENSION_VERSION, OPTIONS_URL, EXTENSIONS_URL,
    inspect_extension, project_url, project_block)
from app_plugins.builtin.flow_assistant.policy import capable
from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
from app_plugins.builtin.flow_assistant.ui import FlowDialog
from app_plugins.builtin.flow_assistant.store import QueueStore

PROJECT = 'https://labs.google/fx/tools/flow/project/first-project'
SECOND = 'https://flow.google.com/project/second-project'
TICKET = 'a' * 32
PROJECT_ENTRY = PROJECT + '#lzx_setup=' + TICKET + '&lzx_port=18189'


class SetupTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs

    def preferences(self, value, name='Preferences', profile='Profile 23'):
        path = self.root / 'Chrome' / profile / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def installed(self, state=1, version=EXTENSION_VERSION):
        return {'extensions': {'settings': {EXTENSION_ID: {'state': state, 'manifest': {'version': version}}}}}

    def ui(self):
        parent = QtWidgets.QWidget(); self.addCleanup(parent.close)
        launcher = SimpleNamespace(profiles=[{'directory': 'Profile 23', 'name': '工作账号'},
            {'directory': 'Profile 24', 'name': '备用账号'}], profile_groups=[],
            user_data_dir=str(self.root / 'Chrome'), launch_profile=MagicMock(return_value=(True, None)))
        parent.chrome_plugin = SimpleNamespace(dialog=launcher)
        plugin = FlowAssistantPlugin()
        plugin.context = SimpleNamespace(parent_widget=parent, current_task_targets=MagicMock(return_value=[]), log=MagicMock())
        commands = []; plugin.command.connect(commands.append)
        dialog = FlowDialog(plugin); plugin.dialog = dialog
        self.addCleanup(dialog.close); self.addCleanup(dialog.setup.clear_sensitive_clipboard)
        return dialog, launcher, commands

    def prepare(self, setup, commands, project=PROJECT):
        setup.project.setText(project); setup.begin()
        request = commands[-1]
        self.assertEqual(request['action'], 'setup_profile')
        setup.prepared({'request_id': request['request_id'], 'success': True,
            'claim_url': OPTIONS_URL + '#setup=' + TICKET + '&port=18189', 'project_claim_url': project + '#lzx_setup=' + TICKET + '&lzx_port=18189',
            'pairing': {'token': 't' * 40, 'port': 18189, 'profile': request['profile'],
                        'project': request['project'], 'extension_id': EXTENSION_ID, 'setup_id': TICKET}})
        deadline = time.monotonic() + 3
        while setup.probe and time.monotonic() < deadline: QtTest.QTest.qWait(10)
        self.assertIsNone(setup.probe)

    def test_extension_detection_is_read_only_and_uses_exact_id(self):
        path = self.preferences(self.installed(), name='Secure Preferences')
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = inspect_extension(self.root / 'Chrome', 'Profile 23')
        self.assertEqual(result['status'], 'enabled'); self.assertEqual(result['version'], EXTENSION_VERSION)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.preferences({'extensions': {'settings': {'another-extension': {'state': 1}}}}, profile='Profile 24')
        self.assertEqual(inspect_extension(self.root / 'Chrome', 'Profile 24')['status'], 'missing')

    def test_disabled_unreadable_and_file_only_are_not_misreported_as_missing(self):
        self.preferences(self.installed(state=0))
        self.assertEqual(inspect_extension(self.root / 'Chrome', 'Profile 23')['status'], 'disabled')
        path = self.root / 'Chrome/Profile 23/Preferences'; path.write_text('{broken', encoding='utf-8')
        self.assertEqual(inspect_extension(self.root / 'Chrome', 'Profile 23')['status'], 'unknown')
        path.write_text('{}', encoding='utf-8')
        (path.parent / 'Extensions' / EXTENSION_ID).mkdir(parents=True)
        self.assertEqual(inspect_extension(self.root / 'Chrome', 'Profile 23')['status'], 'unknown')

    def test_detection_rejects_escaping_profile_paths(self):
        for profile in ('../other', 'C:\\other', '.', '..', '/absolute', 'bad\nname'):
            with self.subTest(profile=profile), self.assertRaises(ValueError): inspect_extension(self.root, profile)
        for root in ('', 'relative-folder'):
            with self.subTest(root=root), self.assertRaises(ValueError): inspect_extension(root, 'Profile 23')

    def test_contradictory_records_are_unknown_not_a_reinstall_verdict(self):
        self.preferences(self.installed(state=1)); self.preferences(self.installed(state=0), name='Secure Preferences')
        self.assertEqual(inspect_extension(self.root / 'Chrome', 'Profile 23')['status'], 'unknown')

    def test_project_must_be_explicit_and_on_supported_google_origin(self):
        self.assertEqual(project_url(SECOND + '/#fragment', required=True), SECOND)
        for value in ('', 'https://labs.google/', 'https://evil.test/project/p',
                      'https://labs.google.evil.test/project/p', 'https://user:password@labs.google/project/p',
                      'http://labs.google/project/p', 'https://labs.google:8080/project/p'):
            with self.subTest(value=value), self.assertRaises(ValueError): project_url(value, required=True)

    def test_no_project_starts_extension_detection_without_saving_a_guessed_project(self):
        dialog, launcher, commands = self.ui()
        dialog.setup.begin()
        self.assertIn('检测扩展', dialog.setup.status.text())
        self.assertFalse(commands); launcher.launch_profile.assert_not_called()

    def test_missing_extension_opens_selected_profile_manager_and_folder_with_bundle_ready(self):
        self.preferences({})
        dialog, launcher, commands = self.ui()
        with patch('app_plugins.builtin.flow_assistant.ui.open_path') as open_path:
            self.prepare(dialog.setup, commands)
        self.assertEqual(launcher.launch_profile.call_args.args[0]['directory'], 'Profile 23')
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [PROJECT])
        self.assertIn('chrome://extensions/', dialog.setup.status.text())
        open_path.assert_called_once()
        self.assertNotIn('tttttttt', QtWidgets.QApplication.clipboard().text())
        self.assertIn('安装／更新完成，打开 Flow', dialog.setup.status.text())
        self.assertFalse(any(item['action'] in {'enqueue', 'start'} for item in commands))

    def test_installed_but_offline_opens_options_not_reinstallation(self):
        self.preferences(self.installed())
        dialog, launcher, commands = self.ui()
        with patch('app_plugins.builtin.flow_assistant.ui.open_path') as open_path:
            self.prepare(dialog.setup, commands)
        open_path.assert_not_called()
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [PROJECT_ENTRY])
        self.assertNotIn('Ctrl+V', dialog.setup.status.text())

    def test_online_extension_is_definitive_without_reading_profile_files(self):
        dialog, launcher, commands = self.ui()
        dialog.state = {'clients': [{'profile': 'Profile 23', 'id': 'online', 'extension_version': EXTENSION_VERSION}]}
        with patch('app_plugins.builtin.flow_assistant.setup_ui.inspect_extension') as inspect:
            self.prepare(dialog.setup, commands)
        inspect.assert_not_called()
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [PROJECT_ENTRY])

    def test_disabled_extension_requests_enable_not_duplicate_install(self):
        self.preferences(self.installed(state=0))
        dialog, launcher, commands = self.ui()
        with patch('app_plugins.builtin.flow_assistant.ui.open_path'):
            self.prepare(dialog.setup, commands)
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [PROJECT])
        self.assertIn('不用重复安装', dialog.setup.status.text())

    def test_shutdown_ignores_late_probe_and_cannot_open_browser(self):
        dialog, launcher, _ = self.ui(); setup = dialog.setup
        setup.bundle = {'profile': 'Profile 23', 'token': 't' * 40}; setup.phase = 'connect'
        setup.shutdown()
        setup.probed({'profile': 'Profile 23', 'status': 'enabled', 'version': EXTENSION_VERSION})
        launcher.launch_profile.assert_not_called()

    def test_old_blank_draft_does_not_hide_an_existing_extensions_saved_address(self):
        dialog, _, _ = self.ui(); setup = dialog.setup
        setup.profile.setCurrentIndex(1); setup.profile.setCurrentIndex(0)
        setup.apply_state({'preferences': {'profile_projects': {'Profile 23': PROJECT}}, 'clients': []})
        self.assertEqual(setup.project.text(), PROJECT)

    def test_old_extension_gets_reload_instructions_then_explicit_continue_opens_options(self):
        self.preferences(self.installed(version='1.1.0'))
        dialog, launcher, commands = self.ui()
        with patch('app_plugins.builtin.flow_assistant.ui.open_path'):
            self.prepare(dialog.setup, commands)
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [PROJECT_ENTRY+'&lzx_action=options'])
        self.assertIn('自动完成连接', dialog.setup.status.text())
        dialog.setup.begin('pair'); request = commands[-1]
        dialog.setup.prepared({'request_id': request['request_id'], 'success': True, 'pairing': dict(dialog.setup.bundle),
            'project_claim_url': PROJECT_ENTRY, 'claim_url': OPTIONS_URL + '#setup=' + TICKET + '&port=18189'})
        deadline = time.monotonic() + 3
        while dialog.setup.probe and time.monotonic() < deadline: QtTest.QTest.qWait(10)
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [PROJECT_ENTRY+'&lzx_action=options'])

    def test_each_profile_keeps_its_own_unsaved_address_draft(self):
        dialog, _, _ = self.ui(); setup = dialog.setup
        setup.project.setText(PROJECT); setup.profile.setCurrentIndex(1)
        self.assertEqual(setup.project.text(), '')
        setup.project.setText(SECOND); setup.profile.setCurrentIndex(0)
        self.assertEqual(setup.project.text(), PROJECT)
        setup.apply_state({'preferences': {'profile_projects': {'Profile 23': 'https://labs.google/project/stale'}}, 'clients': []})
        self.assertEqual(setup.project.text(), PROJECT)
        setup.profile.setCurrentIndex(1); self.assertEqual(setup.project.text(), SECOND)

    def test_setup_save_is_per_profile_atomic_and_never_generates(self):
        service = self.service(); results = []; service.setup_ready.connect(results.append)
        for profile, project in (('Profile 23', PROJECT), ('Profile 24', SECOND)):
            service.execute({'action': 'setup_profile', 'request_id': profile, 'profile': profile, 'project': project})
        projects = QueueStore(self.namespace).settings()['profile_projects']
        self.assertEqual(projects, {'Profile 23': PROJECT, 'Profile 24': SECOND})
        self.assertEqual([r['pairing']['profile'] for r in results], ['Profile 23', 'Profile 24'])
        self.assertFalse(service.pending); self.assertFalse(service.store.rows()); self.assertTrue(service.paused)

    def test_failed_private_save_does_not_open_browser_or_mutate_live_settings(self):
        service = self.service(); before = copy.deepcopy(service.preferences)
        results = []; service.setup_ready.connect(results.append)
        with patch.object(service.store, 'save_settings', side_effect=PermissionError('private volume unavailable')):
            service.execute({'action': 'setup_profile', 'profile': 'Profile 23', 'project': PROJECT})
        self.assertFalse(results[0]['success']); self.assertNotIn('pairing', results[0])
        self.assertEqual(service.preferences, before)

    def test_unavailable_local_server_does_not_prepare_a_dead_connection(self):
        service = self.service(); service.server.close()
        results = []; service.setup_ready.connect(results.append)
        service.execute({'action': 'setup_profile', 'profile': 'Profile 23', 'project': PROJECT})
        self.assertFalse(results[0]['success']); self.assertIn('连接服务', results[0]['error'])
        self.assertNotIn('profile_projects', service.preferences)

    def test_repair_cannot_interrupt_an_active_generation(self):
        service = self.service(); service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', browser='browser', remote={'profile': 'Profile 23'})
        results = []; service.setup_ready.connect(results.append)
        service.execute({'action': 'setup_profile', 'profile': 'Profile 23', 'project': PROJECT})
        self.assertFalse(results[0]['success']); self.assertIn('不能重新设置', results[0]['error'])
        self.assertEqual(service.store.get(row['id'])['status'], 'generating')

    def test_missing_or_mismatched_saved_project_blocks_paid_dispatch(self):
        client = {'profile': 'Profile 23', 'id': 'browser', 'credits': 50, 'credits_at': time.time(), 'account': 'a@test.test'}
        preferences = {'profile_projects': {'Profile 23': PROJECT}}
        client['setup_blocked'] = project_block(client, preferences)
        self.assertIn('未保存', client['setup_blocked'])
        self.assertFalse(capable(client, {'duration': 8}, preferences, 7))
        client.update(saved_project=SECOND, project=SECOND)
        self.assertIn('尚未同步', project_block(client, preferences))
        client.update(saved_project=PROJECT, project=PROJECT)
        self.assertEqual(project_block(client, preferences), '')

    def test_existing_extension_saved_address_is_remembered_not_replaced_by_page_address(self):
        service = self.service(); socket = MagicMock(); service.clients[socket] = {'authorized': False}
        hello = {'type': 'hello', 'protocol': 1, 'token': 'x' * 40, 'browser_id': 'existing-browser',
                 'profile': 'Profile 23', 'project': PROJECT, 'extension_version': EXTENSION_VERSION}
        service._message(socket, json.dumps(hello))
        self.assertEqual(service.preferences['profile_projects']['Profile 23'], PROJECT)
        service._message(socket, json.dumps({'type': 'page_status', 'url': SECOND}))
        self.assertEqual(service.preferences['profile_projects']['Profile 23'], PROJECT)
        self.assertIn('不是此配置', service.clients[socket]['setup_blocked'])

    def test_repair_holds_browser_until_the_new_bundle_is_saved(self):
        service = self.service(); results = []; service.setup_ready.connect(results.append)
        service.execute({'action': 'setup_profile', 'request_id': 'same-request', 'profile': 'Profile 23', 'project': PROJECT})
        nonce = results[-1]['pairing']['setup_id']
        service.execute({'action': 'setup_profile', 'request_id': 'same-request', 'profile': 'Profile 23', 'project': PROJECT})
        self.assertEqual(results[-1]['pairing']['setup_id'], nonce)
        hello = {'type': 'hello', 'protocol': 1, 'token': 'x' * 40, 'browser_id': 'browser',
                 'profile': 'Profile 23', 'project': PROJECT}
        first = MagicMock(); service.clients[first] = {'authorized': False}
        service._message(first, json.dumps(hello))
        self.assertTrue(service.clients[first]['setup_pending'])
        service._message(first, json.dumps({'type': 'page_status', 'url': PROJECT}))
        self.assertIn('后台自动确认', service.clients[first]['setup_blocked'])
        self.assertIn('无需手动', service.clients[first]['setup_blocked'])
        replacement = MagicMock(); service.clients[replacement] = {'authorized': False}
        service._message(replacement, json.dumps({**hello, 'setup_id': nonce}))
        self.assertFalse(service.clients[replacement]['setup_pending'])
        self.assertFalse(service.clients[replacement]['setup_blocked'])
        self.assertNotIn('Profile 23', service.setup_requests)

    def test_setup_ack_must_match_request_and_failed_ack_retains_form(self):
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        setup.project.setText(PROJECT); setup.begin(); pending = setup.request_id
        setup.prepared({'request_id': 'stale', 'success': True})
        self.assertEqual(setup.request_id, pending); launcher.launch_profile.assert_not_called()
        setup.prepared({'request_id': pending, 'success': False, 'error': '保存失败'})
        self.assertEqual(setup.project.text(), PROJECT); self.assertTrue(setup.connect_button.isEnabled())

    def test_sensitive_clipboard_expires_only_if_unchanged(self):
        dialog, _, _ = self.ui(); setup = dialog.setup
        setup.project.setText(PROJECT)
        setup.bundle = {'token': 't' * 40, 'profile': 'Profile 23', 'project': PROJECT}; setup.copy_bundle()
        setup.clear_sensitive_clipboard(); self.assertEqual(QtWidgets.QApplication.clipboard().text(), '')
        setup.copy_bundle(); QtWidgets.QApplication.clipboard().setText('user replacement')
        setup.clear_sensitive_clipboard(); self.assertEqual(QtWidgets.QApplication.clipboard().text(), 'user replacement')

    def test_launcher_explicit_setup_url_does_not_open_unrelated_presets(self):
        from PYUI.chrome_runner_pyui import ChromeRunnerDialog
        launcher = SimpleNamespace(chrome_path='chrome.exe', get_web_list=lambda: ['https://unrelated.test/'])
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            self.assertEqual(ChromeRunnerDialog.launch_profile(launcher, {'directory': 'Profile 23'}, urls=[OPTIONS_URL]), (True, None))
        command = popen.call_args.args[0]
        self.assertIn('--profile-directory=Profile 23', command)
        self.assertIn(OPTIONS_URL, command); self.assertNotIn('https://unrelated.test/', command)

    def test_auto_launch_without_saved_project_prompts_instead_of_guessing(self):
        dialog, launcher, commands = self.ui(); plugin = dialog.plugin
        plugin._launch('Profile 24')
        launcher.launch_profile.assert_not_called()
        self.assertEqual(dialog.setup.profile.currentData(), 'Profile 24')
        self.assertIn('手动添加', dialog.setup.status.text())
        self.assertIs(dialog.tabs.currentWidget(), dialog.settings_page)
        self.assertFalse(any(item['action'] == 'enqueue' for item in commands))

    def test_queue_launch_detects_missing_extension_and_prepares_guide(self):
        self.preferences({})
        dialog, launcher, commands = self.ui(); plugin = dialog.plugin
        plugin.snapshot = dialog.state = {'preferences': {'profile_projects': {'Profile 23': PROJECT}}}
        plugin._launch('Profile 23')
        deadline = time.monotonic() + 3
        while plugin.launch_probes and time.monotonic() < deadline: QtTest.QTest.qWait(10)
        self.assertFalse(plugin.launch_probes)
        self.assertEqual(commands[-1]['action'], 'setup_profile')
        self.assertEqual(commands[-1]['profile'], 'Profile 23')
        launcher.launch_profile.assert_not_called()
        self.assertIs(dialog.tabs.currentWidget(), dialog.settings_page)

    def test_queue_launch_of_installed_current_extension_stays_seamless(self):
        self.preferences(self.installed())
        dialog, launcher, commands = self.ui(); plugin = dialog.plugin
        plugin.snapshot = dialog.state = {'preferences': {'profile_projects': {'Profile 23': PROJECT}}}
        plugin._launch('Profile 23')
        deadline = time.monotonic() + 3
        while plugin.launch_probes and time.monotonic() < deadline: QtTest.QTest.qWait(10)
        self.assertEqual(launcher.launch_profile.call_args.args[0]['directory'], 'Profile 23')
        self.assertEqual(launcher.launch_profile.call_args.kwargs, {'urls': [PROJECT]})
        self.assertFalse(any(item['action'] == 'setup_profile' for item in commands))

    def test_queue_probe_rechecks_project_before_launch_and_never_uses_generic_presets(self):
        dialog, launcher, commands = self.ui(); plugin = dialog.plugin
        plugin.snapshot = {'preferences': {'profile_projects': {'Profile 23': SECOND}}}
        plugin._launch_checked({'profile': 'Profile 23', 'status': 'enabled', 'version': EXTENSION_VERSION})
        self.assertEqual(launcher.launch_profile.call_args.kwargs['urls'], [SECOND])
        launcher.launch_profile.reset_mock()
        plugin.snapshot = {'preferences': {}}
        plugin._launch_checked({'profile': 'Profile 23', 'status': 'enabled', 'version': EXTENSION_VERSION})
        launcher.launch_profile.assert_not_called()
        self.assertFalse(commands)

    def test_launcher_keeps_one_time_project_entry_intact_in_real_command_arguments(self):
        from PYUI.chrome_runner_pyui import ChromeRunnerDialog
        entry = PROJECT + '#lzx_setup=' + 'a'*32 + '&lzx_port=18189'
        launcher = SimpleNamespace(chrome_path='chrome.exe', get_web_list=lambda: ['https://unrelated.test/'])
        with patch('PYUI.chrome_runner_pyui.subprocess.Popen') as popen:
            ChromeRunnerDialog.launch_profile(launcher, {'directory': 'Profile 23'}, urls=[entry])
        self.assertEqual(popen.call_args.args[0][-1], entry)
        self.assertNotIn('https://unrelated.test/', popen.call_args.args[0])

    def test_optional_setup_visual_qa(self):
        target = os.environ.get('LZX_FLOW_SETUP_SCREENSHOT')
        if not target: self.skipTest('optional visual QA snapshot')
        dialog, _, _ = self.ui(); dialog.open_setup('Profile 23')
        dialog.setup.project.setText(PROJECT); dialog.show(); self.app.processEvents()
        dialog.grab().save(target)


if __name__ == '__main__': unittest.main()
