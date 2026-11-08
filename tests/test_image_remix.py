import base64
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PIL import Image
import requests
from model.AppDataStorage import data_directory, data_file
from model.AIProviderKeys import KEYS_CONFIG_KEY, provider_key_identity
from app_plugins.builtin.image_remix.store import Store, summary
from app_plugins.builtin.image_remix.prompts import validate_spec, variant_prompt, MODELS
from app_plugins.builtin.image_remix.service import (Service, new_attempt, batch_lines,
    credential_config, credential_identity, run_operation)
from app_plugins.builtin.image_remix.client import BatchClient, APIError


def png():
    output = io.BytesIO(); Image.new('RGB', (32, 48), '#1298de').save(output, 'PNG')
    return output.getvalue()


def spec(root, **values):
    return {'prompt': '耶稣抱着白鸽，湖边，柔和晨光，写实风格', 'count': 3,
            'innovation': 20, 'quality': 'medium', 'size': '1024x1536',
            'model': MODELS[0], 'output': str(root), 'references': [], **values}


class FakeClient:
    def __init__(self):
        self.uploads = []; self.creates = []; self.results = {}; self.recovered = None
        self.cloud = {'id': 'batch_test', 'status': 'in_progress', 'request_counts': {'completed': 0}}
        self.create_error = None; self.closed = False; self.cancels = []

    def upload(self, raw, name, purpose):
        self.uploads.append((raw, name, purpose)); return 'file-' + str(len(self.uploads))

    def create(self, identifier, metadata):
        self.creates.append((identifier, metadata))
        if self.create_error: raise self.create_error
        return deepcopy(self.cloud)

    def retrieve(self, identifier): return deepcopy(self.cloud)
    def recover(self, *args): return deepcopy(self.recovered)
    def cancel_batch(self, identifier): self.cancels.append(identifier)
    def lines(self, identifier): yield from self.results[identifier]
    def close(self): self.closed = True


class RemixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'private')
        self.client = FakeClient()
        self.service = Service(self.store, self.client)

    def tearDown(self): self.temp.cleanup()
    def create(self, **values): return self.store.create(spec(self.root, **values), 'fingerprint')

    def submitted(self, **values):
        job = self.create(**values); self.service.advance(job); return job

    def row(self, job, index, *, raw=None):
        attempt = job['attempts'][-1]
        return {'custom_id': attempt['token'] + ':' + job['items'][index]['id'],
                'response': {'status_code': 200, 'body': {'data': [
                    {'b64_json': base64.b64encode(raw or png()).decode()}], 'usage': {'total_tokens': 5}}}, 'error': None}

    def finish(self, job, rows=None, status='completed'):
        self.client.cloud.update(status=status, output_file_id='file-output')
        self.client.results['file-output'] = rows if rows is not None else [self.row(job, i) for i in range(len(job['items']))]
        return self.service.advance(self.store.get(job['id']))

    def test_validation_rejects_empty_prompt_and_bad_counts(self):
        for value in ('', ' ' * 10, 'a' * 12001):
            with self.assertRaises(ValueError): validate_spec(spec(self.root, prompt=value))
        for count in (0, 501, True, 2.5):
            with self.assertRaises(ValueError): validate_spec(spec(self.root, count=count))

    def test_no_silent_unsupported_model_or_size_fallback(self):
        for values in ({'model': 'text-model'}, {'size': '100x1'}, {'quality': 'ultra'}, {'innovation': -1}):
            with self.assertRaises(ValueError): validate_spec(spec(self.root, **values))

    def test_creation_is_durable_before_network_and_unique_output(self):
        a = self.create(); b = self.create()
        self.assertNotEqual(a['output'], b['output'])
        self.assertEqual(Store(self.root / 'private').get(a['id'])['spec']['prompt'], a['spec']['prompt'])
        self.assertEqual(self.client.creates, [])
        self.assertNotIn('API_KEY', json.dumps(self.store.all()))

    def test_double_delivery_id_is_idempotent(self):
        a = self.create()
        b = self.store.create(spec(self.root), 'fingerprint', identifier=a['id'])
        self.assertEqual(a, b); self.assertEqual(len(self.store.all()), 1)

    def test_enqueue_is_durable_without_decoding_on_gui_thread(self):
        with patch('app_plugins.builtin.image_remix.store.normalize_image', side_effect=AssertionError('GUI decode')):
            job = self.store.create(spec(self.root, references=['not-yet-read.png']), 'identity', defer_snapshot=True)
        self.assertTrue(self.store.get(job['id'])['snapshot_pending'])
        self.assertEqual(job['items'][0]['prompt'], '')

    def test_deferred_snapshot_is_completed_before_upload(self):
        path = self.root / 'person.png'; data_file(path).write_bytes(png())
        job = self.store.create(spec(self.root, references=[str(path)]), 'identity', defer_snapshot=True)
        self.service.advance(job)
        saved = self.store.get(job['id'])
        self.assertFalse(saved['snapshot_pending']); self.assertTrue(saved['items'][0]['prompt'])
        self.assertEqual(len(saved['references']), 1)

    def test_output_requires_explicit_valid_folder(self):
        for folder in ('', 'relative', str(self.root / 'missing')):
            with self.assertRaises(ValueError): self.create(output=folder)

    def test_low_and_high_innovation_are_distinct_visible_recipes(self):
        low = variant_prompt(spec(self.root, innovation=0), 0)
        high = variant_prompt(spec(self.root, innovation=100), 0)
        self.assertIn('不改变主题', low); self.assertIn('自主重构', high)
        self.assertNotEqual(variant_prompt(spec(self.root), 0), variant_prompt(spec(self.root), 1))

    def test_reference_identity_is_protected_even_at_high_innovation(self):
        prompt = variant_prompt(spec(self.root, innovation=100, references=['face.png']), 1)
        self.assertIn('不授权改变人物身份', prompt)

    def test_reference_snapshot_survives_original_change(self):
        path = self.root / 'person.png'; data_file(path).write_bytes(png())
        job = self.create(references=[str(path)])
        raw = self.store.asset(job, job['references'][0])
        data_file(path).write_bytes(b'changed')
        self.assertEqual(self.store.asset(job, job['references'][0]), raw)

    def test_reference_corruption_is_not_silently_ignored(self):
        path = self.root / 'person.png'; data_file(path).write_bytes(png())
        job = self.create(references=[str(path)])
        saved = self.root / 'private' / job['id'] / 'references' / job['references'][0]['file']
        data_file(saved).write_bytes(b'broken')
        with self.assertRaises(ValueError): self.service.advance(job)
        self.assertFalse(self.client.creates)

    def test_text_only_uses_generations_batch(self):
        job = self.submitted()
        endpoint, raw = batch_lines(job, job['attempts'][-1])
        self.assertEqual(endpoint, '/v1/images/generations')
        lines = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(row['body']['n'] == 1 and 'images' not in row['body'] for row in lines))
        self.assertEqual(len(self.client.creates), 1)

    def test_reference_uses_edits_json_file_id_and_uploaded_once(self):
        path = self.root / 'face.png'; data_file(path).write_bytes(png())
        job = self.submitted(references=[str(path)])
        endpoint, raw = batch_lines(job, job['attempts'][-1])
        self.assertEqual(endpoint, '/v1/images/edits')
        self.assertEqual([row[2] for row in self.client.uploads], ['user_data', 'batch'])
        for row in map(json.loads, raw.splitlines()):
            self.assertEqual(row['body']['images'], [{'file_id': 'file-1'}])

    def test_durable_intent_precedes_create_post(self):
        job = self.create()
        original = self.client.create
        def create(*args):
            saved = self.store.get(job['id'])
            self.assertEqual(saved['attempts'][-1]['state'], 'submitting')
            return original(*args)
        self.client.create = create
        self.service.advance(job)

    def test_timeout_never_automatically_reposts(self):
        self.client.create_error = APIError('timeout')
        job = self.submitted()
        self.assertEqual(job['status'], 'unknown')
        for _ in range(3): self.service.advance(self.store.get(job['id']))
        self.assertEqual(len(self.client.creates), 1)
        with self.assertRaises(ValueError): new_attempt(job, retry=True)

    def test_restart_recovers_lost_batch_response_by_token(self):
        self.client.create_error = APIError('timeout')
        job = self.submitted(); self.client.recovered = deepcopy(self.client.cloud)
        resumed = Service(Store(self.root / 'private'), self.client).advance(self.store.get(job['id']))
        self.assertEqual(resumed['attempts'][-1]['batch_id'], 'batch_test')
        self.assertEqual(len(self.client.creates), 1)

    def test_definite_rejection_fails_items_and_requires_explicit_retry(self):
        self.client.create_error = APIError('403', definite=True)
        job = self.submitted()
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(summary(job)[:2], (0, 3))
        self.service.advance(job); self.assertEqual(len(self.client.creates), 1)

    def test_upload_permission_failure_does_not_loop_automatically(self):
        self.client.upload = Mock(side_effect=APIError('quota', definite=True))
        job = self.submitted()
        self.assertEqual(job['status'], 'failed')
        self.service.advance(job); self.assertEqual(self.client.upload.call_count, 1)
        self.assertFalse(self.client.creates)

    def test_restart_does_not_resubmit_known_batch(self):
        job = self.submitted()
        self.service.advance(self.store.get(job['id']))
        self.assertEqual(len(self.client.creates), 1)

    def test_shuffled_results_map_by_custom_id(self):
        job = self.submitted()
        result = self.finish(job, [self.row(job, i) for i in (2, 0, 1)])
        self.assertEqual(result['status'], 'done')
        for item in result['items']:
            self.assertEqual(Path(item['path']).name, f'{item["index"] + 1:04d}.png')
            self.assertTrue(Path(item['path']).is_file())

    def test_download_replay_does_not_duplicate_images_or_generate(self):
        job = self.submitted(); result = self.finish(job)
        before = sorted(Path(result['output']).iterdir())
        result['status'] = 'collecting'; result['attempts'][-1]['state'] = 'cloud'; self.store.save(result)
        result = self.service.advance(result)
        self.assertEqual(before, sorted(Path(result['output']).iterdir()))
        self.assertEqual(len(self.client.creates), 1)

    def test_missing_results_are_failed_not_success(self):
        job = self.submitted(); result = self.finish(job, [self.row(job, 0)])
        self.assertEqual(result['status'], 'partial'); self.assertEqual(summary(result)[:2], (1, 2))

    def test_error_file_is_consumed(self):
        job = self.submitted()
        self.client.cloud['error_file_id'] = 'file-errors'
        row = {'custom_id': job['attempts'][-1]['token'] + ':' + job['items'][1]['id'],
               'error': {'code': 'moderation_blocked'}}
        self.client.results['file-errors'] = [row]
        result = self.finish(job, [self.row(job, 0), self.row(job, 2)])
        self.assertEqual(summary(result)[:2], (2, 1))
        self.assertIn('moderation_blocked', result['items'][1]['error'])

    def test_foreign_result_is_rejected(self):
        job = self.submitted(); row = self.row(job, 0); row['custom_id'] = 'another:job'
        with self.assertRaises(ValueError): self.finish(job, [row])
        self.assertFalse(list(Path(job['output']).glob('*.png')))

    def test_foreign_batch_id_is_rejected(self):
        job = self.submitted(); self.client.cloud['id'] = 'batch_other'
        with self.assertRaises(ValueError): self.service.advance(job)

    def test_duplicate_result_id_is_rejected(self):
        job = self.submitted()
        with self.assertRaises(ValueError): self.finish(job, [self.row(job, 0), self.row(job, 0)])

    def test_one_broken_result_does_not_block_other_images(self):
        job = self.submitted(); result = self.finish(job, [self.row(job, 0, raw=b'bad'), self.row(job, 1), self.row(job, 2)])
        self.assertEqual(summary(result)[:2], (2, 0)); self.assertEqual(result['status'], 'collecting')
        for _ in range(2): result = self.service.advance(result)
        self.assertEqual(result['status'], 'attention')
        self.assertEqual(len(self.client.creates), 1)

    def test_existing_different_file_is_preserved(self):
        job = self.submitted(); path = Path(job['output']) / '0001.png'
        data_file(path).write_bytes(b'user file')
        result = self.finish(job)
        self.assertEqual(data_file(path).read_bytes(), b'user file')
        self.assertEqual(summary(result)[0], 2)

    def test_retry_contains_only_failed_items_and_new_attempt_token(self):
        job = self.submitted(); job = self.finish(job, [self.row(job, 0)])
        old = job['attempts'][-1]['token']; attempt = new_attempt(job, retry=True)
        self.assertNotEqual(old, attempt['token'])
        endpoint, raw = batch_lines(job, attempt)
        self.assertEqual(len(raw.splitlines()), 2)
        self.assertNotIn(job['items'][0]['id'].encode(), raw)

    def test_old_attempt_results_cannot_poison_retry(self):
        job = self.submitted(); old_row = self.row(job, 1)
        job = self.finish(job, [self.row(job, 0)]); new_attempt(job, retry=True)
        with self.assertRaises(ValueError): self.service.accept_line(job, job['attempts'][-1], old_row)

    def test_cancel_keeps_and_collects_completed_results(self):
        job = self.submitted(); job['cancel_requested'] = True; self.store.save(job)
        job = self.finish(job, [self.row(job, 0)], status='cancelled')
        self.assertEqual(job['status'], 'cancelled'); self.assertEqual(summary(job)[:2], (1, 2))
        self.assertEqual(self.client.cancels, ['batch_test'])

    def test_cancel_before_submission_has_no_paid_post(self):
        job = self.create(); job['cancel_requested'] = True
        self.service.advance(job)
        self.assertEqual(job['status'], 'cancelled'); self.assertFalse(self.client.creates)

    def test_terminal_delete_retains_output(self):
        job = self.submitted()
        with self.assertRaises(ValueError): self.store.delete(job['id'])
        job = self.finish(job); self.store.delete(job['id'])
        self.assertTrue(Path(job['items'][0]['path']).is_file()); self.assertEqual(self.store.all(), [])

    def test_draft_roundtrip_is_managed_and_not_a_job(self):
        self.store.draft(spec(self.root))
        self.assertEqual(Store(self.root / 'private').draft(), spec(self.root))
        self.assertEqual(self.store.all(), [])

    def test_runtime_lock_rejects_concurrent_process_style_access(self):
        with self.store.runtime_lock():
            with self.assertRaises(OSError):
                with Store(self.root / 'private').runtime_lock(): pass

    def test_changed_active_key_can_resume_with_original_saved_key(self):
        pool = {'version': 1, 'active': 'b', 'items': [
            {'id': 'a', 'encrypted': 'dpapi:old'}, {'id': 'b', 'encrypted': 'dpapi:new'}]}
        old = {KEYS_CONFIG_KEY: {'openai': 'dpapi:old'}}
        pinned = credential_config({KEYS_CONFIG_KEY: {'openai': pool}}, provider_key_identity(old, 'openai'))
        self.assertEqual(pinned, old)

    def test_removed_original_key_does_not_use_other_account(self):
        with self.assertRaises(ValueError):
            credential_config({KEYS_CONFIG_KEY: {'openai': 'dpapi:new'}}, 'wrong')

    def test_reentering_same_real_key_can_resume_despite_new_dpapi_ciphertext(self):
        from model.AIProviderKeys import protect_key
        old = {KEYS_CONFIG_KEY: {'openai': protect_key('synthetic-same-key-123456')}}
        new = {KEYS_CONFIG_KEY: {'openai': protect_key('synthetic-same-key-123456')}}
        identity = credential_identity(old)
        self.assertEqual(credential_config(new, identity), new)

    def test_empty_check_does_not_call_network_or_need_key(self):
        context = SimpleNamespace(check_canceled=Mock(), report=Mock())
        with patch('app_plugins.builtin.image_remix.service.BatchClient') as client:
            result = run_operation({'root': str(self.root / 'empty')}, context)
        self.assertEqual(result['jobs'], []); client.assert_not_called()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(); self.session.headers = {}
        self.client = BatchClient('TEST_NOT_REAL', session=self.session)

    def test_billable_post_has_timeout_and_no_retry(self):
        self.session.request.side_effect = requests.Timeout()
        with self.assertRaises(APIError) as error:
            self.client.create('file-test', {'endpoint': '/v1/images/generations'})
        self.assertFalse(error.exception.definite)
        self.assertEqual(self.session.request.call_count, 1)
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(kwargs['timeout'], (10, 60)); self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['json']['completion_window'], '24h')

    def test_http403_is_known_failure_not_retry(self):
        response = Mock(status_code=403); response.json.return_value = {'error': {'message': 'no permission'}}
        self.session.request.return_value = response
        with self.assertRaises(APIError) as error: self.client.retrieve('batch_test')
        self.assertTrue(error.exception.definite); self.assertEqual(self.session.request.call_count, 1)

    def test_nonofficial_path_and_malicious_ids_rejected(self):
        for path in ('https://example.org', '/../x'):
            with self.assertRaises(ValueError): self.client.request('GET', path)
        with self.assertRaises(ValueError): self.client.retrieve('../../other')
        self.session.request.assert_not_called()

    def test_json_lines_handles_arbitrary_stream_chunks(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b'{"a":', b'1}\n\n{"b":', b'2}']
        self.session.request.return_value = response
        self.assertEqual(list(self.client.lines('file-test')), [{'a': 1}, {'b': 2}])
        response.close.assert_called_once()

    def test_recovery_uses_pagination_and_matching_metadata(self):
        a = {'id': 'batch_a', 'created_at': 1000, 'metadata': {'remix_job': 'wrong', 'attempt': 't'}}
        b = {'id': 'batch_b', 'created_at': 1000, 'metadata': {'remix_job': 'j', 'attempt': 't'}}
        with patch.object(self.client, 'json', side_effect=[{'data': [a], 'has_more': True}, {'data': [b]}]) as call:
            self.assertEqual(self.client.recover('j', 't', 1000), b)
            self.assertEqual(call.call_args.kwargs['params']['after'], 'batch_a')


class UIPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        from qt_compat import QtWidgets
        from app_plugins.builtin.image_remix.plugin import ImageRemixPlugin
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.parent = QtWidgets.QWidget(); self.plugin = ImageRemixPlugin()
        self.context = SimpleNamespace(parent_widget=self.parent, storage=data_directory(self.root / 'private'),
            register_command=Mock(), register_settings_page=Mock(), load_config=lambda: {}, submit_background_task=Mock())
        self.plugin.register(self.context)

    def tearDown(self):
        self.plugin.poll.stop(); self.plugin.watch.stop(); self.parent.deleteLater()
        self.app.processEvents(); self.temp.cleanup()

    def test_plugin_registration_is_lazy_and_uses_public_command(self):
        self.assertIsNone(self.plugin.dialog)
        self.assertEqual(self.context.register_command.call_args.args[0].command_id, 'open')
        self.context.submit_background_task.assert_not_called()
        self.context.register_settings_page.assert_called_once()

    def test_empty_loaded_plugin_does_not_create_periodic_background_cards(self):
        self.plugin.loaded = True
        self.plugin.check()
        self.context.submit_background_task.assert_not_called()

    def test_submit_persists_before_host_queue_accepts(self):
        self.context.load_config = lambda: {KEYS_CONFIG_KEY: {'openai': 'dpapi:mock'}}
        self.context.submit_background_task.side_effect = RuntimeError('queue stopped')
        with patch('app_plugins.builtin.image_remix.plugin.credential_identity', return_value='identity'):
            with self.assertRaises(RuntimeError): self.plugin.submit(spec(self.root))
        jobs = Store(self.root / 'private').all()
        self.assertEqual(len(jobs), 1); self.assertEqual(jobs[0]['status'], 'planning')

    def test_settings_share_host_key_pool_and_preserve_other_providers(self):
        from app_plugins.builtin.image_remix.settings import RemixSettingsPage
        config = {KEYS_CONFIG_KEY: {'openai': 'dpapi:old', 'groq': 'dpapi:other'}}
        context = SimpleNamespace(load_config=lambda: deepcopy(config))
        page = RemixSettingsPage(context, self.parent)
        page.editor.dirty = True
        page.editor.value = lambda: 'dpapi:new'
        page.update_config(config)
        self.assertEqual(config[KEYS_CONFIG_KEY], {'openai': 'dpapi:new', 'groq': 'dpapi:other'})

    def test_settings_conflict_does_not_overwrite_other_window(self):
        from app_plugins.builtin.image_remix.settings import RemixSettingsPage
        page = RemixSettingsPage(self.context, self.parent)
        page.editor.dirty = True; page.editor.value = lambda: 'dpapi:new'
        config = {KEYS_CONFIG_KEY: {'openai': 'dpapi:changed'}}
        with self.assertRaises(ValueError): page.update_config(config)
        self.assertEqual(config[KEYS_CONFIG_KEY]['openai'], 'dpapi:changed')

    def test_new_package_uses_managed_file_io(self):
        from model.PluginStorageBoundary import direct_io_calls
        package = Path(__import__('app_plugins.builtin.image_remix', fromlist=['']).__file__).parent
        failures = []
        for path in package.glob('*.py'):
            failures.extend((path.name, line, call) for line, call in direct_io_calls(path.read_text(encoding='utf-8')))
        self.assertFalse(failures)

    def test_optional_reference_and_innovation_controls_are_editable(self):
        from app_plugins.builtin.image_remix.ui import RemixDialog
        dialog = RemixDialog(self.plugin, self.parent); self.plugin.dialog = dialog
        self.assertEqual(dialog.references.count(), 0)
        for value in (0, 30, 100):
            dialog.innovation.setValue(value); self.assertEqual(dialog.spec()['innovation'], value)
        self.assertTrue(dialog.prompt.isEnabled()); self.assertTrue(dialog.model.isEnabled())

    def test_history_never_labels_running_as_complete(self):
        from app_plugins.builtin.image_remix.ui import RemixDialog
        job = Store(self.root / 'private').create(spec(self.root), 'identity'); new_attempt(job)
        job['status'] = 'running'; job['attempts'][-1]['counts'] = {'completed': 1}
        dialog = RemixDialog(self.plugin, self.parent); self.plugin.dialog = dialog
        dialog.refresh([job], 'test', False)
        self.assertIn('已收取 0/3', dialog.history.item(0, 2).text())
        self.assertIn('云端完成 1', dialog.history.item(0, 2).text())
        self.assertNotIn('已完成', dialog.history.item(0, 2).text())

    def test_close_only_saves_draft_without_cancel(self):
        from app_plugins.builtin.image_remix.ui import RemixDialog
        dialog = RemixDialog(self.plugin, self.parent); self.plugin.dialog = dialog
        dialog.prompt.setPlainText('保留这个草稿'); self.plugin.handle = Mock()
        dialog.close()
        self.plugin.handle.requestInterruption.assert_not_called()
        self.assertEqual(Store(self.root / 'private').draft()['prompt'], '保留这个草稿')
        self.plugin.handle = None

    def test_manual_check_includes_attention_jobs(self):
        with patch.object(self.plugin, 'launch') as launch:
            self.plugin.check(manual=True)
        launch.assert_called_once_with('check', manual=True)

    def test_stale_finished_callback_cannot_clear_new_handle(self):
        new = Mock(); self.plugin.handle = new
        self.plugin.finished(Mock()); self.assertIs(self.plugin.handle, new)
        self.plugin.finished(new); self.assertIsNone(self.plugin.handle)

    def test_terminal_handle_reclaimed_without_global_idle(self):
        handle = Mock(); handle.isSettled.return_value = True; self.plugin.handle = handle
        self.assertFalse(self.plugin.busy())

    def test_cloud_wait_does_not_prevent_exit(self):
        self.plugin.jobs = [{'status': 'running'}]
        self.assertEqual(self.plugin.can_close(), (True, ''))

    def test_real_host_handle_completes_and_can_close_without_stale_ownership(self):
        from model.BackgroundTasks import BackgroundTaskManager
        manager = BackgroundTaskManager(history_path=self.root / 'background.json')
        self.context.submit_background_task = lambda **kwargs: manager.submit(owner='image_remix', **kwargs)
        try:
            self.assertTrue(self.plugin.launch('snapshot'))
            deadline = time.monotonic() + 5
            while self.plugin.handle is not None and time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.003)
            self.assertIsNone(self.plugin.handle)
            self.assertEqual(self.plugin.can_close(), (True, ''))
            self.assertTrue(self.plugin.loaded)
        finally:
            manager.cancel_all()
            deadline = time.monotonic() + 5
            while manager.is_busy() and time.monotonic() < deadline:
                self.app.processEvents(); time.sleep(.003)
            manager.shutdown(); manager.deleteLater(); self.app.processEvents()

    def test_launch_rejects_raw_qthread_handle(self):
        from qt_compat import QtCore
        self.context.submit_background_task.return_value = QtCore.QThread()
        with self.assertRaises(TypeError): self.plugin.launch('snapshot')

    def test_active_short_worker_is_cancelled_cooperatively_on_exit(self):
        handle = Mock(); handle.isSettled.return_value = False; self.plugin.handle = handle
        self.assertFalse(self.plugin.can_close()[0]); handle.requestInterruption.assert_called_once()
        self.plugin.handle = None


if __name__ == '__main__': unittest.main()
