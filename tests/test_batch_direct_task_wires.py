"""Task text travels through visible graph connections, not hidden source reads."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtWidgets
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import ResourceRef
from model.node_workflow.graph import evaluate_graph,new_node,validate
from model.node_workflow.library import WorkflowLibrary
from model.node_workflow.scope import initialize_execution
from app_plugins.builtin.batch_text_video import node_effects as fx
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.batch_text_video.component_nodes import component_graph,SOURCE_KIND
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.layers import DEFAULT_LAYERS,componentize_layers
from app_plugins.builtin.batch_text_video.store import fresh_state


class DirectTaskWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.video=self.root/'synthetic.mp4';self.video.write_bytes(b'identity only')
        self.registry=fx.fallback_registry()
        self.library=WorkflowLibrary(self.root/'workflows')
        self.services=SimpleNamespace(available_nodes=lambda:self.registry,workflow_library=self.library)
        self.state=fresh_state()
        self.job=dict(id='job',path=str(self.video),task_dir=str(self.root),task_id='42',
            title='Ďakujem, Pane',body='Život je krásny.',copy_id='paired',requires_title=True)
        self.state['jobs']=[self.job]
        self.state['copy_pool']=[dict(id='wrong',title='WRONG TITLE',body='WRONG BODY')]

    def layer(self,field):
        value=new_text_component()
        value.update(source='pool',pool_field=field,copy_binding='task',font_weight=700,
            color='#22bbcc',x=33,y=44,width=80,height=55,
            sequence_items=[dict(id='wrong',text='wrong pool snapshot')],sequence_cursor=0)
        return value

    def graph(self,field):
        graph,effect=component_graph(self.layer(field),self.state['settings'],self.registry)
        entry=next(n for n in graph['nodes'] if n['kind']==fx.INPUT_KIND)
        text=next(n for n in graph['nodes'] if n['kind']=='batch_text_video.text_layer')
        return graph,effect,entry,text

    def evaluate(self,graph,effect,actual_job=None):
        graph=self.library.save(graph,'direct task text',self.registry)
        effect=copy.deepcopy(effect);effect['workflow_id']=graph['workflow_id']
        state=copy.deepcopy(self.state)
        state['settings']['layers']=[dict(v,enabled=False) for v in DEFAULT_LAYERS]+[effect]
        with patch('app_plugins.builtin.batch_text_video.workflow_component_source.prepare_source',
                side_effect=AssertionError('task text must not use source preparation')):
            bundle,_,_=fx.prepare_bundle(state,['job'],self.services)
        payload=bundle['jobs']['job'][effect['workflow_id']]
        context=TaskRunContext(threading.Event(),lambda *_:None,lambda *_:None)
        initialize_execution(payload,context);context.prepared=payload['prepared']
        context.workflow_input=dict(type=fx.TYPE_ID,job=copy.deepcopy(actual_job or self.job),
            video=ResourceRef.capture(self.video,'video'),subtitles=None,audio=None,
            voice_path='',duration=1.,width=320,height=568,effect_layer=effect)
        outputs=evaluate_graph(payload['graph'],self.registry,context)
        return outputs['operations'][0],context

    def test_title_body_and_full_wire_directly_without_adapter(self):
        for field,port,expected in (('title','title',self.job['title']),
                ('body','body',self.job['body']),('full','full_text',self.job['title']+'\n'+self.job['body'])):
            with self.subTest(field=field):
                graph,effect,entry,text=self.graph(field)
                self.assertFalse(any(n['kind']==SOURCE_KIND for n in graph['nodes']))
                self.assertEqual(len(graph['nodes']),3)
                self.assertIn([entry['id'],port,text['id'],'content'],graph['connections'])
                self.assertEqual(validate(graph,self.registry)[0],[])
                operation,context=self.evaluate(graph,effect)
                value=json.loads(dict(operation.options)['layer'])
                self.assertEqual(value['text'],expected)
                self.assertEqual((value['x'],value['y'],value['font_weight']),(33,44,700))
                self.assertFalse(getattr(context,'component_source_steps',{}))

    def test_visible_wire_controls_text_even_if_preparation_has_other_task_text(self):
        graph,effect,entry,text=self.graph('body')
        different=dict(self.job,title='runtime title',body='runtime body')
        operation,_=self.evaluate(graph,effect,different)
        self.assertEqual(json.loads(dict(operation.options)['layer'])['text'],'runtime body')
        connection=next(e for e in graph['connections'] if e[2:]==[text['id'],'content'])
        connection[1]='title'
        operation,_=self.evaluate(graph,effect,different)
        self.assertEqual(json.loads(dict(operation.options)['layer'])['text'],'runtime title')

    def test_blank_task_body_does_not_fall_back_to_a_different_pool_item(self):
        graph,effect,_,_=self.graph('body')
        operation,_=self.evaluate(graph,effect,dict(self.job,body=''))
        self.assertEqual(operation.name,'null')

    def test_existing_title_and_body_components_also_use_direct_ports(self):
        for layer in componentize_layers(None,self.state['settings']):
            if layer['kind'] not in {'title','body'}:continue
            graph,_=component_graph(layer,self.state['settings'],self.registry)
            entry=next(n for n in graph['nodes'] if n['kind']==fx.INPUT_KIND)
            text=next(n for n in graph['nodes'] if n['kind']=='batch_text_video.text_layer')
            self.assertIn([entry['id'],layer['kind'],text['id'],'content'],graph['connections'])
            self.assertFalse(any(n['kind']==SOURCE_KIND for n in graph['nodes']))

    def test_independent_pool_custom_pool_sequence_and_caption_keep_necessary_adapter(self):
        layers=[dict(self.layer('body'),copy_binding='rotate'),dict(self.layer('body'),pool_id='custom'),
            dict(self.layer('body'),source='sequence'),new_caption()]
        for layer in layers:
            with self.subTest(source=layer['source'],pool=layer.get('pool_id')):
                graph,_=component_graph(layer,self.state['settings'],self.registry)
                self.assertTrue(any(n['kind']==SOURCE_KIND for n in graph['nodes']))
                self.assertEqual(validate(graph,self.registry)[0],[])

    def test_unused_old_source_is_not_required_after_manual_direct_rewire(self):
        graph,effect,_,_=self.graph('body')
        unused=new_node(self.registry.definitions[SOURCE_KIND])
        unused['params']['source']=dict(self.layer('body'),copy_binding='rotate')
        graph['nodes'].append(unused)
        operation,context=self.evaluate(graph,effect)
        self.assertEqual(json.loads(dict(operation.options)['layer'])['text'],self.job['body'])
        self.assertNotIn(unused['id'],context.prepared)
