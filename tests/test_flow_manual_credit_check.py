"""Selected-browser credit probes, visible results and no paid submissions."""
import time
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
import test_flow_assistant as fixtures
import test_flow_setup as setup_fixtures


class ManualCreditCheckTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp
    service = fixtures.FlowTests.service
    jobs = fixtures.FlowTests.jobs
    client = fixtures.FlowTests.client
    ui = setup_fixtures.SetupTests.ui

    def check(self, service, client):
        service.execute({'action': 'credits', 'keys': ['profile:' + client['profile']], 'manual': True})
        return next(key for key, value in service.pending.items() if value['action'] == 'get_points')

    def test_probe_only_selected_browser_and_does_not_release_queue(self):
        service = self.service()
        one = self.client(service, 'Profile 104', credits=52)
        two = self.client(service, 'Profile 105', credits=50)
        service.store.enqueue(self.jobs(duration=8, budget=0))
        request_id = self.check(service, one)
        self.assertEqual({p['client'] for p in service.pending.values()}, {one['id']})
        self.assertEqual({p['action'] for p in service.pending.values()}, {'get_points', 'snapshot'})
        self.assertTrue(service.paused)
        self.assertFalse(service.released)
        self.assertEqual(one['credit_check']['state'], 'checking')
        self.assertNotIn('credit_check', two)
        service._response(one, {'request_id': request_id, 'success': True,
            'data': {'email': one['account'], 'remaining_points': 52}})
        self.assertEqual(one['credit_check']['state'], 'success')
        self.assertGreater(one['credit_check']['at'], 0)
        self.assertEqual(one['credits'], 52)
        self.assertEqual(service.store.rows()[0]['status'], 'queued')

    def test_manual_read_failure_is_visible_and_successful_zero_is_not_failure(self):
        service = self.service()
        client = self.client(service)
        request_id = self.check(service, client)
        service._response(client, {'request_id': request_id, 'success': False,
            'code': 'FLOW_ACCOUNT_NOT_READY', 'error': '未取得唯一账号邮箱'})
        self.assertEqual(client['credit_check']['state'], 'failed')
        self.assertIn('邮箱', client['credit_check']['message'])
        self.assertFalse(client['credits_local'])
        request_id = self.check(service, client)
        service._response(client, {'request_id': request_id, 'success': True,
            'data': {'email': 'current@example.test', 'remaining_points': 0}})
        self.assertEqual(client['credits'], 0)
        self.assertEqual(client['credit_check']['state'], 'success')

    def test_offline_scope_cannot_fall_back_to_querying_every_browser(self):
        service = self.service()
        self.client(service)
        failures = []
        service.failed.connect(failures.append)
        service.execute({'action': 'credits', 'keys': ['profile:Offline'], 'manual': True})
        self.assertFalse(service.pending)
        self.assertIn('未连接', failures[-1])

    def test_manual_check_does_not_open_account_menu_during_active_task(self):
        service = self.service()
        client = self.client(service)
        service.store.enqueue(self.jobs())
        row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', attempt='a' * 32,
                             browser=client['id'], remote={'account': client['account']})
        service.execute({'action': 'credits', 'keys': ['profile:' + client['profile']], 'manual': True})
        self.assertEqual(client['credit_check']['state'], 'deferred')
        self.assertFalse(any(p['action'] == 'get_points' for p in service.pending.values()))
        self.assertEqual(service.store.get(row['id'])['status'], 'generating')

    def test_repeated_probe_deduplicates_and_timeout_reports_failure(self):
        service = self.service()
        client = self.client(service)
        request_id = self.check(service, client)
        self.check(service, client)
        self.assertEqual(sum(p['action'] == 'get_points' for p in service.pending.values()), 1)
        service.pending[request_id]['at'] = time.time() - 61
        service._tick()
        self.assertEqual(client['credit_check']['state'], 'failed')
        self.assertIn('超时', client['credit_check']['message'])

    def test_browser_context_menu_targets_clicked_row(self):
        dialog, _, commands = self.ui()
        dialog.state = {'preferences': {'profile_projects': {
            'Profile 23': 'https://flow.google.com/project/one',
            'Profile 24': 'https://flow.google.com/project/two'}}, 'clients': []}
        dialog.update_browsers()
        dialog.browsers.selectRow(0)
        captured = []
        def choose(menu, *_args):
            captured.extend(action.text() for action in menu.actions())
            menu.actions()[0].trigger()
        with patch.object(QtWidgets.QMenu, 'exec', choose):
            dialog.browser_menu(dialog.browsers.visualItemRect(dialog.browsers.item(1, 0)).center())
        self.assertIn('检测选中浏览器积分', captured)
        self.assertEqual(commands[-1], {'action': 'credits', 'keys': ['profile:Profile 24'], 'manual': True})

    def test_credit_cell_shows_in_progress_success_time_and_actual_error(self):
        dialog, _, _ = self.ui()
        client = {'id': 'browser', 'profile': 'Profile 23', 'credits': 52}
        dialog.state = {'preferences': {}, 'clients': [client]}
        for state, message, expected in (('checking', '', '检测中'),
                ('success', '已读取当前账号积分', '检测成功'),
                ('failed', '没有读取到积分字段', '没有读取到积分字段')):
            client['credit_check'] = {'state': state, 'at': time.time(), 'message': message}
            dialog.update_browsers()
            self.assertIn(expected, dialog.browsers.item(0, 2).text())
            self.assertIn('52', dialog.browsers.item(0, 2).text())
        self.assertIn('上次', dialog.browsers.item(0, 2).text())

    def test_new_panel_cannot_send_scoped_command_to_old_running_service(self):
        dialog, _, commands = self.ui()
        dialog.plugin.manual_credit_check_api = 0
        dialog.state = {'preferences': {'profile_projects': {
            'Profile 23': 'https://flow.google.com/project/one'}}, 'clients': []}
        dialog.update_browsers()
        dialog.browsers.selectRow(0)
        dialog.detect_selected_credits()
        self.assertFalse(commands)
        self.assertIn('正常重启', dialog.note.text())

    def test_probe_temporarily_holds_only_its_browser_until_result(self):
        service = self.service()
        client = self.client(service, credits=52)
        client.update(page_ready=True, page_at=time.time(), credits_local=True)
        service.store.enqueue(self.jobs(duration=8, budget=0))
        request_id = self.check(service, client)
        service.paused = False
        service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'queued')
        self.assertFalse(any(p['action'] == 'prepare' for p in service.pending.values()))
        service._response(client, {'request_id': request_id, 'success': True,
            'data': {'email': client['account'], 'remaining_points': 52}})
        service._tick()
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')

    def test_other_account_can_prepare_while_selected_browser_is_being_probed(self):
        service = self.service()
        checked = self.client(service, 'Profile 104', credits=52)
        other = self.client(service, 'Profile 105', credits=52)
        service.store.enqueue(self.jobs(duration=8, budget=0))
        self.check(service, checked)
        service.paused = False
        service._tick()
        row = service.store.rows()[0]
        self.assertEqual(row['status'], 'preparing')
        self.assertEqual(row['browser'], other['id'])


if __name__ == '__main__':
    unittest.main()
