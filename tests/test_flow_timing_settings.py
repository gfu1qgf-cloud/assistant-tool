"""Configurable timing must affect live policy, persist, and preserve credit evidence."""
import copy
import json
import os
import time
import unittest
from flow_reference_fixtures import upload_receipt
from unittest.mock import MagicMock, patch

from qt_compat import QtWidgets, QtGui
import test_flow_credit_cooldown as fixtures
from app_plugins.builtin.flow_assistant.timing import (CONFIG_KEY, SPECS, snapshot, normalize, validate,
    value, delay, request_timeout)
from app_plugins.builtin.flow_assistant.credit_policy import balance_ready
from app_plugins.builtin.flow_assistant.credit_cache import restore_balance
from app_plugins.builtin.flow_assistant.credit_cooldown import credit_hold, remember_balance
from app_plugins.builtin.flow_assistant.policy import capable
from app_plugins.builtin.flow_assistant.store import QueueStore
from app_plugins.builtin.flow_assistant.setup_support import EXTENSION_VERSION

PROJECT = fixtures.PROJECT


class TimingSettingsTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    jobs = fixtures.CreditCooldownTests.jobs
    client = fixtures.CreditCooldownTests.client
    browser = fixtures.CreditCooldownTests.browser
    observe = fixtures.CreditCooldownTests.observe
    low = fixtures.CreditCooldownTests.low
    ui = fixtures.CreditCooldownTests.ui

    def settings(self, config=None, preferences=None):
        from app_plugins.host import PluginHost
        from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
        from model.AppDataStorage import ApplicationStorage, data_file
        import PYUI.main_setting_pyui as settings_module
        path = self.root / 'program-config.json'
        data_file(path).write_json({'run_upload': False, **(config or {})})
        path_patch = patch.object(settings_module, 'CONFIG_PATH', str(path))
        path_patch.start(); self.addCleanup(path_patch.stop)
        parent = QtWidgets.QWidget(); self.addCleanup(parent.close)
        parent.load_config = lambda: data_file(path).read_json()
        parent.appendLog = MagicMock()
        host = PluginHost(parent)
        host.data_storage = ApplicationStorage(self.root / 'private')
        plugin = FlowAssistantPlugin(); host.install(plugin)
        if preferences is not None: plugin.snapshot = {'preferences': preferences}
        dialog = settings_module.MainSettingDialog(parent, plugin_host=host)
        self.addCleanup(dialog.close)
        panel = next(controller for _, full_id, controller in host._settings_controllers
                     if full_id == 'flow_assistant.timing')
        return dialog, panel, plugin, host, data_file(path)

    def hello(self, service, original, project=PROJECT):
        socket = MagicMock()
        service.clients[socket] = {'socket': socket, 'authorized': False, 'connected': time.time()}
        service._message(socket, json.dumps({'type': 'hello', 'protocol': 1, 'token': 'x' * 40,
            'browser_id': original['id'], 'profile': original['profile'], 'project': project,
            'extension_version': EXTENSION_VERSION, 'capabilities': ['timing_settings_v1']}))
        return service.clients[socket]

    def test_default_and_custom_balance_lifetimes_have_no_ninety_second_expiry(self):
        now = time.time(); client = {'account': 'a@example.test', 'credits': 52,
                                    'credits_at': now, 'credits_local': True}
        self.assertTrue(balance_ready(client, now + 3600))
        self.assertTrue(balance_ready(client, now + 21600 - 1))
        self.assertFalse(balance_ready(client, now + 21600))
        self.assertFalse(balance_ready(client, now + 301, {'timing': {'credit_validity_seconds': 300}}))
        self.assertTrue(balance_ready(client, now + 365 * 86400, {'timing': {'credit_validity_seconds': 0}}))
        self.assertFalse(balance_ready({**client, 'credits_at': 0}, now,
            {'timing': {'credit_validity_seconds': 0}}), 'never-expire still requires a real prior observation')

    def test_bad_stored_values_fall_back_but_bad_edits_are_rejected(self):
        for bad in (None, True, float('nan'), float('inf'), -1, '600'):
            self.assertEqual(value({'timing': {'credit_validity_seconds': bad}}, 'credit_validity_seconds'), 21600)
            with self.assertRaises(ValueError): normalize({'credit_validity_seconds': bad})
        self.assertEqual(value({'timing': 'broken'}, 'request_timeout_seconds'), 60)
        self.assertEqual(normalize({'prepare_retry_delays': '7，20, 60'})['prepare_retry_delays'], [7, 20, 60])
        with self.assertRaises(ValueError): normalize({'prepare_retry_delays': '7,wrong'})

    def test_conflicting_waits_are_rejected_with_actionable_reason(self):
        for changes, reason in (({'prepare_timeout_seconds': 20}, '参数准备'),
                ({'page_stale_seconds': 10}, '页面未响应'),
                ({'bridge_reconnect_initial_seconds': 30}, '断线重连'),
                ({'page_health_wait_seconds': 2}, '健康快照')):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, reason):
                validate({'timing': changes})
        self.assertEqual(validate({})['credit_cooldown_seconds'], 21600)

    def test_legacy_stall_setting_and_long_retry_sequences_remain_valid(self):
        self.assertEqual(value({'stall_seconds': 3600}, 'stall_seconds'), 3600)
        self.assertEqual(delay({'timing': {'prepare_retry_delays': [7, 20]}}, 'prepare_retry_delays', 19), 20)
        self.assertEqual(request_timeout({'timing': {'prepare_timeout_seconds': 30}}, 'prepare',
            {'extension_version': '1.1.19'}), 110, 'an older extension keeps its compatible minimum wait')

    def test_fresh_balance_survives_reconnection_without_opening_account_menu(self):
        service = self.service(); original = self.browser(service); self.observe(service, original, 52)
        client = self.hello(service, original)
        self.assertEqual(client['credits'], 52)
        self.assertTrue(client['credits_cached'])
        self.assertFalse(any(p['action'] == 'get_points' for p in service.pending.values()))
        self.assertFalse(client['page_ready'], 'a cached balance never pretends the page is ready')
        messages = [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list]
        welcome = next(message for message in messages if message.get('type') == 'welcome')
        self.assertEqual(welcome['timing']['credit_validity_seconds'], 21600)
        self.assertNotIn('credit_balances', welcome['timing'])

    def test_confirmed_balance_survives_service_restart(self):
        service = self.service(); client = self.browser(service); self.observe(service, client, 52)
        service.execute({'action': 'stop'})
        restarted = self.service(); restored = self.hello(restarted, client)
        self.assertEqual(restored['account'], client['account'])
        self.assertEqual(restored['credits'], 52)
        self.assertFalse(any(p['action'] == 'get_points' for p in restarted.pending.values()))
        self.assertTrue(restarted.paused)

    def test_cache_cannot_move_to_a_different_project(self):
        service = self.service(); client = self.browser(service); self.observe(service, client, 52)
        other = {'id': client['id'], 'profile': client['profile'],
                 'saved_project': 'https://flow.google.com/project/different'}
        self.assertFalse(restore_balance(other, QueueStore(self.namespace).settings()))
        self.assertNotIn('credits', other)

    def test_debited_balance_is_persisted_before_sending_a_paid_request(self):
        service = self.service(); client = self.browser(service); self.observe(service, client, 50)
        service.store.enqueue(self.jobs(duration=8, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'uploading', attempt='a' * 32, browser=client['id'],
            remote={'account': client['account'], 'profile': client['profile'], 'cost': 12, 'cost_source': 'duration_rule'})
        service.request(client, 'upload_images', job=row['id']); key = next(iter(service.pending))
        seen = []
        client['socket'].sendTextMessage.side_effect = lambda _message: seen.append(
            service.store.settings()['credit_balances'][client['account']]['balance'])
        service._response(client, {'request_id': key, 'success': True, 'data': upload_receipt(service, row['id'])})
        self.assertEqual(seen, [38])
        restored = {'id': client['id'], 'profile': client['profile'], 'saved_project': PROJECT}
        self.assertTrue(restore_balance(restored, service.store.settings()))
        self.assertEqual(restored['credits'], 38)
        self.assertTrue(restored['credits_estimated'])

    def test_failed_probe_preserves_number_and_timestamp_without_authorizing_partial_data(self):
        service = self.service(); client = self.browser(service); self.observe(service, client, 52)
        at = client['credits_at']
        service.request(client, 'get_points', manual_credit=True); key = next(iter(service.pending))
        service._response(client, {'request_id': key, 'success': False, 'error': '读取超时',
            'data': {'email': 'different@example.test', 'remaining_points': 1500}})
        self.assertEqual(client['credits'], 52); self.assertEqual(client['credits_at'], at)
        self.assertNotEqual(client['account'], 'different@example.test')
        self.assertTrue(client['identity_unconfirmed'])
        self.assertFalse(capable(client, self.jobs(duration=8)[0], service.preferences, 12))
        self.assertEqual(service.store.settings()['credit_balances'][client['account']]['balance'], 52)

    def test_missing_numeric_result_preserves_same_account_observation(self):
        service = self.service(); client = self.browser(service); self.observe(service, client, 52)
        at = client['credits_at']
        service.request(client, 'get_points', manual_credit=True); key = next(iter(service.pending))
        service._response(client, {'request_id': key, 'success': True,
            'data': {'email': client['account'], 'remaining_points': None}})
        self.assertEqual((client['credits'], client['credits_at']), (52, at))
        self.assertEqual(client['credit_check']['state'], 'failed')

    def test_page_timeout_uses_setting_and_never_erases_confirmed_balance(self):
        service = self.service(); client = self.browser(service); self.observe(service, client, 52)
        client['page_at'] = time.time() - 100
        service._tick(); self.assertTrue(client['page_ready'])
        service.execute({'action': 'preferences', 'value': {'timing': {'page_stale_seconds': 90}}})
        service._tick()
        self.assertFalse(client['page_ready']); self.assertEqual(client['credits'], 52)
        self.assertIn('90 秒', client['page_error'])

    def test_expired_balance_remains_visible_until_one_fresh_read_succeeds(self):
        service = self.service(); client = self.browser(service); self.observe(service, client, 52)
        service.preferences['timing'] = {'credit_validity_seconds': 60}
        client['credits_at'] -= 120
        service.store.enqueue(self.jobs(duration=6, budget=0)); service.paused = False
        service._tick(); service._tick()
        keys = [key for key,p in service.pending.items() if p['action'] == 'get_points']
        self.assertEqual(len(keys), 1); self.assertEqual(client['credits'], 52)
        self.assertEqual(service.store.rows()[0]['status'], 'queued')
        service._response(client, {'request_id': keys[0], 'success': True,
            'data': {'email': client['account'], 'remaining_points': 40}})
        service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')

    def test_expired_balance_failure_cannot_spin_unbounded_probes(self):
        service = self.service(); client = self.browser(service); self.observe(service, client, 52)
        service.preferences['timing'] = {'credit_validity_seconds': 60}; client['credits_at'] -= 120
        service.store.enqueue(self.jobs(duration=6, budget=0)); service.paused = False; service._tick()
        key = next(key for key,p in service.pending.items() if p['action'] == 'get_points')
        service._response(client, {'request_id': key, 'success': False, 'error': '暂时无法读取'})
        for _ in range(4): service._tick()
        self.assertFalse(any(p['action'] == 'get_points' for p in service.pending.values()))
        self.assertEqual(client['credits'], 52)

    def test_changing_cooldown_recalculates_from_original_start_and_keeps_queue_paused(self):
        service = self.service(); client = self.browser(service); original = self.low(service, client)
        service.execute({'action': 'preferences', 'value': {'timing': {'credit_cooldown_seconds': 3600}}})
        saved = service.store.settings()
        self.assertEqual(saved['credit_cooldowns'][client['account']]['until'], original['at'] + 3600)
        self.assertTrue(service.paused); self.assertFalse(service.released)

    def test_saving_settings_persists_and_synchronizes_without_resetting_pairing_or_jobs(self):
        service = self.service(); client = self.browser(service); service.store.enqueue(self.jobs())
        before = service.store.rows(); pair = copy.deepcopy(service.pair)
        service.execute({'action': 'preferences', 'value': {'timing': {
            'credit_validity_seconds': 43200, 'queue_tick_seconds': 2, 'bridge_heartbeat_seconds': 30}}})
        self.assertEqual(service.store.settings()['timing']['credit_validity_seconds'], 43200)
        self.assertEqual(service.timer.interval(), 2000)
        self.assertEqual(service.store.rows(), before); self.assertEqual(service.pair, pair)
        messages = [json.loads(call.args[0]) for call in client['socket'].sendTextMessage.call_args_list]
        synced = next(message for message in messages if message.get('type') == 'timing_settings')
        self.assertEqual(synced['timing']['bridge_heartbeat_seconds'], 30)
        self.assertNotIn('token', synced['timing'])
        self.assertFalse(service.pending)

    def test_invalid_settings_do_not_partially_modify_preferences(self):
        service = self.service(); previous = copy.deepcopy(service.preferences); failures = []
        service.failed.connect(failures.append)
        service.execute({'action': 'preferences', 'value': {'concurrency': 8, 'timing': {'prepare_timeout_seconds': 20}}})
        self.assertEqual(service.preferences, previous)
        self.assertIn('参数准备', failures[-1])

    def test_longer_prepare_timeout_and_custom_retry_wait_are_used_by_worker(self):
        service = self.service(); client = self.browser(service)
        service.execute({'action': 'preferences', 'value': {'timing': {
            'prepare_timeout_seconds': 600, 'prepare_retry_delays': [17, 29]}}})
        service.store.enqueue(self.jobs(duration=6, budget=0)); service.paused = False; service._tick()
        key = next(key for key,p in service.pending.items() if p['action'] == 'prepare')
        service.pending[key]['at'] = time.time() - 300; service._tick()
        self.assertIn(key, service.pending)
        service.pending[key]['at'] = time.time() - 601; service._tick()
        row = service.store.rows()[0]
        self.assertEqual(row['status'], 'queued')
        self.assertAlmostEqual(row['remote']['prepare_retry']['next_at'] - time.time(), 17, delta=1)

    def test_upload_retry_schedule_uses_configured_delays(self):
        service = self.service(); client = self.browser(service)
        service.preferences['timing'] = {'upload_retry_delays': [7, 21]}
        service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'uploading', attempt='a' * 32, browser=client['id'])
        service.transfer_failed(service.store.get(row['id']), 'upload', 'temporary')
        info = service.store.get(row['id'])['remote']['transfer_retries']['upload']
        self.assertAlmostEqual(info['next_at'] - time.time(), 7, delta=1)

    def test_generation_and_download_stall_times_are_independent(self):
        service = self.service(); client = self.browser(service)
        service.preferences['timing'] = {'stall_seconds': 3600, 'download_stall_seconds': 45}
        service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', browser=client['id'], remote={'activity_at': time.time()-121})
        self.assertFalse(service.watch_stalled())
        service.store.update(row['id'], 'downloading')
        self.assertTrue(service.watch_stalled())
        self.assertEqual(service.store.get(row['id'])['status'], 'download_failed')

    def test_timing_catalog_is_loaded_by_main_settings_and_cancel_discards_edits(self):
        dialog, panel, plugin, host, file = self.settings({CONFIG_KEY: {'credit_validity_seconds': 43200}})
        self.assertEqual(set(panel.edits), {spec.key for spec in SPECS})
        self.assertEqual(panel.edits['credit_validity_seconds'].value(), 12)
        self.assertEqual(panel.property('settings_plugin_id'), 'flow_assistant')
        index = dialog.settingTabWidget.indexOf(panel)
        item = dialog.settings_navigation.items[index]
        self.assertEqual(item.parent().text(0), 'Flow 视频助手')
        self.assertEqual(item.parent().parent().text(0), '插件设置')
        self.assertIn('Flow 视频助手', item.text(0))
        self.assertFalse(hasattr(panel, 'save_button'), 'use the host settings transaction')
        before = file.read_json(); commands = []; plugin.command.connect(commands.append)
        panel.edits['credit_validity_seconds'].setValue(2)
        plugin._changed({'preferences': {'timing': {'credit_validity_seconds': 21600}}})
        self.assertEqual(panel.edits['credit_validity_seconds'].value(), 2)
        dialog.reject()
        self.assertEqual(file.read_json(), before); self.assertFalse(commands)

    def test_main_settings_defaults_and_validation_share_the_host_save(self):
        dialog, panel, plugin, host, file = self.settings()
        commands = []; plugin.command.connect(commands.append)
        panel.edits['credit_validity_seconds'].setValue(1)
        panel.restore_defaults(); self.assertFalse(commands)
        self.assertEqual(panel.edits['credit_validity_seconds'].value(), 6)
        self.assertNotIn(CONFIG_KEY, file.read_json())
        panel.edits['prepare_retry_delays'].setText('invalid')
        with patch('PYUI.main_setting_pyui.QMessageBox.warning') as warning:
            dialog.accept()
        self.assertIn('秒数', warning.call_args.args[-1]); self.assertFalse(commands)
        self.assertIs(dialog.settingTabWidget.currentWidget(), panel)
        self.assertNotIn(CONFIG_KEY, file.read_json())

    def test_failed_main_config_save_keeps_edits_without_applying_them(self):
        dialog, panel, plugin, host, file = self.settings()
        commands = []; plugin.command.connect(commands.append)
        panel.edits['credit_validity_seconds'].setValue(12)
        with patch('PYUI.main_setting_pyui._write_config_with_rolling_backups', side_effect=OSError('磁盘暂不可写')), \
                patch('PYUI.main_setting_pyui.QMessageBox.critical') as warning:
            dialog.accept()
        self.assertIn('磁盘暂不可写', warning.call_args.args[-1])
        self.assertNotEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        self.assertEqual(panel.edits['credit_validity_seconds'].value(), 12)
        self.assertNotIn(CONFIG_KEY, file.read_json()); self.assertFalse(commands)

    def test_main_save_applies_to_worker_and_next_start_without_replaying_jobs(self):
        from app_plugins.builtin.flow_assistant.service import FlowService
        service = self.service(); client = self.browser(service); self.observe(service, client, 52)
        service.store.enqueue(self.jobs()); jobs = service.store.rows(); pair = copy.deepcopy(service.pair)
        dialog, panel, plugin, host, file = self.settings(preferences=service.preferences)
        plugin.service = service; plugin.thread = MagicMock(); plugin.thread.isRunning.return_value = True
        plugin.command.connect(service.execute); service.changed.connect(plugin._changed)
        service.preferences_saved.connect(plugin._preferences_saved)
        panel.edits['credit_validity_seconds'].setValue(12)
        panel.edits['queue_tick_seconds'].setValue(2)
        dialog.accept()
        self.assertEqual(dialog.result(), QtWidgets.QDialog.DialogCode.Accepted)
        config = file.read_json()
        self.assertEqual(config[CONFIG_KEY]['credit_validity_seconds'], 43200)
        self.assertTrue(dict(host.apply_settings(config))['flow_assistant'])
        self.assertEqual(service.preferences['timing']['credit_validity_seconds'], 43200)
        self.assertEqual(service.timer.interval(), 2000)
        self.assertEqual(service.store.rows(), jobs); self.assertEqual(service.pair, pair)
        self.assertTrue(service.paused); self.assertFalse(service.pending)
        service.execute({'action': 'stop'})
        # The program config is authoritative if the private runtime copy is older.
        saved = service.store.settings(); saved['timing']['credit_validity_seconds'] = 60
        service.store.save_settings(saved)
        restarted = FlowService(self.namespace, str(self.root / 'downloads'), program_timing=config[CONFIG_KEY])
        restarted.start(); self.addCleanup(lambda: restarted.execute({'action': 'stop'}))
        self.assertEqual(restarted.preferences['timing']['credit_validity_seconds'], 43200)
        restored = self.hello(restarted, client); self.assertEqual(restored['credits'], 52)
        self.assertEqual(restarted.store.rows(), jobs); self.assertEqual(restarted.pair, pair)

    def test_worker_apply_failure_is_reported_and_can_retry_the_saved_program_config(self):
        service = self.service()
        dialog, panel, plugin, host, file = self.settings()
        plugin.service = service; plugin.thread = MagicMock(); plugin.thread.isRunning.return_value = True
        plugin.command.connect(service.execute); service.preferences_saved.connect(plugin._preferences_saved)
        config = {CONFIG_KEY: {'credit_validity_seconds': 43200}}
        with patch.object(service.store, 'save_settings', side_effect=OSError('磁盘暂不可写')):
            plugin.apply_settings(config)
        self.assertIn('本次应用失败', host.main_window.appendLog.call_args.args[0])
        self.assertIsNone(plugin.pending_timing)
        plugin.apply_settings(config)
        self.assertEqual(service.preferences['timing']['credit_validity_seconds'], 43200)

    def test_reset_defaults_before_first_worker_snapshot_is_still_applied(self):
        service = self.service()
        service.execute({'action': 'preferences', 'value': {'timing': {'credit_validity_seconds': 43200}}})
        dialog, panel, plugin, host, file = self.settings()
        self.assertFalse(plugin.snapshot)
        plugin.service = service; plugin.thread = MagicMock(); plugin.thread.isRunning.return_value = True
        plugin.command.connect(service.execute); service.preferences_saved.connect(plugin._preferences_saved)
        plugin.apply_settings({CONFIG_KEY: snapshot({})})
        self.assertEqual(service.preferences['timing']['credit_validity_seconds'], 21600)

    def test_legacy_private_timing_is_loaded_read_only_into_main_settings(self):
        store = QueueStore(self.namespace)
        store.save_settings({'stall_seconds': 3600, 'timing': {'credit_validity_seconds': 43200}})
        before = self.namespace.file('queue.sqlite3').read_bytes()
        dialog, panel, plugin, host, file = self.settings()
        self.assertEqual(panel.edits['credit_validity_seconds'].value(), 12)
        self.assertEqual(panel.edits['stall_seconds'].value(), 60)
        self.assertEqual(self.namespace.file('queue.sqlite3').read_bytes(), before)
        dialog.reject(); self.assertNotIn(CONFIG_KEY, file.read_json())

    def test_flow_task_window_never_overwrites_program_timing_on_ordinary_save(self):
        dialog, _, commands = self.ui()
        self.assertFalse(hasattr(dialog, 'timings'))
        self.assertNotIn('时间与重试', [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())])
        service = self.service()
        service.execute({'action': 'preferences', 'value': {'timing': {
            'credit_validity_seconds': 43200, 'stall_seconds': 3600}}})
        dialog.save_preferences(); command = commands[-1]
        self.assertNotIn('timing', command['value']); self.assertNotIn('stall_seconds', command['value'])
        service.execute(command)
        self.assertEqual(service.preferences['timing']['credit_validity_seconds'], 43200)
        self.assertEqual(service.preferences['timing']['stall_seconds'], 3600)

    def test_ui_offline_record_shows_original_balance_and_validity(self):
        service = self.service(); client = self.browser(service); self.observe(service, client, 52)
        dialog, _, _ = self.ui(); dialog.state = {'clients': [], 'preferences': service.store.settings()}
        dialog.update_browsers()
        text = dialog.browsers.item(0, 3).text()
        self.assertIn('52', text); self.assertIn('上次检测缓存', text); self.assertIn('有效至', text)

    def test_setup_retry_intervals_are_configurable_beyond_three_attempts(self):
        dialog, _, _ = self.ui()
        dialog.state = {'preferences': {'timing': {'setup_pair_wait_seconds': 91,
            'setup_retry_delays': [120, 240], 'setup_auto_attempts': 8, 'setup_clipboard_seconds': 200}}}
        self.assertEqual(dialog.setup.timer_ms('setup_clipboard_seconds'), 200000)
        self.assertEqual(dialog.setup.retry_wait_ms(), 91000)
        dialog.setup.auto_retries = 7
        self.assertEqual(dialog.setup.retry_wait_ms(), 240000)

    def test_settings_layout_can_filter_and_render_without_losing_values(self):
        output = os.environ.get('FLOW_TIMING_SCREENSHOT')
        if output:
            font_id = QtGui.QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
            families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if families: self.app.setFont(QtGui.QFont(families[0], 9))
        dialog, panel, _, _, _ = self.settings()
        dialog.resize(1160, 820); dialog.settingTabWidget.setCurrentWidget(panel)
        dialog.show(); self.app.processEvents()
        before = panel.values()
        panel.search.setText('积分'); self.app.processEvents()
        self.assertFalse(panel.rows['page_controls_wait_seconds'][0].isVisible())
        self.assertEqual(panel.values(), before)
        panel.search.clear(); self.app.processEvents()
        if output: self.assertTrue(dialog.grab().save(output))


if __name__ == '__main__': unittest.main()
