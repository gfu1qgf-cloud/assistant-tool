"""Connection and scheduling regressions, with no live Chrome or paid calls."""
import copy
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

from qt_compat import QtWidgets
import test_flow_assistant as fixtures
import test_flow_setup as setup_fixtures
from app_plugins.builtin.flow_assistant.connection_status import browser_status, configured_profiles
from app_plugins.builtin.flow_assistant.policy import block_for, capable, waiting_reason
from app_plugins.builtin.flow_assistant.setup_support import project_block

PROJECT, SECOND = setup_fixtures.PROJECT, setup_fixtures.SECOND


class UsabilityTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client
    ui = setup_fixtures.SetupTests.ui

    def test_unconfigured_group_members_are_skipped_without_popups_or_lane_writes(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=10, budget=0)); service.paused = False
        service.preferences.update(profiles=['Missing', 'Invalid', 'Ready'], profile_projects={
            'Invalid': 'https://unrelated.test/project/no', 'Ready': PROJECT})
        launched = []; service.launch_requested.connect(launched.append)
        service._tick()
        self.assertEqual(launched, ['Ready'])
        self.assertEqual(service.preferences['profile_lanes'], {'Ready': 10})
        self.assertEqual(configured_profiles(service.preferences), ['Ready'])

    def test_no_configured_backup_keeps_queue_and_explains_configuration(self):
        service = self.service(); service.store.enqueue(self.jobs(duration=10, budget=0)); service.paused = False
        service.preferences['profiles'] = ['Missing']
        launched = []; service.launch_requested.connect(launched.append)
        service._tick(); service.publish()
        row = service.store.rows()[0]
        self.assertEqual(row['status'], 'queued'); self.assertFalse(launched)
        reason = waiting_reason(row, [], [row], service.preferences, {}, {})
        self.assertIn('保存项目地址', reason)
        self.assertNotIn('profile_lanes', service.preferences)

    def test_batch_submit_releases_only_new_ids_after_durable_save(self):
        service = self.service(); service.store.enqueue(self.jobs(prompt='old waiting'))
        old = service.store.rows()[0]['id']; acks = []; service.submitted.connect(acks.append)
        service.execute({'action': 'enqueue', 'request_id': 'batch-new', 'items': [{**self.args, 'prompt': 'new'}],
                         'start_after_save': True})
        self.assertTrue(service.paused); self.assertNotIn(old, service.released)
        self.assertEqual(service.released, set(acks[-1]['ids'])); self.assertFalse(acks[-1]['held'])
        service.execute({'action': 'enqueue', 'request_id': 'batch-new', 'items': [], 'start_after_save': True})
        self.assertEqual(len(service.store.rows()), 2); self.assertFalse(acks[-1]['held'])

    def test_safety_pause_and_save_only_are_not_bypassed(self):
        service = self.service(); acks = []; service.submitted.connect(acks.append)
        service.safety_paused = True
        service.execute({'action': 'enqueue', 'items': [self.args], 'start_after_save': True})
        self.assertFalse(service.released); self.assertTrue(acks[-1]['held'])
        service.safety_paused = False
        service.execute({'action': 'enqueue', 'items': [{**self.args, 'prompt': 'save only'}], 'start_after_save': False})
        self.assertFalse(service.released); self.assertTrue(acks[-1]['held'])

    def test_failed_save_never_releases_jobs(self):
        service = self.service()
        with patch.object(service.store, 'enqueue', side_effect=OSError('volume unavailable')):
            service.execute({'action': 'enqueue', 'items': [self.args], 'start_after_save': True})
        self.assertFalse(service.released); self.assertFalse(service.store.rows())

    def test_connect_existing_profile_is_idempotent_even_during_generation(self):
        service = self.service(); client = self.client(service, 'Profile 23')
        client.update(saved_project=PROJECT, project=PROJECT, setup_pending=False)
        service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', browser=client['id'], remote={'profile': 'Profile 23'})
        acks = []; service.setup_ready.connect(acks.append)
        service.execute({'action': 'setup_profile', 'request_id': 'check', 'profile': 'Profile 23', 'project': PROJECT})
        self.assertTrue(acks[-1]['connected']); self.assertNotIn('claim_url', acks[-1])
        self.assertFalse(client['setup_pending']); self.assertFalse(service.setup_requests)
        self.assertEqual(service.store.get(row['id'])['status'], 'generating')
        self.assertEqual({request['action'] for request in service.pending.values()}, {'get_points', 'snapshot', 'open_project'})
        self.assertFalse(any(request['action'] in {'repair_project', 'refresh_project', 'run_tasks'}
                             for request in service.pending.values()),
                         'a busy project may only be activated and observed, never refreshed or regenerated')

    def test_changed_project_still_requires_pairing_not_silent_switch(self):
        service = self.service(); client = self.client(service, 'Profile 23')
        client.update(saved_project=PROJECT, project=PROJECT)
        acks = []; service.setup_ready.connect(acks.append)
        service.execute({'action': 'setup_profile', 'profile': 'Profile 23', 'project': SECOND})
        self.assertIn('pairing', acks[-1]); self.assertTrue(client['setup_pending'])

    def test_resume_clears_exact_browser_and_profile_blocks_not_other_accounts(self):
        service = self.service(); client = self.client(service, 'Profile 23')
        service.preferences['browser_blocks'] = {'browser:' + client['id']: 'old browser failure',
            'profile:Profile 23': 'profile failure', 'browser:unrelated': 'must retain'}
        self.assertTrue(block_for(client, service.preferences))
        client['setup_pending'] = True; service.setup_requests['Profile 23'] = {'id': 'not saved'}
        service.execute({'action': 'resume_browser', 'keys': ['profile:Profile 23']})
        self.assertEqual(service.preferences['browser_blocks'], {'browser:unrelated': 'must retain'})
        self.assertTrue(client['setup_pending'], 'environment resume must not bypass an incomplete pairing')
        self.assertIn('Profile 23', service.setup_requests)

    def test_socket_hello_does_not_claim_page_is_ready(self):
        service = self.service(); socket = MagicMock(); service.clients[socket] = {'authorized': False}
        service._message(socket, json.dumps({'type': 'hello', 'protocol': 1, 'token': 'x' * 40,
            'browser_id': 'browser', 'profile': 'Profile 23', 'project': PROJECT}))
        client = service.clients[socket]
        client.update(account='fixture@example.test', credits=90, credits_at=time.time())
        self.assertFalse(client['page_ready']); self.assertFalse(capable(client, {'duration': 10}, service.preferences))
        self.assertNotEqual(browser_status(client, service.preferences)['state'], '可接单')
        service._snapshot(client, {'url': PROJECT, 'tasks': []})
        self.assertTrue(client['page_ready']); self.assertTrue(capable(client, {'duration': 10}, service.preferences))

    def test_missing_page_heartbeat_changes_state_without_failing_active_job(self):
        service = self.service(); client = self.client(service)
        service.preferences['timing'] = {'page_stale_seconds': 45}
        client.update(page_ready=True, page_at=time.time() - 60)
        service.store.enqueue(self.jobs()); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', browser=client['id'], remote={'activity_at': time.time()})
        service._tick()
        self.assertFalse(client['page_ready']); self.assertIn('45 秒', client['page_error'])
        self.assertEqual(service.store.get(row['id'])['status'], 'generating')

    def test_old_paired_extension_needs_reload_not_repair_or_generation(self):
        service = self.service(); client = self.client(service, 'Profile 23')
        client.update(extension_version='1.1.1', saved_project=PROJECT, project=PROJECT, setup_pending=False)
        client['setup_blocked'] = project_block(client, service.preferences)
        self.assertFalse(capable(client, {'duration': 8}, service.preferences))
        acks = []; service.setup_ready.connect(acks.append)
        service.execute({'action': 'setup_profile', 'profile': 'Profile 23', 'project': PROJECT})
        self.assertTrue(acks[-1]['success']); self.assertIn('project_claim_url', acks[-1])
        self.assertTrue(service.setup_requests); self.assertFalse(client['setup_pending'])
        self.assertEqual(service.setup_requests['Profile 23']['mode'], 'reload_only')
        self.assertFalse(any(p['action'] == 'run_tasks' for p in service.pending.values()))

    def test_update_ack_preserves_existing_window_without_copying_secret(self):
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        setup.project.setText(PROJECT); setup.begin(); request = commands[-1]
        with patch.object(setup, 'copy_bundle') as copy_bundle:
            setup.prepared({'request_id': request['request_id'], 'success': True, 'connected': True,
                            'needs_update': True, 'profile': 'Profile 23'})
        copy_bundle.assert_not_called()
        launcher.launch_profile.assert_not_called()
        self.assertIn('不用再复制配置', setup.status.text())

    def test_status_tracks_page_login_credits_ready_and_block(self):
        client = {'profile': 'Profile 23'}; preferences = {'profile_projects': {'Profile 23': PROJECT}}
        self.assertEqual(browser_status(client, preferences)['connection'], '未连接')
        client.update(id='browser', project=PROJECT)
        self.assertEqual(browser_status(client, preferences)['project'], '地址已保存')
        client['page_ready'] = True
        self.assertIn('登录账号', browser_status(client, preferences)['reason'])
        client.update(account='fixture@example.test', credits=50, credits_at=time.time())
        self.assertEqual(browser_status(client, preferences)['state'], '可接单')
        client['credits_at'] -= 100
        self.assertEqual(browser_status(client, preferences)['state'], '可接单', 'confirmed balance must not expire after a few minutes')
        client['credits_at'] -= 21600
        self.assertEqual(browser_status(client, preferences)['state'], '未就绪')
        client['environment_block'] = 'Unsupported duration'
        self.assertEqual(browser_status(client, preferences)['state'], '需处理')

    def test_guide_disconnect_and_profile_change_cannot_leave_connected_badge(self):
        dialog, _, _ = self.ui(); client = {'id': 'online', 'profile': 'Profile 23', 'saved_project': PROJECT,
            'project': PROJECT, 'page_ready': True, 'account': 'fixture@example.test', 'credits': 50, 'credits_at': time.time()}
        dialog.state = {'clients': [client], 'preferences': {'profile_projects': {'Profile 23': PROJECT}}}
        dialog.setup.apply_state(dialog.state)
        self.assertIn('可接单', dialog.setup.connection_label.text())
        dialog.setup.profile.setCurrentIndex(1)
        self.assertIn('未连接', dialog.setup.connection_label.text())
        dialog.setup.profile.setCurrentIndex(0)
        dialog.state = {**dialog.state, 'clients': []}; dialog.setup.apply_state(dialog.state)
        self.assertIn('未连接', dialog.setup.connection_label.text())
        self.assertNotIn('已连接', dialog.setup.connection_label.text())

    def test_connected_ack_does_not_copy_secret_or_open_pairing(self):
        dialog, launcher, commands = self.ui(); setup = dialog.setup
        setup.project.setText(PROJECT); setup.begin(); request = commands[-1]
        with patch.object(setup, 'copy_bundle') as copy_bundle:
            setup.prepared({'request_id': request['request_id'], 'success': True, 'connected': True})
        copy_bundle.assert_not_called()
        launcher.launch_profile.assert_not_called()
        self.assertTrue(setup.connect_button.isEnabled()); self.assertIn('没有重新配对', setup.status.text())

    def test_batch_ui_default_is_submit_and_generate_but_save_only_is_explicit(self):
        dialog, _, commands = self.ui()
        target = {**self.args['target'], 'label': '8010'}
        for save_only in (False, True):
            dialog.submit_page.setEnabled(True); dialog.submit_button.setEnabled(True); dialog.compose.setRowCount(0)
            dialog.add_image(target, self.image); dialog.save_only.setChecked(save_only)
            dialog.enqueue()
            command = next(item for item in reversed(commands) if item['action'] == 'enqueue')
            self.assertEqual(command['start_after_save'], not save_only)

    def test_queue_header_and_browser_badges_are_visible(self):
        dialog, _, _ = self.ui()
        client = {'id': 'online', 'profile': 'Profile 23', 'saved_project': PROJECT, 'project': PROJECT,
            'page_ready': False, 'account': '', 'credits': None}
        state = {'jobs': [], 'clients': [client], 'paused': True, 'preferences': {
            'profile_projects': {'Profile 23': PROJECT, 'Profile 24': SECOND}}, 'spaces': []}
        dialog.update_state(state)
        self.assertIn('已连接 1', dialog.connection_summary.text())
        self.assertIn('可接单 0', dialog.connection_summary.text())
        self.assertEqual(dialog.browsers.item(0, 5).text(), '已连接')
        self.assertEqual(dialog.browsers.item(0, 6).text(), '地址已保存')
        self.assertEqual(dialog.browsers.item(0, 7).text(), '未就绪')
        self.assertEqual(dialog.browsers.rowCount(), 2)
        paths = os.environ.get('LZX_FLOW_USABILITY_SCREENSHOTS')
        if paths:
            from pathlib import Path
            dialog.show(); self.app.processEvents()
            dialog.tabs.setCurrentWidget(dialog.browser_page); self.app.processEvents()
            dialog.grab().save(str(Path(paths) / 'browsers.png'))
            dialog.open_setup('Profile 23'); self.app.processEvents()
            dialog.grab().save(str(Path(paths) / 'connection.png'))


if __name__ == '__main__': unittest.main()
