import base64
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from model.AppDataStorage import data_directory
from app_plugins.builtin.image_remix.completion import completion_target, completion_targets
from app_plugins.builtin.image_remix.service import Service, new_attempt, run_operation
from app_plugins.builtin.image_remix.store import Store
from app_plugins.builtin.image_remix.client import APIError
from test_image_remix import FakeClient, png, spec


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root / 'private')
        self.context = SimpleNamespace(check_canceled=Mock(), report=Mock())

    def partial(self, count=10, **changes):
        job = self.store.create(spec(self.root, count=count, **changes), 'original-key')
        client = FakeClient(); service = Service(self.store, client); service.advance(job)
        attempt = job['attempts'][-1]
        client.results['file-output'] = [{'custom_id': attempt['token'] + ':' + job['items'][0]['id'],
            'response': {'status_code': 200, 'body': {'data': [{'b64_json': base64.b64encode(png()).decode()}]}},
            'error': None}]
        client.cloud.update(status='completed', output_file_id='file-output')
        return service.advance(self.store.get(job['id']))

    def run_complete(self, targets, clients=None, **patches):
        clients = clients or [FakeClient() for _ in targets]
        with patch('app_plugins.builtin.image_remix.service.credential_config', **patches) as credentials, \
             patch('app_plugins.builtin.image_remix.service.read_provider_key', return_value='synthetic-key'), \
             patch('app_plugins.builtin.image_remix.service.BatchClient', side_effect=clients):
            result = run_operation({'root': str(self.store.root), 'action': 'complete', 'targets': targets}, self.context)
        return result, clients, credentials

    def test_screenshot_one_of_ten_retries_only_nine_and_keeps_file_and_prompts(self):
        job = self.partial(); saved = deepcopy(job['items'][0]); raw = Path(saved['path']).read_bytes()
        target = completion_target(job)
        result, clients, credentials = self.run_complete([target])
        latest = self.store.get(job['id']); attempt = latest['attempts'][-1]
        self.assertEqual(len(attempt['item_ids']), 9); self.assertEqual(len(clients[0].creates), 1)
        self.assertEqual(latest['items'][0], saved); self.assertEqual(Path(saved['path']).read_bytes(), raw)
        self.assertEqual([i['prompt'] for i in latest['items']], [i['prompt'] for i in job['items']])
        rows = [json.loads(line) for line in clients[0].uploads[-1][0].splitlines()]
        self.assertEqual(len(rows), 9)
        self.assertNotIn(saved['id'], attempt['item_ids'])
        self.assertEqual(latest['status'], 'running'); self.assertEqual(len(latest['attempts']), 2)
        self.assertEqual(latest['spec'], job['spec']); self.assertEqual(latest['credential'], job['credential'])
        credentials.assert_called_once_with({}, 'original-key')
        self.assertIn('9 张', result['messages'][0])

    def test_no_replanning_of_accepted_plans_or_reference_changes(self):
        reference = self.root / 'face.png'; reference.write_bytes(png())
        job = self.partial(references=[str(reference)])
        snapshot = self.store.asset(job, job['references'][0])
        job['planning'] = {'state': 'done', 'chunks': []}
        for item in job['items']: item['plan'] = {'prompt': item['prompt']}
        self.store.save(job); reference.unlink()
        with patch('app_plugins.builtin.image_remix.planning.Planner', side_effect=AssertionError('No replanning')):
            _, clients, _ = self.run_complete([completion_target(job)])
        latest = self.store.get(job['id'])
        self.assertEqual(latest['planning'], job['planning']); self.assertEqual(latest['references'], job['references'])
        self.assertEqual(clients[0].uploads[0][2], 'user_data')
        self.assertEqual(clients[0].uploads[0][0], snapshot)

    def test_running_unknown_collecting_attention_and_pending_never_retry(self):
        job = self.partial()
        for status in ('running', 'unknown', 'submitting', 'collecting', 'attention', 'ready', 'planning', 'done'):
            changed = deepcopy(job); changed['status'] = status
            with self.subTest(status=status): self.assertIsNone(completion_target(changed))
        for item_status in ('pending', 'submitting', 'unknown', 'received'):
            changed = deepcopy(job); changed['items'][1]['status'] = item_status
            with self.subTest(item_status=item_status): self.assertIsNone(completion_target(changed))

    def test_unfinished_attempt_or_planning_never_retry(self):
        job = self.partial()
        for state in ('cloud', 'submitting', 'prepared', 'unknown'):
            changed = deepcopy(job); changed['attempts'][-1]['state'] = state
            self.assertIsNone(completion_target(changed))
        job['planning'] = {'state': 'failed'}; self.assertIsNone(completion_target(job))

    def test_instant_uses_its_existing_separate_retry_flow(self):
        job = self.partial(); job['spec']['mode'] = 'instant'
        self.assertIsNone(completion_target(job))

    def test_cancelled_and_definitely_failed_batches_can_be_completed(self):
        for status in ('cancelled', 'failed', 'partial'):
            job = self.partial(); job['status'] = status
            self.assertEqual(len(completion_target(job)['item_ids']), 9)

    def test_empty_or_duplicate_confirmation_rejected_without_network(self):
        target = completion_target(self.partial())
        for targets in ([], [target, target], ['invalid'], [{'id': []}]):
            with self.subTest(targets=targets), patch('app_plugins.builtin.image_remix.service.BatchClient') as client:
                with self.assertRaises(ValueError):
                    run_operation({'root': str(self.store.root), 'action': 'complete', 'targets': targets}, self.context)
                client.assert_not_called()

    def test_repeated_click_payload_never_submits_twice(self):
        target = completion_target(self.partial())
        self.run_complete([target])
        result, clients, _ = self.run_complete([target])
        self.assertFalse(clients[0].creates); self.assertIn('跳过', result['messages'][0])
        self.assertEqual(len(self.store.get(target['id'])['attempts']), 2)

    def test_new_finished_attempt_invalidates_old_confirmation(self):
        job = self.partial(); target = completion_target(job)
        new_attempt(job, retry=True)
        for item in job['items'][1:]: item['status'] = 'failed'
        job['attempts'][-1]['state'] = 'finished'; job['status'] = 'partial'; self.store.save(job)
        _, clients, _ = self.run_complete([target]); self.assertFalse(clients[0].creates)

    def test_changed_missing_count_invalidates_confirmation(self):
        job = self.partial(); target = completion_target(job)
        job['items'][1]['status'] = 'done'; self.store.save(job)
        _, clients, _ = self.run_complete([target]); self.assertFalse(clients[0].creates)

    def test_bulk_one_bad_key_does_not_block_other_job(self):
        first = self.partial(); second = self.partial(count=4)
        result, clients, _ = self.run_complete(completion_targets([first, second]), clients=[FakeClient()],
            side_effect=[ValueError('原账号已移除'), {}])
        self.assertEqual(len(clients[0].creates), 1)
        self.assertEqual(len(self.store.get(first['id'])['attempts']), 1)
        self.assertEqual(len(self.store.get(second['id'])['attempts']), 2)
        self.assertIn('原账号已移除', result['messages'][0])

    def test_bulk_deleted_row_does_not_block_other_job(self):
        first = self.partial(); second = self.partial()
        targets = completion_targets([first, second]); self.store.delete(first['id'])
        result, clients, _ = self.run_complete(targets, clients=[FakeClient()])
        self.assertEqual(len(clients[0].creates), 1); self.assertIn('已不存在', result['messages'][0])

    def test_timeout_after_post_is_unknown_never_automatically_resent(self):
        job = self.partial(); target = completion_target(job)
        client = FakeClient(); client.create_error = APIError('timeout')
        self.run_complete([target], clients=[client]); latest = self.store.get(job['id'])
        self.assertEqual(latest['status'], 'unknown'); self.assertEqual(len(client.creates), 1)
        _, clients, _ = self.run_complete([target]); self.assertFalse(clients[0].creates)
        self.assertEqual(self.store.get(job['id'])['items'][0], job['items'][0])

    def test_partial_is_already_settled_and_manual_check_does_not_generate(self):
        job = self.partial(); client = FakeClient()
        Service(self.store, client).advance(job)
        self.assertFalse(client.creates); self.assertEqual(job['status'], 'partial')


class CompletionUITests(unittest.TestCase):
    partial = CompletionTests.partial

    def setUp(self):
        CompletionTests.setUp(self)
        from app_plugins.builtin.image_remix.plugin import ImageRemixPlugin
        from app_plugins.builtin.image_remix.ui import RemixDialog
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.parent = QtWidgets.QWidget(); self.plugin = ImageRemixPlugin()
        self.plugin.context = SimpleNamespace(storage=data_directory(self.store.root))
        self.plugin.launch = Mock(return_value=True)
        self.dialog = RemixDialog(self.plugin, self.parent)
        self.addCleanup(self.cleanup_ui)

    def cleanup_ui(self):
        self.parent.deleteLater(); self.plugin.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()

    def test_visible_button_shows_nine_and_terminal_explanation(self):
        job = self.partial(); self.dialog.refresh([job], '', False)
        self.assertTrue(self.dialog.complete_button.isEnabled())
        self.assertIn('9', self.dialog.complete_button.text())
        self.assertIn('可补全 9 张', self.dialog.history.item(0, 2).text())
        self.assertIn('继续等待不会自动补全', self.dialog.history.item(0, 2).toolTip())

    def test_cancel_confirmation_never_launches(self):
        self.dialog.refresh([self.partial()], '', False)
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No):
            self.dialog.complete_button.click()
        self.plugin.launch.assert_not_called()

    def test_selected_confirmed_count_and_payload(self):
        job = self.partial(); self.dialog.refresh([job], '', False)
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes) as ask:
            self.dialog.complete_button.click()
        self.assertIn('共 9 张', ask.call_args.args[2]); self.assertIn('新增费用', ask.call_args.args[2])
        self.plugin.launch.assert_called_once_with('complete', targets=[completion_target(job)])

    def test_all_menu_filters_running_and_unknown_batches(self):
        a = self.partial(); b = self.partial(count=4); running = self.partial(); running['status'] = 'running'
        unknown = self.partial(); unknown['status'] = 'unknown'
        self.dialog.refresh([a, running, b, unknown], '', False)
        self.assertIn('2 批／12 张', self.dialog.complete_all.text())
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.dialog.complete_all.trigger()
        self.plugin.launch.assert_called_once_with('complete', targets=completion_targets([a, b]))

    def test_busy_disables_completion(self):
        self.dialog.refresh([self.partial()], '', True)
        self.assertFalse(self.dialog.complete_button.isEnabled()); self.assertFalse(self.dialog.complete_all.isEnabled())

    def test_other_eligible_rows_use_menu_instead_of_wrong_current_row(self):
        running = self.partial(); running['status'] = 'running'
        self.dialog.refresh([running, self.partial()], '', False)
        self.assertFalse(self.dialog.complete_current.isEnabled()); self.assertTrue(self.dialog.complete_all.isEnabled())
        self.assertEqual(self.dialog.complete_button.popupMode(), QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)

    def test_new_ui_refuses_old_submission_runtime(self):
        from app_plugins.builtin.image_remix.ui import RemixDialog
        with self.assertRaises(RuntimeError): RemixDialog(SimpleNamespace(submission_api_version=5), self.parent)


if __name__ == '__main__': unittest.main()
