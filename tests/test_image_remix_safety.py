import base64
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtWidgets, QtCore
from model.AppDataStorage import data_directory
from app_plugins.builtin.image_remix.client import APIError, BatchClient
from app_plugins.builtin.image_remix.completion import completion_target
from app_plugins.builtin.image_remix.service import Service, run_operation
from app_plugins.builtin.image_remix.instant import InstantService
from app_plugins.builtin.image_remix.store import Store
from app_plugins.builtin.image_remix import safety_repair
from test_image_remix import FakeClient, png, spec


BLOCK = {'code': 'moderation_blocked', 'type': 'image_generation_user_error',
         'message': 'Rejected by the safety system; violence.',
         'moderation_details': {'stage': 'input'}}


def reply(index, prompt=None):
    return {'id': 'resp_safe', 'status': 'completed', 'usage': {'total_tokens': 100},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({
                'variants': [{'index': index + 1, 'prompt': prompt if prompt is not None else
                    f'安全主题{index + 1}：衣着完整的人物在宁静花园中祈祷，画面表达关怀与希望，无血腥的温和场景。'}]}, ensure_ascii=False)}]}]}


class SafetyClient(FakeClient):
    def __init__(self):
        super().__init__(); self.clouds = {}; self.rewrites = []; self.calls = []
        self.rewrite_error = self.image_error = None; self.text_reply = None
        self.before_rewrite = lambda: None

    def create(self, file_id, metadata):
        self.creates.append((file_id, deepcopy(metadata)))
        if self.create_error: raise self.create_error
        kind = 'text' if metadata['endpoint'] == '/v1/responses' else 'image'
        value = {'id': f'batch_{kind}_{len(self.creates)}', 'status': 'in_progress',
                 'metadata': {k: v for k, v in metadata.items() if k != 'endpoint'}}
        self.clouds[value['id']] = value
        return deepcopy(value)

    def retrieve(self, identifier): return deepcopy(self.clouds[identifier])

    def rewrite(self, body):
        self.rewrites.append(deepcopy(body)); self.before_rewrite()
        if self.rewrite_error: raise self.rewrite_error
        text = body['input'][1]['content'][0]['text']
        index = json.loads(text)['requested_variants'][0]['index'] - 1
        return deepcopy(self.text_reply) if self.text_reply else reply(index)

    def generate(self, body, *, references=False):
        self.calls.append(deepcopy(body))
        if self.image_error: raise self.image_error
        return {'data': [{'b64_json': base64.b64encode(png()).decode()}]}


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root / 'private')
        self.client = SafetyClient(); self.shared = Service(self.store, self.client)

    def create(self, **values):
        return self.store.create(spec(self.root, auto_safety_repair=True, **values), 'original-key')

    def reload(self, job): return self.store.get(job['id'])

    def finish_images(self, job, blocked=(1, 2), *, errors=None):
        attempt = job['attempts'][-1]; cloud = self.client.clouds[attempt['batch_id']]
        cloud.update(status='completed', output_file_id='file-output')
        rows = []
        for item in job['items']:
            if item['id'] not in attempt['item_ids']: continue
            error = (errors or {}).get(item['index'], BLOCK if item['index'] in blocked else None)
            rows.append({'custom_id': attempt['token'] + ':' + item['id'], 'error': None,
                'response': {'status_code': 400 if error else 200, 'body': {'error': error} if error else
                             {'data': [{'b64_json': base64.b64encode(png()).decode()}]}}})
        self.client.results['file-output'] = rows
        return self.shared.advance(self.reload(job))

    def blocked_job(self, **values):
        job = self.create(**values); self.shared.advance(job)
        return self.finish_images(job)

    def start_text(self, job):
        self.shared.advance(job)
        return self.reload(job)

    def finish_text(self, job, *, bad_index=None, unchanged=False):
        batch = job['safety_repair']['planning']['batches'][-1]
        cloud = self.client.clouds[batch['batch_id']]
        cloud.update(status='completed', output_file_id='file-safety')
        rows = []
        for chunk in job['safety_repair']['planning']['chunks']:
            index = chunk['indices'][0]
            body = reply(index, job['items'][index]['safety_repair']['original_prompt'] if unchanged else None)
            if index == bad_index:
                body['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'No safe alternative'}]
            rows.append({'custom_id': batch['token'] + ':' + chunk['id'], 'response': {'status_code': 200, 'body': body}})
        self.client.results['file-safety'] = rows
        return self.shared.advance(self.reload(job))

    def operation(self, job, **fields):
        context = SimpleNamespace(check_canceled=Mock(), report=Mock(), report_item_stage=Mock())
        with patch('app_plugins.builtin.image_remix.service.credential_config', return_value={}), \
             patch('app_plugins.builtin.image_remix.service.read_provider_key', return_value='synthetic'), \
             patch('app_plugins.builtin.image_remix.service.BatchClient', return_value=self.client):
            return run_operation({'root': str(self.store.root), 'id': job['id'], **fields}, context)

    def test_batch_moderation_automatically_enters_persistent_revision(self):
        job = self.blocked_job()
        self.assertEqual(job['status'], 'repairing')
        self.assertEqual(len(job['safety_repair']['item_ids']), 2)
        self.assertEqual(job['items'][0]['status'], 'done')
        self.assertEqual(len(self.client.creates), 1)  # no text/image duplicate POST in receipt processing
        for item in job['items'][1:]:
            self.assertEqual(item['last_failure']['code'], 'moderation_blocked')
            self.assertEqual(len(item['failure_history']), 1)
            self.assertIn('original_prompt', item['safety_repair'])

    def test_batch_repair_uses_batch_discount_endpoint_and_official_error(self):
        job = self.start_text(self.blocked_job())
        self.assertEqual(self.client.creates[-1][1]['endpoint'], '/v1/responses')
        rows = [json.loads(line) for line in self.client.uploads[-1][0].splitlines()]
        self.assertEqual(len(rows), 2)
        body = rows[0]['body']; content = json.loads(body['input'][1]['content'][0]['text'])
        self.assertEqual(content['requested_variants'][0]['moderation_error']['code'], 'moderation_blocked')
        self.assertIn('实质性修改', body['input'][0]['content'])
        self.assertIn('不能仅改同义词', body['input'][0]['content'])
        self.assertIn('安全要求优先', body['input'][0]['content'])
        self.assertFalse(self.client.rewrites)

    def test_accepted_revision_automatically_generates_only_failed_items(self):
        job = self.start_text(self.blocked_job()); old = deepcopy(job['items'][0]); raw = Path(old['path']).read_bytes()
        job = self.finish_text(job)
        self.assertEqual(job['status'], 'running'); self.assertEqual(len(job['attempts']), 2)
        self.assertEqual(job['items'][0], old); self.assertEqual(Path(old['path']).read_bytes(), raw)
        self.assertEqual(len(job['attempts'][-1]['item_ids']), 2)
        for item in job['items'][1:]:
            self.assertEqual(item['safety_repair']['state'], 'done')
            self.assertIn('安全主题', item['prompt']); self.assertTrue(item['failure_history'])
        job = self.finish_images(job, blocked=())
        self.assertEqual(job['status'], 'done')
        self.assertEqual(len(self.client.creates), 3)
        self.shared.advance(job); self.assertEqual(len(self.client.creates), 3)

    def test_repeated_moderation_does_not_rewrite_again_or_enter_complete_all(self):
        job = self.finish_text(self.start_text(self.blocked_job()))
        job = self.finish_images(job)
        self.assertEqual(job['status'], 'partial'); self.assertEqual(len(self.client.creates), 3)
        self.assertIsNone(completion_target(job))
        self.assertEqual(len(job['items'][1]['failure_history']), 2)
        self.shared.advance(job); self.assertEqual(len(self.client.creates), 3)

    def test_one_refused_item_does_not_stop_other_revised_item(self):
        job = self.finish_text(self.start_text(self.blocked_job()), bad_index=1)
        self.assertEqual(job['status'], 'running')
        self.assertEqual(job['attempts'][-1]['item_ids'], [job['items'][2]['id']])
        self.assertEqual(job['items'][1]['safety_repair']['state'], 'failed')
        self.assertEqual(job['items'][1]['status'], 'failed')

    def test_unchanged_prompts_are_not_billed_for_regeneration(self):
        job = self.finish_text(self.start_text(self.blocked_job()), unchanged=True)
        self.assertEqual(len(self.client.creates), 2)
        self.assertEqual(job['status'], 'partial'); self.assertEqual(job['safety_repair']['state'], 'failed')

    def test_reading_results_again_does_not_duplicate_text_or_images(self):
        job = self.start_text(self.blocked_job())
        self.shared.advance(self.reload(job)); self.assertEqual(len(self.client.creates), 2)
        job = self.finish_text(job)
        self.shared.advance(self.reload(job)); self.assertEqual(len(self.client.creates), 3)

    def test_restart_replays_durable_batch_state(self):
        job = self.start_text(self.blocked_job())
        self.store = Store(self.store.root); self.shared = Service(self.store, self.client)
        job = self.finish_text(job)
        self.assertEqual(job['status'], 'running'); self.assertEqual(len(self.client.creates), 3)

    def test_unknown_text_submission_is_recovered_not_resent(self):
        job = self.blocked_job(); self.client.create_error = APIError('timeout')
        self.shared.advance(job); job = self.reload(job)
        self.assertEqual(job['status'], 'repair_unknown'); self.assertEqual(len(self.client.creates), 2)
        self.client.create_error = None; self.shared.advance(self.reload(job))
        self.assertEqual(len(self.client.creates), 2)

    def test_non_moderation_error_not_automatically_rewritten(self):
        job = self.create(); self.shared.advance(job)
        job = self.finish_images(job, blocked=(), errors={1: {'code': 'server_error', 'message': 'temporary'}})
        self.assertEqual(job['status'], 'partial'); self.assertNotIn('safety_repair', job)

    def test_disabled_legacy_job_does_not_silently_acquire_paid_repair(self):
        job = self.store.create(spec(self.root), 'identity'); self.shared.advance(job)
        job = self.finish_images(job)
        self.assertEqual(job['status'], 'partial'); self.assertNotIn('safety_repair', job)

    def test_confirmed_complete_all_repairs_existing_legacy_moderation_first(self):
        job = self.store.create(spec(self.root), 'identity'); self.shared.advance(job)
        job = self.finish_images(job); target = completion_target(job)
        self.operation(job, action='complete', targets=[target], repair_moderation=True)
        job = self.reload(job)
        self.assertEqual(job['status'], 'repairing'); self.assertEqual(len(job['attempts']), 1)
        self.assertEqual(len(self.client.creates), 1)
        self.assertTrue(job['spec']['auto_safety_repair'])

    def test_reference_snapshots_are_read_for_revision_not_original_paths(self):
        path = self.root / 'original.png'; path.write_bytes(png())
        job = self.blocked_job(references=[str(path)]); path.unlink()
        self.start_text(job)
        rows = [json.loads(line) for line in self.client.uploads[-1][0].splitlines()]
        self.assertEqual(rows[0]['body']['input'][1]['content'][1]['type'], 'input_image')

    def test_storage_version_blocks_old_clients(self):
        self.create()
        with self.store.connection() as db: self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 5)

    def test_batch_cancel_does_not_dispatch_repaired_images(self):
        job = self.start_text(self.blocked_job()); job['cancel_requested'] = True; self.store.save(job)
        job = self.finish_text(job)
        self.assertEqual(job['status'], 'cancelled'); self.assertEqual(len(self.client.creates), 2)

    def test_failed_revision_keeps_specific_error_for_inspection(self):
        job = self.finish_text(self.start_text(self.blocked_job()), bad_index=1)
        self.assertIn('拒绝', job['items'][1]['safety_repair']['error'])
        self.assertTrue(job['items'][1]['failure_history'])

    def test_automatic_repair_does_not_retry_unrelated_server_failure(self):
        job = self.create(); self.shared.advance(job)
        job = self.finish_images(job, blocked=(1,), errors={2: {'code': 'server_error', 'message': 'temporary'}})
        self.assertEqual(job['safety_repair']['item_ids'], [job['items'][1]['id']])
        job = self.finish_text(self.start_text(job))
        self.assertEqual(job['attempts'][-1]['item_ids'], [job['items'][1]['id']])
        self.assertNotIn('safety_repair', job['items'][2])

    def test_original_creative_plan_is_preserved(self):
        job = self.blocked_job()
        job['planning'] = {'state': 'done', 'model': 'original-text-model', 'chunks': []}
        job['items'][1]['plan'] = {'prompt': '原始创意方案'}; self.store.save(job)
        old = deepcopy(job['planning']); job = self.finish_text(self.start_text(job))
        self.assertEqual(job['planning'], old); self.assertEqual(job['items'][1]['plan']['prompt'], '原始创意方案')

    def test_corrupt_reference_stops_repair_without_text_or_image_post(self):
        path = self.root / 'ref.png'; path.write_bytes(png())
        job = self.blocked_job(references=[str(path)])
        saved = self.store.root / job['id'] / 'references' / job['references'][0]['file']
        saved.write_bytes(b'corrupt')
        job = self.start_text(job)
        self.assertEqual(len(self.client.creates), 1); self.assertEqual(job['safety_repair']['state'], 'failed')

    def test_instant_refusal_stops_without_reposting_original(self):
        job = self.create(mode='instant', count=1); direct = InstantService(self.store, self.client, self.shared)
        self.client.image_error = APIError('moderation', definite=True, details=BLOCK)
        job = direct.advance(job); self.client.image_error = None
        self.client.text_reply = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal'}]}]}
        job = direct.advance(self.reload(job)); self.assertEqual(job['status'], 'failed')
        self.assertEqual(len(self.client.calls), 1); self.assertEqual(len(self.client.rewrites), 1)

    def test_instant_saved_revision_receipt_replays_without_second_text_charge(self):
        job = self.create(mode='instant', count=1); direct = InstantService(self.store, self.client, self.shared)
        self.client.image_error = APIError('moderation', definite=True, details=BLOCK)
        job = direct.advance(job); self.client.image_error = None
        planner = safety_repair.RepairPlanner(self.store, self.client, job)
        with patch.object(planner.__class__, 'accept', side_effect=OSError('interrupted after response saved')):
            with self.assertRaises(OSError): direct.advance(self.reload(job))
        job = direct.advance(self.reload(job))
        self.assertEqual(job['status'], 'done'); self.assertEqual(len(self.client.rewrites), 1)

    def test_instant_second_block_is_not_rewritten_again_and_remaining_items_continue(self):
        job = self.create(mode='instant'); direct = InstantService(self.store, self.client, self.shared)
        self.client.image_error = APIError('moderation', definite=True, details=BLOCK)
        job = direct.advance(job); job = direct.advance(self.reload(job))
        self.assertEqual(len(self.client.rewrites), 1); self.assertEqual(job['status'], 'ready')
        self.client.image_error = None
        for _ in range(3): job = direct.advance(self.reload(job))
        self.assertEqual(job['status'], 'partial'); self.assertEqual(len(self.client.rewrites), 1)
        self.assertEqual(sum(i['status'] == 'done' for i in job['items']), 2)

    def test_unknown_image_result_never_triggers_prompt_repair(self):
        job = self.create(mode='instant'); direct = InstantService(self.store, self.client, self.shared)
        self.client.image_error = APIError('timeout', definite=False, details=BLOCK)
        job = direct.advance(job)
        self.assertEqual(job['status'], 'unknown'); self.assertNotIn('safety_repair', job)

    def test_api_client_preserves_structured_moderation_error(self):
        import requests
        session = Mock(spec=requests.Session)
        session.headers = {}; response = Mock(status_code=400)
        response.json.return_value = {'error': BLOCK}; session.request.return_value = response
        client = BatchClient('synthetic', session=session)
        with patch('app_plugins.builtin.image_remix.client.require_online'), self.assertRaises(APIError) as error:
            client.generate({})
        self.assertEqual(error.exception.details['code'], 'moderation_blocked')
        self.assertTrue(error.exception.definite)

    def test_instant_one_refusal_does_not_spin_or_block_other_pending_correction(self):
        job = self.create(mode='instant')
        for item in job['items'][1:]:
            item.update(status='failed', error='moderation_blocked')
        safety_repair.begin(self.store, job)
        self.client.text_reply = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal'}]}]}
        self.assertIsNone(safety_repair.advance(self.store, self.client, job))
        self.client.text_reply = None
        ready = safety_repair.advance(self.store, self.client, self.reload(job))
        self.assertEqual(ready, [job['items'][2]['id']]); self.assertEqual(len(self.client.rewrites), 2)

    def test_later_repair_wave_cannot_resubmit_previously_blocked_revised_item(self):
        job = self.finish_text(self.start_text(self.blocked_job()))
        job = self.finish_images(job)
        # A later independent item now fails moderation; older exhausted items
        # must not sneak into that wave's image dispatch list.
        job['items'][0].update(status='failed', error='moderation_blocked')
        safety_repair.begin(self.store, job)
        self.assertEqual(job['safety_repair']['dispatch_ids'], [job['items'][0]['id']])
        self.assertEqual(len(job['safety_repair_history']), 1)

    def test_instant_rewrites_then_resumes_pending_images_without_dead_wait(self):
        job = self.create(mode='instant'); direct = InstantService(self.store, self.client, self.shared)
        self.client.image_error = APIError('moderation', definite=True, status=400, details=BLOCK)
        job = direct.advance(job); self.assertEqual(job['status'], 'repairing')
        self.assertFalse(self.store.stop_requested(job['id']))
        self.client.image_error = None
        for _ in range(6): job = direct.advance(self.reload(job))
        self.assertEqual(job['status'], 'done'); self.assertEqual(len(self.client.rewrites), 1)
        self.assertEqual(len(self.client.calls), 4); self.assertFalse(self.client.creates)

    def test_instant_rewrite_intent_is_durable_before_request(self):
        job = self.create(mode='instant'); direct = InstantService(self.store, self.client, self.shared)
        self.client.image_error = APIError('moderation', definite=True, details=BLOCK)
        job = direct.advance(job)
        def before():
            saved = self.reload(job)
            self.assertEqual(saved['safety_repair']['planning']['chunks'][0]['state'], 'submitting')
        self.client.before_rewrite = before; self.client.image_error = None
        direct.advance(self.reload(job)); self.assertEqual(len(self.client.rewrites), 1)

    def test_instant_unknown_text_reply_is_not_resent(self):
        job = self.create(mode='instant'); direct = InstantService(self.store, self.client, self.shared)
        self.client.image_error = APIError('moderation', definite=True, details=BLOCK)
        job = direct.advance(job); self.client.rewrite_error = APIError('timeout')
        direct.advance(self.reload(job)); direct.advance(self.reload(job))
        self.assertEqual(len(self.client.rewrites), 1); self.assertEqual(len(self.client.calls), 1)

    def test_instant_stop_prevents_new_repair_request(self):
        job = self.create(mode='instant'); direct = InstantService(self.store, self.client, self.shared)
        self.client.image_error = APIError('moderation', definite=True, details=BLOCK)
        job = direct.advance(job); self.store.request_stop(job['id'])
        job = direct.advance(self.reload(job))
        self.assertEqual(job['status'], 'paused'); self.assertFalse(self.client.rewrites)

    def test_instant_worker_continues_repair_then_images(self):
        job = self.create(mode='instant')
        self.client.image_error = APIError('moderation', definite=True, details=BLOCK)
        result = self.operation(job); self.assertTrue(result['auto_continue'])
        self.client.image_error = None
        for _ in range(5): result = self.operation(job, instant_only=True)
        self.assertEqual(self.reload(job)['status'], 'done'); self.assertFalse(result['auto_continue'])


class SafetyUITests(unittest.TestCase):
    def setUp(self):
        from app_plugins.builtin.image_remix.plugin import ImageRemixPlugin
        from app_plugins.builtin.image_remix.ui import RemixDialog
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.parent = QtWidgets.QWidget(); self.plugin = ImageRemixPlugin()
        self.plugin.context = SimpleNamespace(storage=data_directory(Path(self.temp.name) / 'private'))
        self.dialog = RemixDialog(self.plugin, self.parent)
        self.addCleanup(self.cleanup_ui)

    def cleanup_ui(self):
        self.parent.deleteLater(); self.plugin.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()

    def test_checkbox_enabled_by_default_and_can_disable(self):
        self.assertTrue(self.dialog.spec()['auto_safety_repair'])
        self.dialog.auto_safety_repair.setChecked(False)
        self.assertFalse(self.dialog.spec()['auto_safety_repair'])

    def test_option_roundtrips_in_draft(self):
        self.dialog.load_draft({'prompt': '安全的主题', 'auto_safety_repair': False})
        self.assertFalse(self.dialog.auto_safety_repair.isChecked())


if __name__ == '__main__': unittest.main()
