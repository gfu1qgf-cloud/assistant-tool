from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from model.AppDataStorage import data_file, data_directory
from app_plugins.builtin.image_remix.client import BatchClient, APIError
from app_plugins.builtin.image_remix.instant import InstantService
from app_plugins.builtin.image_remix.service import Service, run_operation
from app_plugins.builtin.image_remix.store import Store, summary
from app_plugins.builtin.image_remix.prompts import validate_spec
from test_image_remix import FakeClient, spec, png
import base64


class DirectClient(FakeClient):
    def __init__(self):
        super().__init__(); self.calls = []; self.error = None; self.before = lambda: None

    def generate(self, body, *, references=False):
        self.calls.append((deepcopy(body), references)); self.before()
        if self.error: raise self.error
        return {'data': [{'b64_json': base64.b64encode(png()).decode()}], 'usage': {'total_tokens': 12}}


class InstantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root / 'private'); self.client = DirectClient()
        self.shared = Service(self.store, self.client)
        self.direct = InstantService(self.store, self.client, self.shared)
        self.job = self.store.create(spec(self.root, mode='instant'), 'identity')

    def tearDown(self): self.temp.cleanup()
    def reload(self): return self.store.get(self.job['id'])
    def advance(self): self.job = self.direct.advance(self.reload()); return self.job

    def test_missing_mode_is_legacy_batch_and_invalid_mode_rejected(self):
        self.assertEqual(validate_spec(spec(self.root))['mode'], 'batch')
        with self.assertRaises(ValueError): validate_spec(spec(self.root, mode='oops'))

    def test_instant_marks_storage_v2_without_being_reset_on_reopen(self):
        reopened = Store(self.root / 'private')
        with reopened.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_one_standard_request_per_round_and_immediate_saved_image(self):
        job = self.advance()
        self.assertEqual(job['status'], 'ready'); self.assertEqual(summary(job)[:2], (1, 0))
        self.assertTrue(Path(job['items'][0]['path']).is_file())
        self.assertEqual(len(self.client.calls), 1); self.assertFalse(self.client.creates)
        self.assertFalse(self.client.uploads)
        self.assertEqual(self.client.calls[0][0]['n'], 1)

    def test_all_rounds_save_in_order_without_resending_completed_images(self):
        for _ in range(3): self.advance()
        self.assertEqual(self.job['status'], 'done'); self.assertEqual(summary(self.job)[:2], (3, 0))
        self.assertEqual(len(self.client.calls), 3)
        self.advance(); self.assertEqual(len(self.client.calls), 3)

    def test_per_image_intent_precedes_billable_post(self):
        def before():
            saved = self.reload()
            self.assertEqual(saved['items'][0]['status'], 'submitting')
            self.assertEqual(saved['attempts'][-1]['requests'][saved['items'][0]['id']]['state'], 'submitting')
        self.client.before = before; self.advance()

    def test_reference_is_uploaded_once_and_reused_in_edit_json(self):
        path = self.root / 'face.png'; data_file(path).write_bytes(png())
        self.job = self.store.create(spec(self.root, mode='instant', references=[str(path)]), 'identity')
        self.advance(); self.advance()
        self.assertEqual(len(self.client.uploads), 1)
        self.assertTrue(all(refs for body, refs in self.client.calls))
        self.assertEqual(self.client.calls[0][0]['images'], [{'file_id': 'file-1'}])
        self.assertFalse(self.client.creates)

    def test_timeout_is_unknown_and_never_auto_reposted(self):
        self.client.error = APIError('response lost')
        self.advance(); self.assertEqual(self.job['status'], 'unknown')
        self.assertEqual(self.job['items'][0]['status'], 'unknown')
        for _ in range(3): self.advance()
        self.assertEqual(len(self.client.calls), 1)

    def test_restart_during_request_without_receipt_is_unknown(self):
        attempt = self.direct.attempt(self.job)
        item = self.job['items'][0]; item['status'] = 'submitting'
        attempt['requests'][item['id']] = {'state': 'submitting'}
        self.store.save(self.job); self.advance()
        self.assertEqual(self.job['status'], 'unknown'); self.assertFalse(self.client.calls)

    def test_saved_response_survives_output_failure_and_replays_without_post(self):
        with patch.object(self.shared, 'accept_line', side_effect=OSError('disk blocked')):
            with self.assertRaises(OSError): self.advance()
        self.assertEqual(self.reload()['items'][0]['status'], 'received')
        self.advance()
        self.assertEqual(len(self.client.calls), 1); self.assertEqual(summary(self.job)[0], 1)

    def test_receipt_write_failure_is_not_a_license_to_regenerate(self):
        with patch('model.AppDataStorage.ManagedFile.write_json', side_effect=OSError('disk blocked')):
            with self.assertRaises(OSError): self.advance()
        self.advance()
        self.assertEqual(self.job['status'], 'unknown'); self.assertEqual(len(self.client.calls), 1)

    def test_successful_receipt_removed_after_verified_output_to_save_space(self):
        job = self.advance(); attempt = job['attempts'][-1]
        self.assertFalse(self.direct.receipt(job, attempt, job['items'][0]).exists())
        metadata = data_file(Path(job['output']) / '0001.json').read_json()
        self.assertEqual(metadata['submission_mode'], 'instant')
        self.assertEqual(metadata['usage']['total_tokens'], 12)

    def test_unknown_request_can_leave_pending_items_to_explicit_resume(self):
        self.client.error = APIError('timeout'); self.advance()
        self.client.error = None
        self.direct.resume(self.job)
        self.store.save(self.job)
        self.advance(); self.advance()
        self.assertEqual(summary(self.job)[0], 2)
        self.assertEqual(self.job['items'][0]['status'], 'unknown')
        self.assertEqual(len(self.client.calls), 3)

    def test_explicit_unknown_retry_uses_new_attempt_not_old_receipt(self):
        self.client.error = APIError('timeout'); self.advance()
        old = self.job['attempts'][-1]['token']; self.client.error = None
        self.direct.resume(self.job, retry=True, include_unknown=True)
        self.assertNotEqual(old, self.job['attempts'][-1]['token'])
        self.advance(); self.assertEqual(summary(self.job)[0], 1)

    def test_definite_quota_failure_pauses_without_cascading_to_remaining(self):
        self.client.error = APIError('quota', definite=True, status=429); self.advance()
        self.assertEqual(self.job['status'], 'paused'); self.assertEqual(self.job['items'][0]['status'], 'failed')
        self.advance(); self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(sum(i['status'] == 'pending' for i in self.job['items']), 2)

    def test_stop_while_request_in_flight_keeps_returned_image_but_stops_following(self):
        self.client.before = lambda: self.store.request_stop(self.job['id'])
        self.advance(); self.assertEqual(self.job['status'], 'paused'); self.assertEqual(summary(self.job)[0], 1)
        self.advance(); self.assertEqual(len(self.client.calls), 1)

    def test_stop_before_dispatch_has_no_upload_or_generation(self):
        self.store.request_stop(self.job['id']); self.advance()
        self.assertEqual(self.job['status'], 'paused'); self.assertFalse(self.client.calls)

    def test_live_stages_only_report_saved_after_verified_file_write(self):
        stages = []
        self.direct.stage = stages.append; self.advance()
        self.assertEqual(stages, ['instant:running', 'instant:saved:0'])

    def test_resume_after_stop_does_not_repeat_saved_picture(self):
        self.advance(); self.store.request_stop(self.job['id']); self.advance()
        self.direct.resume(self.job); self.advance()
        self.assertEqual(summary(self.job)[0], 2); self.assertEqual(len(self.client.calls), 2)

    def test_instant_cannot_take_over_batch_attempt(self):
        self.job['attempts'] = [{'token': 'old', 'state': 'cloud'}]; self.store.save(self.job)
        with self.assertRaises(ValueError): self.advance()
        self.assertFalse(self.client.calls)

    def test_corrupt_receipt_rejected_without_generation(self):
        attempt = self.direct.attempt(self.job)
        self.direct.receipt(self.job, attempt, self.job['items'][0]).write_json({'custom_id': 'foreign'})
        with self.assertRaises(ValueError): self.advance()
        self.assertFalse(self.client.calls)

    def test_worker_dispatches_instant_and_requests_continuation(self):
        context = SimpleNamespace(check_canceled=Mock(), report=Mock())
        with patch('app_plugins.builtin.image_remix.service.credential_config', return_value={}), patch(
                'app_plugins.builtin.image_remix.service.read_provider_key', return_value='SYNTHETIC'), patch(
                'app_plugins.builtin.image_remix.service.BatchClient', return_value=self.client):
            result = run_operation({'root': str(self.root / 'private'), 'id': self.job['id']}, context)
        self.assertTrue(result['auto_continue']); self.assertEqual(len(self.client.calls), 1)
        self.assertFalse(self.client.creates)

    def test_followup_rounds_do_not_touch_legacy_batch_jobs(self):
        old = self.store.create(spec(self.root), 'identity')
        context = SimpleNamespace(check_canceled=Mock(), report=Mock())
        with patch('app_plugins.builtin.image_remix.service.credential_config', return_value={}), patch(
                'app_plugins.builtin.image_remix.service.read_provider_key', return_value='SYNTHETIC'), patch(
                'app_plugins.builtin.image_remix.service.BatchClient', return_value=self.client):
            run_operation({'root': str(self.root / 'private'), 'instant_only': True}, context)
        self.assertEqual(self.store.get(old['id'])['attempts'], []); self.assertFalse(self.client.creates)


class DirectClientTests(unittest.TestCase):
    def test_standard_endpoints_have_no_batch_and_no_retry(self):
        session = Mock(); session.headers = {}; response = Mock(status_code=200)
        response.json.return_value = {'data': [{'b64_json': 'x'}]}; session.request.return_value = response
        client = BatchClient('SYNTHETIC', session=session)
        client.generate({'prompt': 'text'}); client.generate({'prompt': 'text', 'images': []}, references=True)
        self.assertEqual(session.request.call_args_list[0].args[1], 'https://api.openai.com/v1/images/generations')
        self.assertEqual(session.request.call_args_list[1].args[1], 'https://api.openai.com/v1/images/edits')
        self.assertEqual(session.request.call_args.kwargs['timeout'], (10, 180))
        self.assertEqual(session.request.call_count, 2)


class InstantUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        from app_plugins.builtin.image_remix.plugin import ImageRemixPlugin
        from app_plugins.builtin.image_remix.ui import RemixDialog
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.parent = QtWidgets.QWidget(); self.plugin = ImageRemixPlugin()
        self.plugin.context = SimpleNamespace(storage=data_directory(self.root / 'private'))
        self.dialog = RemixDialog(self.plugin, self.parent); self.plugin.dialog = self.dialog

    def tearDown(self):
        self.plugin.stopping = True; self.dialog.close(); self.parent.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents(); self.temp.cleanup()

    def test_new_submission_default_is_instant_and_batch_remains_available(self):
        self.assertEqual(self.dialog.spec()['mode'], 'instant')
        self.assertIn('标准价', self.dialog.mode_note.text())
        self.dialog.mode.setCurrentIndex(1)
        self.assertEqual(self.dialog.spec()['mode'], 'batch'); self.assertIn('Batch', self.dialog.generate.text())

    def test_new_ui_rejects_running_old_submitter_instead_of_mixing_prices(self):
        from app_plugins.builtin.image_remix.ui import RemixDialog
        with self.assertRaises(RuntimeError): RemixDialog(SimpleNamespace(), self.parent)

    def test_draft_and_reuse_keep_explicit_mode(self):
        self.dialog.load_draft(spec(self.root, mode='batch'))
        self.assertEqual(self.dialog.spec()['mode'], 'batch')

    def test_old_draft_without_mode_does_not_silently_become_standard_price(self):
        self.dialog.load_draft(spec(self.root))
        self.assertEqual(self.dialog.spec()['mode'], 'batch')

    def test_switching_editor_mode_does_not_mutate_existing_batch(self):
        store = Store(self.root / 'private'); job = store.create(spec(self.root), 'identity')
        self.dialog.refresh([job], '', False); self.dialog.mode.setCurrentIndex(0)
        self.assertEqual(store.get(job['id'])['spec']['mode'], 'batch')
        self.assertIn('Batch', self.dialog.history.item(0, 2).text())

    def test_confirmation_explicitly_warns_standard_price(self):
        self.dialog.prompt.setPlainText('a landscape'); self.dialog.output.setText(str(self.root))
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No) as question:
            self.dialog.generate_clicked()
        self.assertIn('不享受Batch折扣', question.call_args.args[2])

    def test_finished_schedules_next_instant_step_but_not_batch_conversion(self):
        handle = Mock(); self.plugin.handle = handle; self.plugin.auto_continue = True
        with patch.object(QtCore.QTimer, 'singleShot') as shot:
            self.plugin.finished(handle)
        self.assertEqual(shot.call_args.args[0], 0)
        with patch.object(self.plugin, 'check') as check:
            shot.call_args.args[1]()
        check.assert_called_once_with(instant_only=True)

    def test_stopping_plugin_does_not_schedule_more_paid_work(self):
        handle = Mock(); self.plugin.handle = handle; self.plugin.auto_continue = True; self.plugin.stopping = True
        with patch.object(QtCore.QTimer, 'singleShot') as shot: self.plugin.finished(handle)
        shot.assert_not_called()

    def test_live_badge_keeps_saved_count_and_does_not_claim_complete(self):
        store = Store(self.root / 'private'); job = store.create(spec(self.root, mode='instant'), 'identity')
        self.plugin.jobs = [job]; self.dialog.refresh([job], '', True)
        self.plugin.item_stage(job['id'], 'instant:running')
        self.plugin.item_stage(job['id'], 'instant:saved:0')
        self.assertIn('已收取 1/3', self.dialog.history.item(0, 2).text())
        self.assertNotIn('已完成', self.dialog.history.item(0, 2).text())


if __name__ == '__main__': unittest.main()
