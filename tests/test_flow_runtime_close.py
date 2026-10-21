"""Manual close is scoped, guarded, recoverable, and never cancels the queue."""
import json
import time
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets
import test_flow_credit_cooldown as fixtures
import test_flow_browser_table as tables
from app_plugins.builtin.flow_assistant.connection_status import browser_status
from app_plugins.builtin.flow_assistant.policy import block_for
from app_plugins.builtin.flow_assistant.runtime_close import CAPABILITY, PAUSED, timeout


class RuntimeCloseTests(unittest.TestCase):
    setUpClass = fixtures.CreditCooldownTests.__dict__['setUpClass']
    setUp = fixtures.CreditCooldownTests.setUp
    service = fixtures.CreditCooldownTests.service
    browser = fixtures.CreditCooldownTests.browser
    client = fixtures.CreditCooldownTests.client
    jobs = fixtures.CreditCooldownTests.jobs

    def close(self, service, client):
        service.execute({'action': 'browser_runtime', 'operation': 'close',
                         'keys': ['profile:' + client['profile']]})

    def ready(self, service, profile='One', points=50):
        client = self.browser(service, profile, points)
        client['capabilities'] = [CAPABILITY, 'window_inventory_v1']
        return client

    def pending(self, service, client):
        return next(k for k, p in service.pending.items()
                    if p['action'] == 'close_browser' and p['client'] == client['id'])

    def test_manual_close_targets_only_selected_profile_without_low_credit_requirement(self):
        service = self.service(); one = self.ready(service, points=1038); two = self.ready(service, 'Two')
        self.close(service, one)
        request = json.loads(one['socket'].sendTextMessage.call_args.args[0])
        self.assertEqual(request['action'], 'close_browser')
        self.assertEqual(request['params'], {'idle': True, 'profile': 'One', 'url': fixtures.PROJECT,
            'reason': 'manual_runtime', 'released_batches': []})
        two['socket'].sendTextMessage.assert_not_called()
        self.assertEqual(block_for(one, service.preferences), PAUSED)
        self.assertEqual(service.store.settings()['browser_blocks']['profile:One'], PAUSED)

    def test_all_inflight_states_including_unpaid_upload_retry_are_protected(self):
        service = self.service(); client = self.ready(service)
        service.store.enqueue(self.jobs(duration=8, budget=0)); row = service.store.rows()[0]
        for status in ('preparing', 'uploading', 'submitting', 'generating', 'downloading',
                       'archiving', 'uncertain', 'download_failed'):
            service.store.update(row['id'], status, browser=client['id'], attempt='a'*32,
                remote={'profile': 'One', 'account': client['account'],
                        'transfer_retries': {'upload': {'next_at': time.time()+60}}})
            before = service.store.rows(); self.close(service, client)
            self.assertEqual(service.store.rows(), before)
            self.assertFalse(service.pending)
            self.assertNotIn('browser_blocks', service.preferences)
        client['socket'].sendTextMessage.assert_not_called()

    def test_other_profile_of_same_account_with_work_also_prevents_close(self):
        service = self.service(); one = self.ready(service); two = self.ready(service, 'Two')
        two['account'] = one['account'].upper()
        service.store.enqueue(self.jobs(duration=8, budget=0)); row = service.store.rows()[0]
        service.store.update(row['id'], 'generating', browser=two['id'], attempt='a'*32,
            remote={'profile': 'Two', 'account': two['account']})
        self.close(service, one); self.assertFalse(service.pending)

    def test_native_and_pending_operations_refuse_close_without_adding_pause(self):
        service = self.service(); client = self.ready(service)
        for flags in ({'native_busy': True}, {'batch_running': True}, {'reset_wait_until': time.time()+60}):
            client.update(flags); self.close(service, client)
            self.assertFalse(service.pending); self.assertFalse(block_for(client, service.preferences))
            for key in flags: client.pop(key)
        service.request(client, 'snapshot'); self.close(service, client)
        self.assertEqual([p['action'] for p in service.pending.values()], ['snapshot'])

    def test_disconnected_old_bridge_and_automatic_call_cannot_close(self):
        service = self.service(); client = self.ready(service)
        self.assertFalse(service.request(client, 'close_browser', {'idle': True}))
        self.assertFalse(service.request(client, 'close_exhausted', {'idle': True}))
        client['capabilities'] = []; self.close(service, client)
        self.assertIn('1.1.34', client['runtime_note']); self.assertFalse(service.pending)
        client.update(capabilities=[CAPABILITY], authorized=False); self.close(service, client)
        self.assertFalse(service.pending)

    def test_closing_profile_is_not_reused_and_other_account_runs_without_waiting_for_exit(self):
        service = self.service(); one = self.ready(service); two = self.ready(service, 'Two')
        service.preferences.update(concurrency=4, profiles=['One', 'Two'])
        service.store.enqueue(self.jobs(duration=8, budget=0)); self.close(service, one)
        service.paused = False; service._tick()
        self.assertEqual(service.store.rows()[0]['browser'], two['id'])
        self.assertEqual(service.store.rows()[0]['status'], 'preparing')
        self.assertTrue(self.pending(service, one))
        self.assertFalse(service.request(one, 'prepare'))

    def test_busy_response_restores_only_new_blocks_and_preserves_queue(self):
        service = self.service(); client = self.ready(service)
        service.preferences['browser_blocks'] = {'browser:'+client['id']: '原有隔离', 'profile:Other': '其他隔离'}
        service.store.enqueue(self.jobs(duration=8, budget=0)); before = service.store.rows()
        self.close(service, client); key = self.pending(service, client)
        service._response(client, {'request_id': key, 'success': False, 'code': 'FLOW_CLOSE_BUSY', 'error': '仍在下载'})
        self.assertNotIn(key, service.pending)
        self.assertEqual(service.preferences['browser_blocks'], {'browser:'+client['id']: '原有隔离', 'profile:Other': '其他隔离'})
        self.assertEqual(service.store.rows(), before)
        self.assertIn('仍在下载', client['runtime_note'])

    def test_close_intent_is_not_final_close_and_final_receipt_releases_slot(self):
        service = self.service(); client = self.ready(service)
        service.preferences['browser_slots'] = {'One': {'state': 'online', 'browser': client['id']}}
        self.close(service, client); key = self.pending(service, client)
        service._response(client, {'request_id': key, 'success': True, 'data': {'closing': True}})
        self.assertIn(key, service.pending); self.assertIn('One', service.preferences['browser_slots'])
        service._response(client, {'request_id': key, 'success': True, 'data': {'closed': True}})
        self.assertNotIn(key, service.pending); self.assertNotIn('One', service.preferences['browser_slots'])
        self.assertEqual(client['runtime_close']['state'], 'closed')
        self.assertEqual(block_for({'profile': 'One'}, service.preferences), PAUSED)
        self.assertEqual(browser_status({'profile': 'One'}, service.preferences)['state'], '手动关闭／暂停')

    def test_timeout_keeps_profile_paused_and_manual_resume_restores_it(self):
        service = self.service(); client = self.ready(service)
        self.close(service, client); service.pending.pop(self.pending(service, client))
        timeout(client)
        self.assertEqual(block_for(client, service.preferences), PAUSED)
        service.resume_browser({'keys': ['profile:One']})
        self.assertNotIn('runtime_close', client)
        self.assertFalse(block_for(client, service.preferences))

    def test_resume_during_close_does_not_reassign_work_or_interrupt_another_selected_profile(self):
        service = self.service(); one = self.ready(service); two = self.ready(service, 'Two')
        service.preferences['browser_blocks'] = {'profile:Two': '环境暂停'}
        self.close(service, one)
        service.resume_browser({'keys': ['profile:One', 'profile:Two']})
        self.assertEqual(block_for(one, service.preferences), PAUSED)
        self.assertEqual(one['runtime_close']['state'], 'checking')
        self.assertFalse(block_for(two, service.preferences))
        self.assertTrue(self.pending(service, one))

    def test_released_batch_proofs_stay_scoped_to_original_browser(self):
        service = self.service(); one = self.ready(service); two = self.ready(service, 'Two')
        with patch.object(service.store, 'released_attempts', side_effect=lambda browser: ['a'*32] if browser == one['id'] else ['b'*32]):
            self.close(service, one)
        request = json.loads(one['socket'].sendTextMessage.call_args.args[0])
        self.assertEqual(request['params']['released_batches'], ['a'*32])

    def test_native_zero_windows_event_settles_close_when_last_window_exits_before_final_response(self):
        service = self.service(); client = self.ready(service)
        service.preferences['browser_slots'] = {'One': {'state': 'online', 'browser': client['id']}}
        self.close(service, client); key = self.pending(service, client)
        service._message(client['socket'], json.dumps({'type': 'windows_closed', 'profile': 'One',
            'project': fixtures.PROJECT}))
        self.assertNotIn(key, service.pending)
        self.assertNotIn('One', service.preferences['browser_slots'])
        self.assertEqual(client['runtime_close']['state'], 'closed')
        self.assertEqual(block_for({'profile': 'One'}, service.preferences), PAUSED)

    def test_disconnection_during_close_keeps_pause_and_does_not_cancel_queue(self):
        service = self.service(); client = self.ready(service)
        service.store.enqueue(self.jobs(duration=8, budget=0)); before = service.store.rows()
        self.close(service, client); service._disconnected(client['socket'])
        self.assertFalse(service.pending)
        self.assertEqual(service.store.rows(), before)
        self.assertEqual(block_for({'profile': 'One'}, service.preferences), PAUSED)
        service.resume_browser({'keys': ['profile:One']})
        self.assertFalse(block_for({'profile': 'One', 'id': client['id']}, service.preferences))

    def test_failed_send_rolls_back_only_this_close_reservation(self):
        service = self.service(); client = self.ready(service)
        with patch.object(service, 'send', return_value=False): self.close(service, client)
        self.assertFalse(service.pending)
        self.assertFalse(block_for(client, service.preferences))
        self.assertIn('连接已断开', client['runtime_note'])


class RuntimeCloseMenuTests(unittest.TestCase):
    setUpClass = tables.BrowserTableTests.__dict__['setUpClass']
    setUp = tables.BrowserTableTests.setUp
    ui = tables.BrowserTableTests.ui
    table = tables.BrowserTableTests.table
    profiles = tables.BrowserTableTests.profiles
    row = tables.BrowserTableTests.row

    def test_close_menu_requires_supported_live_bridge_and_sends_explicit_command(self):
        dialog, _, commands = self.table()
        dialog.browsers.selectRow(self.row(dialog, 'Profile 2'))
        menu = QtWidgets.QMenu(); runtime = dialog.runtime_menu(menu)
        close = runtime.actions()[-1]
        self.assertFalse(close.isEnabled()); self.assertIn('1.1.34', close.text())
        for client in dialog.state['clients']: client['capabilities'] = [CAPABILITY]
        menu = QtWidgets.QMenu(); runtime = dialog.runtime_menu(menu); close = runtime.actions()[-1]
        self.assertTrue(close.isEnabled()); close.trigger()
        self.assertEqual(commands[-1]['operation'], 'close')
        self.assertEqual(commands[-1]['keys'], ['profile:Profile 2'])
        dialog.state['clients'] = []; dialog.update_browsers()
        menu = QtWidgets.QMenu(); self.assertFalse(dialog.runtime_menu(menu).isEnabled())


if __name__ == '__main__': unittest.main()
