import copy
import unittest
from unittest.mock import patch
from tests import test_ai_chat_branches as branches
from tests import test_ai_model_client as base
from app_plugins.builtin.ai_model_client import batch_chat
from app_plugins.builtin.ai_model_client.settings import default_options
from app_plugins.builtin.ai_model_client.store import ChatStore


class FakeBatchClient:
    def __init__(self):
        self.cloud = None
        self.output = []
        self.creates = self.uploads = self.cancels = 0
        self.timeout_create = False

    def __call__(self, *_args, **_kwargs): return self
    def close(self): pass
    def upload(self, raw, *_args):
        self.uploads += 1
        import json
        self.submitted = json.loads(raw)
        return 'file-test'
    def create(self, file_id, metadata):
        self.creates += 1
        self.cloud = dict(id='batch_test', input_file_id=file_id, endpoint='/v1/responses', status='validating', metadata={k:v for k,v in metadata.items() if k != 'endpoint'})
        if self.timeout_create:
            raise batch_chat.APIError('synthetic unknown response')
        return copy.deepcopy(self.cloud)
    def retrieve(self, _identifier): return copy.deepcopy(self.cloud)
    def json(self, *_args, **_kwargs): return {'data':[copy.deepcopy(self.cloud)],'has_more':False}
    def lines(self, _identifier): return iter(copy.deepcopy(self.output))
    def cancel_batch(self, _identifier):
        self.cancels += 1
        self.cloud['status'] = 'cancelled'
        return copy.deepcopy(self.cloud)
    def ready(self, custom_id):
        self.cloud.update(status='completed', output_file_id='file-output')
        body = dict(status='completed', model='gpt-6-luna',
            output=[dict(type='message',content=[dict(type='output_text',text='Batch回答')])],
            usage=dict(input_tokens=10,output_tokens=5))
        self.output = [dict(custom_id=custom_id,response=dict(status_code=200,body=body))]


class BatchTests(unittest.TestCase):
    # Use the branch store fixture but avoid inheriting its test methods below.
    def setUp(self):
        branches.BranchTests.setUp(self)
        self.client = FakeBatchClient()
        self.key_patch = patch.object(batch_chat, 'read_provider_key', return_value='synthetic-batch-key')
        self.key_patch.start()
        prompt = self.store.begin_request(self.identifier, '问题', [], 'openai', 'gpt-6-luna')
        self.payload = dict(conversation=self.identifier,prompt_id=prompt,provider='openai',model='gpt-6-luna',
                            config={},options=default_options())
        self.job = base.Job()

    def tearDown(self):
        self.key_patch.stop()
        branches.BranchTests.tearDown(self)

    def submit(self):
        return batch_chat.submit_batch(self.payload,self.job,store=self.store,client_factory=self.client)['batch']

    def check(self, **options):
        return batch_chat.check_batches(dict(conversation=self.identifier,config={},**options),self.job,store=self.store,client_factory=self.client)

    def test_submit_restart_collect_once_and_usage(self):
        record = self.submit()
        self.assertFalse(self.client.submitted['body']['stream'])
        self.assertNotIn('synthetic-batch-key', record['body'])
        self.store = ChatStore(self.store.root)
        self.assertEqual(self.store.messages(self.identifier)[-1]['status'], 'pending')
        self.client.ready(record['id'])
        self.check(); self.check()
        rows = self.store.messages(self.identifier)
        self.assertEqual([r['text'] for r in rows], ['问题','Batch回答'])
        self.assertEqual(rows[-1]['metadata']['usage'], {'input':10,'output':5})
        self.assertEqual(self.client.creates, 1)

    def test_unknown_submission_recovers_without_another_billable_post(self):
        self.client.timeout_create = True
        record = self.submit()
        self.assertEqual(record['state'], 'unknown')
        self.submit()
        self.assertEqual(self.client.creates, 1)
        self.client.ready(record['id'])
        self.check()
        self.assertEqual(self.store.messages(self.identifier)[-1]['text'], 'Batch回答')
        self.assertEqual(self.client.creates, 1)

    def test_result_identity_and_duplicates_are_rejected_before_insert(self):
        record = self.submit()
        self.client.ready('foreign-request')
        with self.assertRaisesRegex(ValueError, '不属于'):
            self.check()
        self.assertEqual(len(self.store.messages(self.identifier)), 1)
        self.client.ready(record['id'])
        self.client.output *= 2
        with self.assertRaisesRegex(ValueError, '重复'):
            self.check()
        self.assertEqual(len(self.store.messages(self.identifier)), 1)

    def test_switched_key_cannot_recover_or_import_another_account(self):
        self.submit()
        with patch.object(batch_chat,'read_provider_key',return_value='other-key'), self.assertRaisesRegex(ValueError,'选回'):
            self.check()
        self.assertEqual(self.client.creates, 1)

    def test_cancel_is_cloud_operation_and_does_not_delete_history(self):
        record = self.submit()
        self.check(cancel=True)
        self.assertEqual(self.client.cancels, 1)
        self.assertEqual(self.store.get_batch(record['id'])['state'], 'cancelled')
        self.assertEqual(self.store.messages(self.identifier)[0]['text'], '问题')
        self.assertEqual(self.store.messages(self.identifier)[0]['status'], 'failed')

    def test_delete_pending_batch_is_blocked_until_result_is_collected(self):
        record = self.submit()
        with self.assertRaisesRegex(ValueError,'未收取'):
            self.store.delete(self.identifier)
        self.client.ready(record['id']); self.check()
        self.store.delete(self.identifier)
        self.assertEqual(self.store.conversations(), [])

    def test_failed_batch_retry_creates_a_new_branch_and_keeps_receipt(self):
        record = self.submit()
        self.client.cloud['status'] = 'failed'
        self.check()
        prompt = self.store.begin_request(self.identifier,'',[],'openai','gpt-6-luna',retry_id=record['prompt_id'])
        self.assertNotEqual(prompt, record['prompt_id'])
        self.assertEqual(self.store.get_batch(record['id'])['state'], 'failed')
        self.assertEqual(len(self.store.messages(self.identifier,all_branches=True)), 2)

    def test_prepared_batch_claim_is_atomic_and_cannot_be_claimed_twice(self):
        body = batch_chat.build_body(self.payload,self.store)
        record = self.store.prepare_batch(self.identifier,self.payload['prompt_id'],'identity',body)
        self.assertTrue(self.store.claim_batch(record['id']))
        self.assertFalse(self.store.claim_batch(record['id']))

    def test_pending_batch_does_not_allow_a_dependent_followup_without_answer(self):
        self.submit()
        with self.assertRaisesRegex(ValueError,'等待回答'):
            self.store.begin_request(self.identifier,'依赖上一条的继续追问',[],'openai','gpt-6-luna')
        self.assertEqual(len(self.store.messages(self.identifier)), 1)


class BatchUITests(unittest.TestCase):
    setUpClass = base.UITests.__dict__['setUpClass']
    setUp = base.UITests.setUp
    tearDown = base.UITests.tearDown
    wait = base.UITests.wait

    def test_ui_batch_submission_and_collection_use_the_original_request(self):
        from model.AIProviderKeys import KEYS_CONFIG_KEY, protect_key
        client = FakeBatchClient()
        self.context.config[KEYS_CONFIG_KEY] = {'openai': protect_key('synthetic-batch-key')}
        with patch.object(self.context.ai_client,'list_models',return_value=[]), patch.object(batch_chat,'BatchClient',client):
            self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('openai'))
            self.dialog.model.setCurrentText('gpt-6-luna')
            identity = self.dialog.sync_openai_identity(self.dialog.connection_snapshot())
            self.dialog.catalog_attempts.add(identity)
            self.dialog.request_mode.setCurrentIndex(1)
            self.dialog.input.setPlainText('批次问题')
            self.dialog.send()
            self.wait(lambda: self.dialog.handle is None)
            record = self.store.batches(self.dialog.current_id)[0]
            self.assertEqual(record['state'], 'validating')
            self.assertIsNone(self.dialog.stream)
            self.assertEqual(self.store.messages(self.dialog.current_id)[-1]['status'], 'pending')
            client.ready(record['id'])
            self.dialog.check_batch_requests()
            self.wait(lambda: self.dialog.handle is None)
        self.assertEqual([r['text'] for r in self.store.messages(self.dialog.current_id)], ['批次问题','Batch回答'])
        self.assertIn('Batch回答', self.dialog.transcript.toPlainText())
        self.assertEqual(client.creates, 1)
