"""The optional foreground setting belongs to main settings and stays optional."""
import copy
import json
import unittest
from unittest.mock import MagicMock

import test_flow_timing_settings as fixtures
from app_plugins.builtin.flow_assistant.browser_options import CONFIG_KEY, normalize, snapshot
from app_plugins.builtin.flow_assistant.service import FlowService


class BrowserOptionsTests(unittest.TestCase):
    setUpClass = fixtures.TimingSettingsTests.__dict__['setUpClass']
    setUp = fixtures.TimingSettingsTests.setUp
    service = fixtures.TimingSettingsTests.service
    jobs = fixtures.TimingSettingsTests.jobs
    client = fixtures.TimingSettingsTests.client
    browser = fixtures.TimingSettingsTests.browser
    settings = fixtures.TimingSettingsTests.settings
    hello = fixtures.TimingSettingsTests.hello
    observe = fixtures.TimingSettingsTests.observe
    ui = fixtures.TimingSettingsTests.ui

    def options_panel(self, config=None):
        dialog, _, plugin, host, file = self.settings(config)
        panel = next(controller for _, full_id, controller in host._settings_controllers
                     if full_id == 'flow_assistant.browser')
        return dialog, panel, plugin, host, file

    def test_default_enabled_and_non_boolean_values_cannot_enable_or_disable_it(self):
        self.assertEqual(snapshot({}), {'focus_before_action': True})
        self.assertEqual(normalize({'focus_before_action': False}), {'focus_before_action': False})
        for bad in ('false', 'true', 0, 1, None, [], {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize({'focus_before_action': bad})
        self.assertFalse(snapshot({'browser_options': {'focus_before_action': 'true'}})['focus_before_action'])

    def test_main_settings_checkbox_cancel_does_not_save_or_send_commands(self):
        dialog, panel, plugin, host, file = self.options_panel()
        self.assertTrue(panel.focus_before_action.isChecked())
        index = dialog.settingTabWidget.indexOf(panel)
        self.assertEqual(dialog.settings_navigation.items[index].parent().text(0), 'Flow 视频助手')
        self.assertEqual(dialog.settings_navigation.items[index].parent().parent().text(0), '插件设置')
        self.assertIn('浏览器操作', dialog.settings_navigation.items[index].text(0))
        before = file.read_json(); commands = []; plugin.command.connect(commands.append)
        panel.focus_before_action.setChecked(False)
        dialog.reject()
        self.assertEqual(file.read_json(), before)
        self.assertFalse(commands)

    def test_save_off_persists_and_syncs_without_changing_jobs_pairing_or_pause(self):
        service = self.service(); client = self.browser(service)
        service.store.enqueue(self.jobs())
        jobs, pair = service.store.rows(), copy.deepcopy(service.pair)
        dialog, panel, plugin, host, file = self.options_panel()
        plugin.service = service; plugin.thread = MagicMock(); plugin.thread.isRunning.return_value = True
        plugin.command.connect(service.execute); service.preferences_saved.connect(plugin._preferences_saved)
        panel.focus_before_action.setChecked(False)
        dialog.accept()
        self.assertEqual(file.read_json()[CONFIG_KEY], {'focus_before_action': False})
        self.assertTrue(dict(host.apply_settings(file.read_json()))['flow_assistant'])
        self.assertEqual(service.store.settings()['browser_options'], {'focus_before_action': False})
        synced = [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list
                  if json.loads(call.args[0]).get('type') == 'timing_settings']
        self.assertFalse(synced[-1]['browser_options']['focus_before_action'])
        self.assertEqual(service.store.rows(), jobs); self.assertEqual(service.pair, pair)
        self.assertTrue(service.paused)

    def test_saved_off_is_loaded_and_can_be_enabled_again(self):
        dialog, panel, plugin, host, file = self.options_panel({CONFIG_KEY: {'focus_before_action': False}})
        self.assertFalse(panel.focus_before_action.isChecked())
        panel.focus_before_action.setChecked(True)
        dialog.accept()
        self.assertTrue(file.read_json()[CONFIG_KEY]['focus_before_action'])

    def test_worker_restart_takes_authoritative_program_setting(self):
        service = self.service()
        service.execute({'action': 'preferences', 'value': {'browser_options': {'focus_before_action': False}}})
        service.execute({'action': 'stop'})
        restarted = FlowService(self.namespace, str(self.root / 'downloads'),
            program_browser_options={'focus_before_action': True})
        restarted.start(); self.addCleanup(lambda: restarted.execute({'action': 'stop'}))
        self.assertTrue(restarted.preferences['browser_options']['focus_before_action'])
        self.assertTrue(restarted.paused)

    def test_welcome_and_every_command_carry_the_latest_option_including_false(self):
        service = self.service(); client = self.browser(service)
        service.execute({'action': 'preferences', 'value': {'browser_options': {'focus_before_action': False}}})
        reconnected = self.hello(service, client)
        messages = [json.loads(call.args[0]) for call in reconnected['socket'].sendTextMessage.call_args_list]
        welcome = next(message for message in messages if message.get('type') == 'welcome')
        self.assertEqual(welcome['browser_options'], {'focus_before_action': False})
        for message in messages:
            if message.get('action'):
                self.assertEqual(message['browser_options'], {'focus_before_action': False})

    def test_invalid_option_rejects_entire_preferences_edit(self):
        service = self.service(); before = copy.deepcopy(service.preferences); failures = []
        service.failed.connect(failures.append)
        service.execute({'action': 'preferences', 'value': {'concurrency': 8,
            'browser_options': {'focus_before_action': 'false'}}})
        self.assertEqual(service.preferences, before)
        self.assertTrue(failures)

    def test_ordinary_flow_window_save_preserves_the_main_browser_option(self):
        service = self.service()
        service.execute({'action': 'preferences', 'value': {'browser_options': {'focus_before_action': False}}})
        dialog, _, commands = self.ui()
        dialog.save_preferences()
        self.assertNotIn('browser_options', commands[-1]['value'])
        service.execute(commands[-1])
        self.assertFalse(service.preferences['browser_options']['focus_before_action'])


if __name__ == '__main__':
    unittest.main()
