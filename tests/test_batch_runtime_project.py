import copy
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video.project_runtime import ProjectServices,project_payload,unbind_project_paths
from app_plugins.builtin.batch_text_video.workflow_pools import prepare_pool,pool_node
from app_plugins.builtin.batch_text_video.workflow_component_source import prepare_source,source_node
from app_plugins.builtin.batch_text_video.component_nodes import component_graph
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.store import fresh_state,Store,DEFAULTS
from app_plugins.builtin.batch_text_video.node_effects import fallback_registry


def state(label):
    value=fresh_state()
    value['copy_pool']=[{'id':'row','title':label+' title','body':label+' body'}]
    value['profiles']=[{'id':label,'name':label,'data':None}];value['active_profile']=label
    value['credentials']='must not leak';value['jobs']=[{'id':'private job'}]
    return value


class RuntimeProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        for widget in self.app.topLevelWidgets(): widget.close();widget.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_same_node_uses_each_run_project_not_legacy_paths_or_profile(self):
        params={'store_file':'Q:/never-mounted/old/state.json','profile_id':'old','pool_id':'builtin:copy'}
        for label in ('A','B'):
            with patch.object(Store,'load',side_effect=AssertionError('node cannot open projects')):
                prepared=prepare_pool(None,params,ProjectServices(None,state(label)))
            self.assertEqual(json.loads(prepared['pool']['data_json'])['items'][0]['body'],label+' body')
            self.assertEqual(params['profile_id'],'old')

    def test_context_snapshot_is_private_minimal_and_independent(self):
        original=state('A');services=ProjectServices(None,original);original['copy_pool'][0]['body']='changed'
        payload=services.workflow_runtime_context('batch_text_video')
        self.assertEqual(payload['data']['copy_pool'][0]['body'],'A body')
        self.assertNotIn('credentials',json.dumps(payload));self.assertNotIn('private job',json.dumps(payload))
        payload['data']['copy_pool'].clear()
        self.assertTrue(services.workflow_runtime_context('batch_text_video')['data']['copy_pool'])

    def test_concurrent_projects_do_not_share_global_binding(self):
        params={'pool_id':'builtin:copy'}
        def run(label):
            result=prepare_pool(None,params,ProjectServices(None,state(label)))
            return json.loads(result['pool']['data_json'])['items'][0]['title']
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(run,['A','B'])),['A title','B title'])

    def test_no_runtime_context_never_falls_back_to_a_default_or_old_file(self):
        with patch.object(Store,'load',side_effect=AssertionError('unexpected disk read')):
            with self.assertRaisesRegex(ValueError,'本次运行'):
                prepare_pool(None,{'store_file':'old/state.json','pool_id':'builtin:copy'},None)

    def test_component_source_follows_runtime_pool(self):
        source=new_text_component();source.update(source='pool',pool_field='body',copy_binding='rotate')
        params={'source':source,'store_file':'X:/stale/state.json','profile_id':'old'}
        for label in ('A','B'):
            prepared=prepare_source(None,params,ProjectServices(None,state(label)))
            self.assertEqual(prepared['source']['sequence_items'][0]['text'],label+' body')

    def test_schema_and_conversion_never_store_project_binding(self):
        for definition in (pool_node(),source_node()):
            self.assertNotIn('store_file',[p.key for p in definition.parameters])
            self.assertNotIn('profile_id',[p.key for p in definition.parameters])
        layer=new_text_component();layer.update(source='pool')
        graph,_=component_graph(layer,DEFAULTS,fallback_registry())
        for node in graph['nodes']:
            self.assertNotIn('store_file',node['params']);self.assertNotIn('profile_id',node['params'])

    def test_legacy_effect_cleanup_is_in_memory_only(self):
        original={'nodes':[{'kind':'batch_text_video.component_source','params':{'store_file':'old','profile_id':'A','source':{}}}]}
        result=unbind_project_paths(original)
        self.assertEqual(result['nodes'][0]['params'],{'source':{}})
        self.assertEqual(original['nodes'][0]['params']['store_file'],'old')

    def test_pool_inspector_only_selects_runtime_pool_and_refresh_is_read_only(self):
        from app_plugins.builtin.batch_text_video.workflow_pool_ui import PoolNodeEditor
        parent=QtWidgets.QWidget();parent.workflow_services=ProjectServices(None,state('A'));changes=[]
        editor=PoolNodeEditor({'pool_id':'builtin:copy','store_file':'bad','profile_id':'old'},changes.append,parent)
        self.assertEqual(changes,[]);self.assertEqual(editor.pools.count(),4)
        self.assertFalse(hasattr(editor,'path'));self.assertFalse(hasattr(editor,'profiles'))
        editor.pools.setCurrentIndex(editor.pools.findData('builtin:image'))
        self.assertEqual(changes[-1],{'pool_id':'builtin:image'})
        editor.services=ProjectServices(None,state('B'));editor.refresh()
        self.assertIn('B',editor.origin.text());self.assertEqual(len(changes),1)
        parent.deleteLater()

    def test_store_guard_runs_before_missing_file_fallback(self):
        from model.ProtectedStorage import StorageUnavailable
        managed=SimpleNamespace(exists=lambda:(_ for _ in ()).throw(StorageUnavailable('卷已卸载')))
        with tempfile.TemporaryDirectory() as directory,patch('app_plugins.builtin.batch_text_video.store.data_file',return_value=managed):
            with self.assertRaisesRegex(StorageUnavailable,'卷已卸载'):Store(directory).load()

    def test_plugin_provider_uses_current_owner_state_not_saved_profile(self):
        from app_plugins.builtin.batch_text_video.plugin import BatchTextVideoPlugin
        plugin=BatchTextVideoPlugin();plugin.dialog=SimpleNamespace(state=state('B'))
        with patch.object(Store,'load',side_effect=AssertionError('owner is already open')):
            self.assertEqual(plugin.workflow_runtime_context()['name'],'B')

    def test_one_graph_submission_captures_each_provider_only_once(self):
        from model.node_workflow.tasks import PreparationServices
        calls=[]
        def provider(name):
            calls.append(name);return project_payload(state('A' if len(calls)==1 else 'B'))
        services=SimpleNamespace(workflow_runtime_context=provider)
        first=PreparationServices(services)
        self.assertEqual(first.workflow_runtime_context('batch_text_video')['name'],'A')
        self.assertEqual(first.workflow_runtime_context('batch_text_video')['name'],'A')
        self.assertEqual(len(calls),1)
        self.assertEqual(PreparationServices(services).workflow_runtime_context('batch_text_video')['name'],'B')

    def test_owner_submission_overrides_unrelated_saved_or_host_project(self):
        from model.node_workflow.library import WorkflowLibrary
        from app_plugins.builtin.batch_text_video.node_effects import prepare_bundle
        registry=fallback_registry();layer=new_text_component()
        layer.update(source='pool',pool_field='body',copy_binding='rotate')
        graph,effect=component_graph(layer,DEFAULTS,registry)
        with tempfile.TemporaryDirectory() as directory:
            library=WorkflowLibrary(Path(directory)/'workflows')
            graph=library.save(graph,'shared',registry);effect['workflow_id']=graph['workflow_id']
            owner=state('B');owner['jobs']=[dict(id='job',path='',task_dir=directory,title='t',body='b')]
            owner['settings']['layers']=[effect]
            services=SimpleNamespace(available_nodes=lambda:registry,workflow_library=library,
                workflow_runtime_context=lambda _name:project_payload(state('A')))
            bundle,_,_=prepare_bundle(owner,['job'],services)
            prepared=bundle['jobs']['job'][graph['workflow_id']]['prepared']
            source=next(value['source'] for value in prepared.values() if 'source' in value)
            self.assertEqual(source['sequence_items'][0]['text'],'B body')

    def test_save_as_profile_does_not_require_changing_a_shared_node(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            original=fresh_state()
            original['copy_pool']=[{'id':'row','title':'A title','body':'A body'}]
            original=store.save(original,original['revision'])
            cloned=store.create_profile(original,'另存项目',True)
            cloned['copy_pool'][0]['body']='B body'
            cloned=store.save(cloned,cloned['revision'])
            params={'pool_id':'builtin:copy','store_file':'Q:/old/state.json','profile_id':'default'}
            result=prepare_pool(None,params,ProjectServices(None,cloned))
            self.assertEqual(json.loads(result['pool']['data_json'])['items'][0]['body'],'B body')
            self.assertEqual(original['copy_pool'][0]['body'],'A body')
            self.assertEqual(params['profile_id'],'default')
