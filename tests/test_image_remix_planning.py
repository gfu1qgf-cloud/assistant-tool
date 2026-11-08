from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtWidgets, QtCore
from model.AppDataStorage import data_file, data_directory
from app_plugins.builtin.image_remix.planning import Planner, request_body, checked_variants, parse_response, image_prompt
from app_plugins.builtin.image_remix.store import Store, summary
from app_plugins.builtin.image_remix.service import Service, run_operation, batch_lines, new_attempt
from app_plugins.builtin.image_remix.instant import InstantService
from app_plugins.builtin.image_remix.client import APIError, BatchClient
from test_image_remix import spec
from test_image_remix_instant import DirectClient


def plans(indices):
    scenes = ['雪山救助受困旅人', '雨夜港口安慰渔民', '沙漠绿洲分水给路人', '古城庭院照顾病人', '森林小径陪伴孤独老人',
              '冬日村落分享热汤', '湖边码头帮助孩子', '城市屋顶仰望星空', '麦田里帮助收获', '海边岩洞照亮归途']
    variants = []
    for index in indices:
        concept = scenes[index % 10] + f'独立构思{index + 1}'
        variants.append({'index': index + 1, 'concept': concept,
            'changes': [{'dimension': dim, 'description': f'{dim}从原有表达改成第{index + 1}个构思中的具体新内容'}
                        for dim in ('scene', 'action', 'composition', 'lighting')],
            'prompt': f'第{index + 1}个方案：{concept}。主体位于画面右侧，远处的环境与人物形成层次，'
                      f'自然光照亮面孔与手部动作，背景保持清晰的叙事细节。画面主题保持关怀和希望，但采用新的地点与互动方式。'})
    return {'theme': '关怀和希望', 'invariants': ['人物身份'], 'variants': variants}


def response(indices):
    return {'id': 'resp_test', 'status': 'completed', 'usage': {'total_tokens': 900},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(plans(indices), ensure_ascii=False)}]}]}


class PlanningClient(DirectClient):
    def __init__(self):
        super().__init__(); self.rewrites = []; self.rewrite_error = None; self.next_response = None
        self.cloud = {'id': 'batch_plan', 'status': 'in_progress'}

    def rewrite(self, body):
        self.rewrites.append(deepcopy(body))
        if self.rewrite_error: raise self.rewrite_error
        slots = json.loads(body['input'][1]['content'])['requested_variants']
        return deepcopy(self.next_response) if self.next_response else response([s['index'] - 1 for s in slots])

    def create(self, file_id, metadata):
        self.creates.append((file_id, deepcopy(metadata)))
        if self.create_error: raise self.create_error
        return deepcopy(self.cloud) if metadata['endpoint'] == '/v1/responses' else {'id': 'batch_image', 'status': 'in_progress'}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root / 'private'); self.client = PlanningClient(); self.planner = Planner(self.store, self.client)
        self.job = self.store.create(spec(self.root, mode='instant', innovation=100, rewrite_enabled=True,
            rewrite_model='gpt-5-mini'), 'identity', defer_snapshot=True)

    def tearDown(self): self.temp.cleanup()
    def reload(self): self.job = self.store.get(self.job['id']); return self.job

    def operation(self, action='check', **kwargs):
        context = SimpleNamespace(check_canceled=Mock(), report=Mock())
        with patch('app_plugins.builtin.image_remix.service.credential_config', return_value={}), patch(
            'app_plugins.builtin.image_remix.service.read_provider_key', return_value='SYNTHETIC'), patch(
            'app_plugins.builtin.image_remix.service.BatchClient', return_value=self.client):
            return run_operation({'root': str(self.root / 'private'), 'id': self.job['id'], 'action': action, **kwargs}, context)

    def batch_job(self, count=3):
        self.job = self.store.create(spec(self.root, count=count, innovation=100, rewrite_enabled=True,
            rewrite_model='gpt-5-mini', mode='batch'), 'identity')
        return self.job

    def complete_text_batch(self):
        batch = self.job['planning']['batches'][-1]
        self.client.cloud.update(status='completed', output_file_id='file-plans')
        self.client.results['file-plans'] = [
            {'custom_id': batch['token'] + ':' + c['id'], 'response': {'status_code': 200, 'body': response(c['indices'])}}
            for c in self.job['planning']['chunks'] if c['id'] in batch['chunks']]

    def test_new_job_starts_in_planning_without_legacy_image_prompts(self):
        self.assertEqual(self.job['status'], 'planning')
        self.assertTrue(all(not i['prompt'] for i in self.job['items']))
        self.assertFalse(self.job['attempts'])

    def test_new_schema_rejects_older_clients_and_never_downgrades_for_legacy_job(self):
        old = self.store.create(spec(self.root, mode='instant'), 'identity')
        self.store.save(old)
        with Store(self.root / 'private').connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_prompt_request_uses_structured_output_and_no_input_images(self):
        self.store.prepare_snapshot(self.job)
        body = request_body(self.job, self.job['planning']['chunks'][0])
        self.assertEqual(body['model'], 'gpt-5-mini'); self.assertFalse(body['store'])
        self.assertEqual(body['text']['format']['type'], 'json_schema')
        self.assertTrue(body['text']['format']['strict'])
        self.assertIn('100', body['input'][1]['content']); self.assertNotIn('input_image', json.dumps(body))

    def test_direct_first_stage_never_sends_an_image_request(self):
        result = self.operation()
        self.assertEqual(len(self.client.rewrites), 1); self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.creates, []); self.assertTrue(result['auto_continue'])
        self.assertEqual(self.reload()['planning']['state'], 'done')

    def test_second_stage_uses_new_prompt_without_appending_original_or_fixed_angle(self):
        self.operation(); self.operation()
        self.assertEqual(len(self.client.rewrites), 1); self.assertEqual(len(self.client.calls), 1)
        prompt = self.client.calls[0][0]['prompt']
        self.assertIn('雪山', prompt); self.assertNotIn(self.job['spec']['prompt'], prompt)
        self.assertNotIn('若与原提示词', prompt); self.assertNotIn('本版本优先尝试', prompt)

    def test_plans_and_usage_are_persistent_before_images(self):
        self.operation(); self.reload()
        self.assertTrue(self.job['items'][0]['plan']['changes'])
        self.assertEqual(self.job['planning']['chunks'][0]['usage']['total_tokens'], 900)
        saved = data_file(self.root / 'private' / self.job['id'] / 'creative_plans.json').read_json()
        self.assertEqual(saved['model'], 'gpt-5-mini'); self.assertEqual(len(saved['variants']), 3)

    def test_restart_does_not_rewrite_accepted_plans(self):
        self.operation(); self.operation(); self.operation()
        self.assertEqual(len(self.client.rewrites), 1); self.assertEqual(len(self.client.calls), 2)

    def test_image_retry_does_not_rewrite_text_again(self):
        self.operation(); self.client.error = APIError('temporary image rejection', definite=True)
        self.operation(); self.client.error = None; self.operation('retry')
        self.assertEqual(len(self.client.rewrites), 1); self.assertEqual(len(self.client.calls), 2)

    def test_retry_preserves_accepted_text_groups(self):
        self.job = self.store.create(spec(self.root, mode='instant', count=11, innovation=20,
            rewrite_enabled=True), 'identity')
        self.operation()
        self.client.rewrite_error = APIError('403', definite=True); self.operation()
        self.client.rewrite_error = None; self.operation('retry_planning')
        self.assertEqual(len(self.client.rewrites), 3)
        requested = json.loads(self.client.rewrites[-1]['input'][1]['content'])['requested_variants']
        self.assertEqual([v['index'] for v in requested], [11])
        self.assertEqual(self.reload()['planning']['state'], 'done')

    def test_reference_snapshot_does_not_overwrite_planned_prompt(self):
        self.operation(); job = self.reload(); prompt = job['items'][0]['prompt']
        job['snapshot_pending'] = True; self.store.save(job); self.store.prepare_snapshot(job)
        self.assertEqual(job['items'][0]['prompt'], prompt)

    def test_high_innovation_with_only_camera_change_is_rejected(self):
        value = plans([0, 1, 2])
        for row in value['variants']: row['changes'] = [{'dimension': 'camera', 'description': '换一个角度'}]
        with self.assertRaises(ValueError): checked_variants(self.job, self.job['planning']['chunks'][0], value)

    def test_100_requires_scene_action_composition_and_fourth_dimension(self):
        value = plans([0, 1, 2]); value['variants'][0]['changes'].pop()
        with self.assertRaises(ValueError): checked_variants(self.job, self.job['planning']['chunks'][0], value)

    def test_duplicate_or_missing_indices_rejected(self):
        for change in ('duplicate', 'missing'):
            value = plans([0, 1, 2])
            if change == 'duplicate': value['variants'][1]['index'] = 1
            else: value['variants'].pop()
            with self.assertRaises(ValueError): checked_variants(self.job, self.job['planning']['chunks'][0], value)

    def test_duplicate_creative_prompts_rejected(self):
        value = plans([0, 1, 2]); value['variants'][1]['prompt'] = value['variants'][0]['prompt']
        with self.assertRaises(ValueError): checked_variants(self.job, self.job['planning']['chunks'][0], value)

    def test_incomplete_or_refused_response_blocks_image_stage(self):
        for body in ({'status': 'incomplete'}, {'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'cannot'}]}]}):
            with self.assertRaises(ValueError): parse_response(body)

    def test_text_timeout_is_not_silently_retried_or_replaced_by_direct_images(self):
        self.client.rewrite_error = APIError('timeout')
        self.operation(); self.operation()
        self.assertEqual(len(self.client.rewrites), 1); self.assertFalse(self.client.calls)
        self.assertEqual(self.reload()['status'], 'planning_unknown')

    def test_definite_text_permission_error_has_no_fallback_or_images(self):
        self.client.rewrite_error = APIError('403', definite=True)
        self.operation(); self.operation()
        self.assertEqual(len(self.client.rewrites), 1); self.assertFalse(self.client.calls)
        self.assertEqual(self.reload()['status'], 'planning_failed')

    def test_explicit_retry_rewrites_failed_text_without_generating_yet(self):
        self.client.rewrite_error = APIError('timeout'); self.operation()
        self.client.rewrite_error = None; self.operation('retry_planning')
        self.assertEqual(len(self.client.rewrites), 2); self.assertFalse(self.client.calls)

    def test_saved_text_response_recovers_without_new_text_or_image_post(self):
        self.store.prepare_snapshot(self.job)
        chunk = self.job['planning']['chunks'][0]; chunk.update(state='submitting', token='saved')
        self.planner.receipt(self.job, 'saved', chunk).write_json(response(chunk['indices']))
        self.store.save(self.job); self.operation()
        self.assertFalse(self.client.rewrites)
        # Already saved text can proceed to image; never another text request.
        self.assertEqual(self.reload()['planning']['state'], 'done')

    def test_direct_image_services_cannot_bypass_incomplete_planning(self):
        shared = Service(self.store, self.client)
        with self.assertRaises(ValueError): InstantService(self.store, self.client, shared).advance(self.job)
        new_attempt(self.job)
        with self.assertRaises(ValueError): batch_lines(self.job, self.job['attempts'][-1])
        self.assertFalse(self.client.calls)

    def test_large_job_is_split_into_bounded_text_groups(self):
        job = self.store.create(spec(self.root, count=23, rewrite_enabled=True), 'identity')
        self.assertEqual([len(c['indices']) for c in job['planning']['chunks']], [10, 10, 3])

    def test_batch_first_endpoint_is_responses_not_images(self):
        self.batch_job(); self.operation(); self.reload()
        self.assertEqual(self.client.creates[0][1]['endpoint'], '/v1/responses')
        raw = self.client.uploads[0][0]
        self.assertEqual(json.loads(raw.splitlines()[0])['url'], '/v1/responses')
        self.assertFalse(self.client.calls)

    def test_batch_images_submit_only_after_text_batch_completed(self):
        self.batch_job(); self.operation(); self.reload(); self.complete_text_batch(); self.operation()
        self.assertEqual([meta['endpoint'] for _, meta in self.client.creates], ['/v1/responses', '/v1/images/generations'])
        self.assertEqual(self.reload()['planning']['state'], 'done')

    def test_batch_text_response_loss_recovers_original_batch(self):
        self.batch_job(); self.client.create_error = APIError('timeout'); self.operation(); self.reload()
        self.client.create_error = None; self.client.recovered = deepcopy(self.client.cloud)
        self.operation(); self.assertEqual(len(self.client.creates), 1)

    def test_batch_retry_while_original_unknown_is_rejected(self):
        self.batch_job(); self.client.create_error = APIError('timeout'); self.operation(); self.reload()
        with self.assertRaises(ValueError): self.planner.retry(self.job)
        self.assertEqual(len(self.client.creates), 1)

    def test_foreign_batch_text_response_is_rejected(self):
        self.batch_job(); self.operation(); self.reload(); self.complete_text_batch()
        self.client.results['file-plans'][0]['custom_id'] = 'foreign:plan-0000'
        self.operation(); self.assertEqual(len(self.client.creates), 1)
        self.assertFalse(self.reload()['attempts'])

    def test_failed_batch_text_cannot_start_image_batch(self):
        self.batch_job(); self.operation(); self.reload()
        self.client.cloud.update(status='failed'); self.operation()
        self.assertEqual(self.reload()['status'], 'planning_failed'); self.assertEqual(len(self.client.creates), 1)

    def test_cancel_text_batch_never_submits_images(self):
        self.batch_job(); self.operation(); self.client.cloud['status'] = 'cancelled'; self.operation('cancel')
        self.assertEqual(self.reload()['status'], 'cancelled'); self.assertEqual(len(self.client.creates), 1)

    def test_cancel_prepared_text_batch_does_not_post_a_paid_batch(self):
        self.batch_job()
        self.job['planning']['batches'] = [{'token': 'prepared', 'state': 'prepared', 'batch_id': '',
            'input_file': 'file-input', 'chunks': ['plan-0000'], 'created': 0}]
        self.job['cancel_requested'] = True; self.store.save(self.job)
        self.operation()
        self.assertEqual(self.reload()['status'], 'cancelled'); self.assertFalse(self.client.creates)

    def test_stop_before_snapshot_does_not_read_missing_reference(self):
        self.job = self.store.create(spec(self.root, mode='instant', rewrite_enabled=True,
            references=[str(self.root / 'missing.png')]), 'identity', defer_snapshot=True)
        self.store.request_stop(self.job['id']); self.operation()
        self.assertEqual(self.reload()['status'], 'paused'); self.assertFalse(self.client.rewrites)

    def test_stop_then_resume_text_stage_does_not_get_stuck(self):
        self.store.request_stop(self.job['id']); self.operation(); self.assertEqual(self.reload()['status'], 'paused')
        self.operation('resume'); self.assertEqual(self.reload()['planning']['state'], 'done')

    def test_legacy_job_does_not_gain_new_text_fees(self):
        old = self.store.create(spec(self.root, mode='instant'), 'identity'); self.job = old
        self.operation(); self.assertFalse(self.client.rewrites); self.assertEqual(len(self.client.calls), 1)


class PlanningUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_editor_exposes_text_model_and_new_spec_enables_two_stages(self):
        from app_plugins.builtin.image_remix.plugin import ImageRemixPlugin
        from app_plugins.builtin.image_remix.ui import RemixDialog
        with tempfile.TemporaryDirectory() as root:
            parent = QtWidgets.QWidget(); plugin = ImageRemixPlugin()
            plugin.context = SimpleNamespace(storage=data_directory(Path(root) / 'private'))
            dialog = RemixDialog(plugin, parent)
            self.assertTrue(dialog.spec()['rewrite_enabled']); self.assertEqual(dialog.spec()['rewrite_model'], 'gpt-5-mini')
            self.assertTrue(dialog.rewrite_model.isEditable())
            dialog.mode.setCurrentIndex(1); self.assertIn('两轮24小时', dialog.mode_note.text())
            dialog.close(); parent.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_client_text_endpoint_has_no_automatic_fallback(self):
        session = Mock(); session.headers = {}; result = Mock(status_code=200); result.json.return_value = response([0])
        session.request.return_value = result
        client = BatchClient('SYNTHETIC', session=session); client.rewrite({'model': 'gpt-5-mini'})
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(session.request.call_args.args[1], 'https://api.openai.com/v1/responses')


if __name__ == '__main__': unittest.main()
