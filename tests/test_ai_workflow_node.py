"""No real provider calls or user credentials: fake transport, isolated storage."""
import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from model.AIWorkflowService import AIWorkflowService
from model.AIProviderClient import AIProviderError
from model.AppDataStorage import ApplicationStorage
from model.BackgroundTasks import TaskRunContext, BackgroundTaskCanceled
from model.OfflineMode import OfflineModeError
from model.node_workflow.contracts import TaskSnapshot, FileList, DirectoryRef, ResourceRef
from model.node_workflow.graph import NodeRegistry, register_core, empty_graph, new_node, evaluate_graph, validate, GraphError
from model.node_workflow.global_data import WorkflowGlobals
from model.node_workflow.scope import initialize_execution
from model.node_workflow.tasks import prepare_job
from app_plugins.builtin.ai_model_client import nodes


class FakeClient:
    def __init__(self):
        self.calls, self.action = [], None
    def chat(self, *args):
        self.calls.append(args)
        if self.action:
            return self.action(*args)
        return {'text': 'Božia láska · 中文', 'provider': args[0], 'model': args[1],
                'usage': {'input': 12, 'output': 8}, 'elapsed': .01, 'warning': ''}


def job(preview=False):
    context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
    context.workflow_globals = WorkflowGlobals.create('detail', is_preview=preview)
    return context


class AIWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = ApplicationStorage(self.root).for_plugin('ai_model_client').directory('workflow_cache')
        self.client = FakeClient()
        self.service = AIWorkflowService(self.client, self.directory)
        # Reversible test-only cipher. Production always uses Windows DPAPI.
        cipher = patch('model.AIWorkflowService._dpapi', side_effect=lambda value, decrypt=False: bytes(b ^ 173 for b in value))
        cipher.start(); self.addCleanup(cipher.stop)
        self.online = patch('model.AIWorkflowService.require_online')
        self.online.start(); self.addCleanup(self.online.stop)
        self.config = {'gemini_api_keys': ['do-not-copy-gemini'], 'ai_provider_keys':
            {'mistral': 'dpapi:synthetic-secret', 'groq': 'dpapi:other-secret'},
            'ai_model_client': {'cloudflare_account_id': 'a'*32, 'private_setting': 'not-needed'},
            'unrelated': 'private-row-secret'}
        self.registry = NodeRegistry(); register_core(self.registry)
        context = SimpleNamespace(ai_workflows=self.service,
            register_node=lambda definition: self.registry.register('ai_model_client', definition))
        nodes.register_nodes(context)
        self.params = new_node(self.registry.definitions[nodes.KIND])['params']
        self.params.update(provider='mistral', model='synthetic-model')

    def bind(self, provider='mistral'):
        return self.service.bind(provider, self.config)['binding']

    def execute(self, context=None, **kwargs):
        return self.service.execute(self.bind(), 'mistral', 'synthetic-model',
            [{'role': 'user', 'text': 'PRIVATE prompt', 'images': []}], {}, context or job(), **kwargs)

    def build(self, inputs=None, context=None, **params):
        c = context or job(); c.node_prepared = {'binding': self.bind()}
        return nodes.build(self.service, inputs or {}, {**self.params, **params}, c)

    def test_registration_no_http_or_store_and_ports(self):
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.directory.path.exists())
        definition = self.registry.definitions[nodes.KIND]
        self.assertEqual(definition.category, 'AI')
        self.assertEqual({p.key for p in definition.outputs}, {'text','data','metadata','cached','model'})
        self.assertNotIn('key', self.params)

    def test_prepared_binding_no_config_keys_and_not_per_row(self):
        first = self.service.bind('mistral', self.config)
        for _ in range(5000):
            self.assertEqual(self.service.bind('mistral', self.config), first)
        self.assertEqual(set(first), {'binding'})
        row = self.service._binding(first['binding'], 'mistral')
        self.assertEqual(row, {'ai_provider_keys': {'mistral': 'dpapi:synthetic-secret'}})
        gemini = self.service.bind('gemini', self.config)
        self.assertEqual(self.service._binding(gemini['binding'], 'gemini'), {})
        cloud = self.service.bind('cloudflare', self.config)
        self.assertEqual(self.service._binding(cloud['binding'], 'cloudflare'),
                         {'ai_model_client': {'cloudflare_account_id': 'a'*32}})
        self.config['ai_provider_keys']['mistral'] = 'changed'
        self.assertEqual(row['ai_provider_keys']['mistral'], 'dpapi:synthetic-secret')

    def test_binding_expired_wrong_provider_or_missing_rejected(self):
        token = self.bind()
        with self.assertRaisesRegex(ValueError, '失效'):
            self.service._binding(token, 'groq')
        self.service._bindings[token]['expires'] = 0
        with self.assertRaisesRegex(ValueError, '失效'):
            self.service._binding(token, 'mistral')
        with self.assertRaisesRegex(ValueError, '失效'):
            self.service._binding('', 'mistral')

    def test_cache_hit_revision_off_and_persistent_encrypted_bytes(self):
        first, cached = self.execute(); self.assertFalse(cached)
        second, cached = self.execute(); self.assertTrue(cached); self.assertEqual(first, second)
        self.assertEqual(len(self.client.calls), 1)
        disk = self.directory.file('answers.sqlite3').read_bytes()
        self.assertNotIn('Božia'.encode(), disk)
        for text in (b'PRIVATE prompt', b'synthetic-secret', b'private-row-secret'):
            self.assertNotIn(text, disk)
        self.execute(revision=2); self.assertEqual(len(self.client.calls), 2)
        self.execute(cache=False); self.execute(cache=False)
        self.assertEqual(len(self.client.calls), 4)
        service = AIWorkflowService(self.client, self.directory)
        token = service.bind('mistral', self.config)['binding']
        _, hit = service.execute(token, 'mistral', 'synthetic-model',
            [{'role': 'user', 'text': 'PRIVATE prompt', 'images': []}], {}, job())
        self.assertTrue(hit)

    def test_preview_defaults_cache_only_explicit_optin_and_offline(self):
        with self.assertRaisesRegex(ValueError, '预览默认不联网'):
            self.execute(job(True))
        self.assertEqual(len(self.client.calls), 0)
        self.execute(job(True), allow_preview=True)
        _, cached = self.execute(job(True)); self.assertTrue(cached)
        with patch('model.AIWorkflowService.require_online', side_effect=OfflineModeError('离线')):
            self.execute()  # offline reuse is allowed
            with self.assertRaises(OfflineModeError): self.execute(revision=2)
        self.assertEqual(len(self.client.calls), 1)

    def test_failure_and_cancellation_not_cached(self):
        self.client.action = lambda *_: (_ for _ in ()).throw(AIProviderError('missing key', status=403))
        with self.assertRaises(AIProviderError): self.execute(retries=2)
        self.assertEqual(len(self.client.calls), 1)
        self.assertFalse(self.directory.file('answers.sqlite3').exists())
        c = job(); c._canceled.set()
        with self.assertRaises(BackgroundTaskCanceled): self.execute(c)
        self.client.action = None
        _, hit = self.execute(); self.assertFalse(hit)

    def test_bounded_retry_and_no_retry_long_rate_limit_or_timeout(self):
        self.client.action = lambda *_: (_ for _ in ()).throw(AIProviderError('503', status=503))
        with patch.object(self.service, '_wait'), self.assertRaises(AIProviderError): self.execute(retries=2)
        self.assertEqual(len(self.client.calls), 3)
        self.client.calls.clear()
        self.client.action = lambda *_: (_ for _ in ()).throw(AIProviderError('429', status=429, retry_after=60))
        with self.assertRaises(AIProviderError): self.execute(retries=2)
        self.assertEqual(len(self.client.calls), 1)
        self.client.action = lambda *_: (_ for _ in ()).throw(AIProviderError('timeout'))
        with self.assertRaises(AIProviderError): self.execute(retries=2)
        self.assertEqual(len(self.client.calls), 2)

    def test_request_cap_shared_across_nodes_in_context(self):
        c = job()
        self.execute(c, cache=False, request_limit=1)
        with self.assertRaisesRegex(ValueError, '请求上限'):
            self.execute(c, cache=False, request_limit=100)
        self.assertEqual(len(self.client.calls), 1)

    def test_reused_worker_context_resets_budget_per_execution_not_inner_loop(self):
        c = job(); payload = {'graph':empty_graph(),'task':None}
        for _ in range(3):
            initialize_execution(payload,c)
            self.execute(c,cache=False,request_limit=1)
            with self.assertRaisesRegex(ValueError,'请求上限'):
                self.execute(c,cache=False,request_limit=1)
        self.assertEqual(len(self.client.calls),3)

    def test_same_input_single_flight_and_two_per_provider(self):
        entered, release = threading.Event(), threading.Event()
        active = [0,0]; lock = threading.Lock(); errors = []
        def action(*args):
            with lock:
                active[0] += 1; active[1] = max(active)
            entered.set(); release.wait(3)
            with lock: active[0] -= 1
            return {'text': args[2][0]['text']}
        self.client.action = action
        def run(revision):
            try: self.execute(revision=revision)
            except Exception as e: errors.append(e)
        threads = [threading.Thread(target=run, args=(1,)) for _ in range(3)]
        for thread in threads: thread.start()
        self.assertTrue(entered.wait(2)); release.set()
        for thread in threads: thread.join(4); self.assertFalse(thread.is_alive())
        self.assertFalse(errors); self.assertEqual(len(self.client.calls), 1)
        release.clear(); entered.clear(); self.client.calls.clear(); active[:] = [0,0]
        threads = [threading.Thread(target=run, args=(i,)) for i in range(2,6)]
        for thread in threads: thread.start()
        deadline = time.monotonic()+2
        while active[1] < 2 and time.monotonic() < deadline: time.sleep(.01)
        self.assertEqual(active[1], 2); self.assertLessEqual(len(self.client.calls), 2)
        release.set()
        for thread in threads: thread.join(4); self.assertFalse(thread.is_alive())
        self.assertFalse(errors); self.assertEqual(active[1], 2)

    def test_cancel_while_waiting_does_not_interrupt_other_task(self):
        entered, release = threading.Event(), threading.Event()
        self.client.action = lambda *_: (entered.set(), release.wait(3), {'text':'ok'})[-1]
        errors = []
        def first():
            try: self.execute()
            except Exception as e: errors.append(e)
        thread = threading.Thread(target=first); thread.start(); self.assertTrue(entered.wait(2))
        c = job(); c._canceled.set()
        with self.assertRaises(BackgroundTaskCanceled): self.execute(c)
        release.set(); thread.join(4)
        self.assertFalse(errors); self.assertEqual(len(self.client.calls), 1)

    def test_node_only_explicit_inputs_without_task_or_history(self):
        c = job(); c.task = SimpleNamespace(fields={'private':'must-not-upload'})
        output = self.build({'text': 'Slovenský text'}, c, prompt='翻译', system='只返回翻译')
        args = self.client.calls[0]
        self.assertEqual(args[2], [{'role':'user','text':'翻译\n\nSlovenský text','images':[]}])
        self.assertEqual(args[3]['system'], '只返回翻译')
        self.assertNotIn('must-not-upload', repr(args[:-1]))
        self.assertEqual(output['data'].value, output['text'])
        self.assertEqual(output['metadata']['usage'], {'input':12,'output':8})
        self.assertFalse(output['cached'])

    def test_inputs_override_params_and_json_strict(self):
        self.client.action = lambda *_: {'text':'```json\n{"číslo":2,"ok":true}\n```'}
        out = self.build({'prompt':'明确输入','system':'明确系统'}, output_format='json')
        self.assertEqual(out['data'].value, {'číslo':2,'ok':True})
        self.assertEqual(self.client.calls[0][2][0]['text'], '明确输入')
        self.assertTrue(self.client.calls[0][3]['system'].startswith('明确系统'))
        for invalid in ('{"x":1,"x":2}', '{"x":NaN}', '解释：{"x":1}', 'null\n其他'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '严格有效'):
                nodes.response_data(invalid, 'json')
        self.assertIsNone(nodes.response_data('null','json').value)

    def test_temperature_default_omitted_and_custom_explicit(self):
        self.build()
        self.assertIsNone(self.client.calls[-1][3]['temperature'])
        self.build(temperature_mode='custom',temperature=.7)
        self.assertEqual(self.client.calls[-1][3]['temperature'],.7)

    def test_invalid_json_is_not_success_cache(self):
        self.client.action = lambda *_: {'text':'not-json'}
        for _ in range(2):
            with self.assertRaisesRegex(ValueError,'严格有效'):
                self.build(output_format='json')
        self.assertEqual(len(self.client.calls),2)
        self.assertFalse(self.directory.file('answers.sqlite3').exists())

    def test_image_normalized_only_explicit_local_file_changes_cache(self):
        path = self.root/'image.png'; Image.new('RGB',(2500,30),'red').save(path)
        original = path.read_bytes()
        self.build({'image_path':str(path)})
        image = self.client.calls[0][2][0]['images'][0]
        import base64
        with Image.open(io.BytesIO(base64.b64decode(image['data']))) as converted:
            self.assertLessEqual(converted.width,2048)
        self.assertEqual(path.read_bytes(), original)
        self.build({'image_path':str(path)}); self.assertEqual(len(self.client.calls),1)
        Image.new('RGB',(2500,30),'blue').save(path)
        self.build({'image_path':str(path)}); self.assertEqual(len(self.client.calls),2)

    def test_image_urls_invalid_media_and_oversized_list_rejected(self):
        with self.assertRaisesRegex(ValueError,'本地文件'): self.build({'image_path':'https://example.com/x.jpg'})
        path = self.root/'bad.mp4'; path.write_bytes(b'not image')
        with self.assertRaises(Exception): self.build({'image_path':str(path)})
        ref = ResourceRef.capture(path,'image')
        files = FileList(DirectoryRef(str(self.root)), (ref,)*4)
        with self.assertRaisesRegex(ValueError,'最多3张'): self.build({'images':files})
        self.assertEqual(self.client.calls,[])

    def test_empty_oversize_and_invalid_params_fail_before_http(self):
        with self.assertRaisesRegex(ValueError,'没有提示词'): self.build(prompt='')
        with self.assertRaisesRegex(ValueError,'输入过长'): self.build({'text':'a'*64001})
        with self.assertRaises(ValueError): self.build(model='')
        with self.assertRaises(ValueError): self.build(max_tokens=1)
        with self.assertRaises(ValueError): self.build(temperature=float('nan'))
        self.assertEqual(self.client.calls,[])

    def test_corrupt_cache_never_silently_rebills(self):
        self.execute()
        db = self.directory.file('answers.sqlite3').connect()
        with db: db.execute('UPDATE answers SET payload=?', (b'corrupted',))
        db.close()
        with self.assertRaisesRegex(ValueError,'缓存无法解密'): self.execute()
        self.assertEqual(len(self.client.calls),1)

    def test_missing_crypto_fails_before_http(self):
        with patch('model.AIWorkflowService._dpapi', side_effect=RuntimeError('crypto unavailable')):
            with self.assertRaises(RuntimeError): self.execute()
        self.assertEqual(self.client.calls,[])

    def test_storage_unavailable_blocks_cache_hit_and_no_plain_fallback(self):
        self.execute()
        with patch.object(self.directory.storage,'check',side_effect=OSError('VC unavailable')):
            with self.assertRaisesRegex(OSError,'VC unavailable'): self.execute()
        self.assertEqual(len(self.client.calls),1)

    def graph(self, line=False):
        node = new_node(self.registry.definitions[nodes.KIND]); node['params'].update(self.params)
        output = new_node(self.registry.definitions['core.py_output'])
        graph = empty_graph(); graph.update(nodes=[node,output],output_node=output['id'],
            execution_mode='line' if line else 'detail',
            connections=[[node['id'],'data',output['id'],'value']])
        return graph

    def test_detail_line_prepare_payload_no_secrets_and_worker_execution(self):
        for line in (False, True):
            graph = self.graph(line)
            task = TaskSnapshot('k','label',str(self.root),'',None,json.dumps({'secret':'private-row-secret'}),'sk') if line else None
            services = SimpleNamespace(prepare_ai_workflow=lambda p: self.service.bind(p,self.config))
            root = graph['nodes'][0]['id'] if line else None
            payload, resources = prepare_job(graph,self.registry,task,services,root_id=root)
            self.assertFalse(resources)
            for text in ('synthetic-secret','other-secret','do-not-copy-gemini'):
                self.assertNotIn(text,json.dumps(payload))
            self.assertFalse(validate(graph,self.registry,root_id=root)[0])
            c = job(); initialize_execution(payload,c); c.prepared=payload['prepared']
            result = evaluate_graph(graph,self.registry,c,root_id=root)
            self.assertEqual(result['data' if line else 'value'].value,'Božia láska · 中文')

    def test_graph_preview_blocks_and_json_types_work_with_core(self):
        graph = self.graph()
        services = SimpleNamespace(prepare_ai_workflow=lambda p: self.service.bind(p,self.config))
        payload,_ = prepare_job(graph,self.registry,None,services)
        payload['preview_node'] = graph['output_node']
        c = job(); initialize_execution(payload,c); c.prepared=payload['prepared']
        with self.assertRaisesRegex(GraphError,'预览默认不联网'):
            evaluate_graph(graph,self.registry,c)
        self.assertEqual(self.client.calls,[])

    def test_host_binding_must_be_prepared_on_main_thread_not_worker(self):
        from qt_compat import QtWidgets
        from tests.test_plugin_host import FakeMainWindow
        from app_plugins.host import PluginHost, PluginContext
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = FakeMainWindow(); window.config = self.config
        host = PluginHost(window); host._ai_workflows = self.service
        record = host.prepare_ai_workflow('mistral')
        self.assertEqual(set(record),{'binding'})
        errors = []
        def prepare_in_worker():
            try: host.prepare_ai_workflow('mistral')
            except Exception as error: errors.append(str(error))
        thread = threading.Thread(target=prepare_in_worker); thread.start(); thread.join(3)
        self.assertEqual(len(errors),1); self.assertIn('主线程',errors[0])
        self.assertNotIn('secret',json.dumps(record))
        window.deleteLater(); app.processEvents()

    def test_shared_http_client_and_status_metadata_without_real_network(self):
        from model.AIProviderClient import AIProviderClient
        from tests.test_ai_model_client import Response
        self.service.client = AIProviderClient()
        response = Response({'choices':[{'message':{'content':'Ahoj'},'finish_reason':'stop'}],
                             'usage':{'prompt_tokens':3,'completion_tokens':2}})
        with patch.object(self.service.client,'_key',return_value='synthetic-http-key'), \
                patch.object(self.service.client,'list_models',return_value=[{'id':'synthetic-model','vision':True}]), \
                patch('model.AIProviderClient.require_online'), \
                patch('model.AIProviderClient.requests.request',return_value=response) as request:
            output = self.build({'text':'Ahoj'})
        self.assertEqual(output['text'],'Ahoj')
        self.assertEqual(output['metadata']['usage'],{'input':3,'output':2})
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'],'Bearer synthetic-http-key')
        self.assertNotIn('synthetic-http-key',json.dumps(output['metadata']))

    @unittest.skipUnless(os.name=='nt','Windows DPAPI only')
    def test_production_dpapi_roundtrip(self):
        from model.AIProviderKeys import _dpapi
        raw = '仅合成数据 · no real key'.encode()
        encrypted = _dpapi(raw)
        self.assertNotIn(raw,encrypted)
        self.assertEqual(_dpapi(encrypted,decrypt=True),raw)


if __name__ == '__main__':
    unittest.main()
