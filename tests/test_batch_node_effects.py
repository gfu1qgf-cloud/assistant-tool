"""Actual queue injection, named-library UI, shared composition and safe publishing."""
import base64
import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets, QtGui
from PIL import Image, ImageChops, ImageStat
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import FilterOperation, ResourceRef, NodeDefinition, NodePort
from model.node_workflow.graph import empty_graph, new_node, validate, evaluate_graph, GraphError
from model.node_workflow.global_data import WorkflowGlobals
from model.node_workflow.library import WorkflowLibrary
from model.node_workflow.tasks import prepare_job
from model.node_workflow.scope import initialize_execution
from model.node_workflow.preview import run_preview
from app_plugins.builtin.batch_text_video import node_effects as fx
from app_plugins.builtin.batch_text_video.node_effect_ui import NodeEffectProperties
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.playback import preview_key
from app_plugins.builtin.batch_text_video.playback import PlaybackWorker
from app_plugins.builtin.batch_text_video.engine import render_static_text, _run, probe, Cancelled
from app_plugins.builtin.batch_text_video.workflow_layers import layer_node

FFMPEG = os.environ.get('NODE_FFMPEG', '')


def context():
    return TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)


class NodeEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = fx.fallback_registry()
        self.library = WorkflowLibrary(self.root/'workflows')
        self.services = SimpleNamespace(available_nodes=lambda: self.registry, workflow_library=self.library)
        self.video = self.root/'background.mp4'; self.video.write_bytes(b'synthetic')
        self.voice = self.root/'task_audio.wav'; self.voice.write_bytes(b'synthetic audio')
        self.srt = self.root/'task_audio.srt'
        self.srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nBožia láska.\n', encoding='utf8')
        self.job = dict(id='job-a', name='final', path=str(self.video), title='Božia', body='láska.',
            task_dir=str(self.root), task_id='8009', task_type='reels', task_date='1002',
            task_voice_path=str(self.voice), voice_path=str(self.voice), task_subtitle_path=str(self.srt))

    def save(self, graph=None):
        graph = self.library.save(graph or fx.example(self.registry), '字幕配色', self.registry)
        state = fresh_state(); state['jobs'] = [copy.deepcopy(self.job)]
        effect = fx.new_effect(); effect['workflow_id'] = graph['workflow_id']
        state['settings']['layers'] = normalize_layers([dict(v, enabled=False) for v in DEFAULT_LAYERS]+[effect])
        state['settings'].update(music_enabled=False, darkness=0, use_voice=False, ffmpeg_path=FFMPEG,
            aspect='source', auto_crop_black_bars=False)
        return state, effect, graph

    def payload_context(self, state):
        bundle, registry, _ = fx.prepare_bundle(state, ['job-a'], self.services)
        payload = next(iter(bundle['jobs']['job-a'].values()))
        c = context(); initialize_execution(payload, c); c.prepared = payload['prepared']
        c.workflow_input = dict(type=fx.TYPE_ID, job=self.job, video=ResourceRef.capture(self.video,'video'),
            subtitles=ResourceRef.capture(self.srt,'subtitles'), audio=ResourceRef.capture(self.voice,'audio'),
            voice_path=str(self.voice), duration=1., width=320, height=568)
        return bundle, registry, payload, c

    def text_graph(self, texts=('第一层', '第二层')):
        graph = empty_graph(); graph.update(workflow_type=fx.TYPE_ID, workflow_type_version=1, execution_mode='line')
        entry = new_node(self.registry.definitions[fx.INPUT_KIND])
        output = new_node(self.registry.definitions[fx.OUTPUT_KIND], (680,0))
        nodes = [entry, output]; wires = []
        for index, text in enumerate(texts):
            node = new_node(layer_node('text'), (340,index*240))
            node['params']['layer'].update(text=text, source='fixed', color='#ff6699',
                font='Open Sans', font_min=45, font_max=45, outline_width=0, y=30+index*30,
                width=85, height=25, background_enabled=True, background_color='#fff0e0', background_opacity=100)
            nodes.append(node); wires.append([node['id'],'operations',output['id'],'operations'])
        graph.update(nodes=nodes, connections=wires, output_node=output['id'])
        return graph

    def test_type_is_line_owned_and_example_has_no_export_or_video_requirement(self):
        self.assertEqual(self.registry.workflow_types[fx.TYPE_ID].execution_mode,'line')
        graph = fx.example(self.registry)
        self.assertEqual(validate(graph,self.registry)[0], [])
        sink = self.registry.definitions[fx.OUTPUT_KIND]
        self.assertEqual(sink.outputs, ()); self.assertTrue(sink.inputs[0].multiple)
        self.assertFalse(any(n['kind'] in {'core.render','core.export'} for n in graph['nodes']))
        graph['execution_mode'] = 'detail'
        self.assertTrue(validate(graph,self.registry)[0])

    def test_background_can_connect_to_video_preview_with_seek_capability(self):
        graph=fx.example(self.registry)
        entry=next(n for n in graph['nodes'] if n['kind']==fx.INPUT_KIND)
        preview=new_node(self.registry.definitions['core.preview_video'])
        graph['nodes'].append(preview); graph['connections'].append([entry['id'],'video',preview['id'],'value'])
        self.assertEqual(validate(graph,self.registry,root_id=preview['id'])[0],[])

    def test_input_fields_are_actual_and_not_one_task_packet(self):
        state,_,_ = self.save(); _,_,_,c = self.payload_context(state)
        values = fx.build_input({}, {}, c)
        self.assertEqual(set(values), {v[0] for v in fx.INPUT_PORTS})
        self.assertEqual(values['background_path'],str(self.video))
        self.assertEqual(values['subtitles'].path,str(self.srt)); self.assertEqual(values['audio'].path,str(self.voice))
        self.assertEqual(values['title'],'Božia'); self.assertEqual(values['full_text'],'Božia\nláska.')
        self.assertEqual(values['task_name'],'final')
        self.assertEqual((values['width'],values['height'],values['duration']), (320,568,1.))
        self.assertEqual((values['iteration'],values['iteration_count']), (1,1))
        self.assertNotIn('task',values)
        with self.assertRaises(ValueError): fx.build_input({}, {}, context())

    def test_queue_iteration_counts_submitted_subset_not_spreadsheet_row(self):
        state,_,_ = self.save(self.text_graph())
        state['jobs'] = [dict(self.job,id='j'+str(i),task_id=str(5000+i),source_row=200+i) for i in range(5)]
        bundle,_,_ = fx.prepare_bundle(state,['j3'],self.services)
        payload = next(iter(bundle['jobs']['j3'].values()))
        self.assertEqual(payload['global_data']['iteration'],1)
        self.assertEqual(payload['global_data']['iteration_count'],1)
        self.assertEqual(json.loads(payload['task']['fields_json'])['task_id'],'5003')

    def test_saved_graph_is_frozen_and_bundle_is_plain(self):
        state,_,graph = self.save(); bundle,_,payload,_ = self.payload_context(state)
        json.dumps(bundle,allow_nan=False)
        before = copy.deepcopy(bundle)
        graph['nodes'][1]['params']['seed'] = 'changed'; self.library.save(graph,'changed',self.registry)
        state['jobs'][0]['body'] = 'changed'
        self.assertEqual(bundle,before); self.assertEqual(payload['task']['directory'],str(self.root))

    def test_missing_srt_only_blocks_nodes_that_need_it(self):
        state,_,_ = self.save(); _,registry,payload,c = self.payload_context(state)
        c.workflow_input['subtitles'] = None
        with self.assertRaisesRegex(GraphError,'空|缺|字幕'): evaluate_graph(payload['graph'],registry,c)
        state,_,_ = self.save(self.text_graph()); _,registry,payload,c = self.payload_context(state)
        c.workflow_input['subtitles'] = None
        values = evaluate_graph(payload['graph'],registry,c)
        self.assertEqual(len(values['operations']),2)

    def test_other_voice_never_borrows_original_task_srt(self):
        other = self.root/'pool_voice.wav'; other.write_bytes(b'another')
        self.assertIsNone(fx.subtitle_for(self.job,str(other)))
        srt = other.with_suffix('.srt'); srt.write_text(self.srt.read_text('utf8'),'utf8')
        self.assertEqual(fx.subtitle_for(self.job,str(other)),srt)
        self.assertEqual(fx.subtitle_for(self.job,str(self.voice)),self.srt)

    def test_task_identity_reproducible_across_quick_export_uuid_but_separates_backgrounds(self):
        self.assertEqual(fx.task_snapshot(self.job).key,fx.task_snapshot(dict(self.job,id='another-id')).key)
        self.assertNotEqual(fx.task_snapshot(self.job).key,fx.task_snapshot(dict(self.job,path='different.mp4')).key)

    def test_task_name_and_queue_iteration_can_seed_palette_without_changing_on_rerun(self):
        graph=fx.example(self.registry)
        entry=next(n for n in graph['nodes'] if n['kind']==fx.INPUT_KIND)
        color=next(n for n in graph['nodes'] if n['kind']=='core.text_palette')
        color['params']['scope']='fixed'
        graph['connections'] += [[entry['id'],'task_name',color['id'],'seed'],
                                 [entry['id'],'iteration',color['id'],'iteration']]
        state,_,_=self.save(graph); _,registry,payload,c=self.payload_context(state)
        first=evaluate_graph(payload['graph'],registry,c)
        c.workflow_input['job']=dict(self.job,name='preview-cache-hash')
        self.assertEqual(first,evaluate_graph(payload['graph'],registry,c))
        c.workflow_globals=WorkflowGlobals.create('line',3).at_iteration(2)
        self.assertNotEqual(first,evaluate_graph(payload['graph'],registry,c))

    def test_output_flattens_order_and_cannot_accept_functions_or_other_video(self):
        state,_,_ = self.save(); _,_,_,c = self.payload_context(state)
        a,b = FilterOperation('a',None,'null'),FilterOperation('b',None,'null')
        self.assertEqual(fx.build_output({'operations':[(a,),(b,)]},{},c)['operations'],(a,b))
        with self.assertRaises(TypeError): fx.build_output({'operations':[lambda: None]},{},c)
        with self.assertRaisesRegex(ValueError,'128'): fx.build_output({'operations':[(a,)*129]},{},c)
        other = self.root/'other.mp4'; other.write_bytes(b'other')
        with self.assertRaises(ValueError): fx.build_output({'operations':[(replace(a,video=ResourceRef.capture(other,'video')),)]},{},c)

    def test_disabled_effects_need_no_library_and_bad_active_selection_is_clear(self):
        state = fresh_state()
        self.assertEqual(fx.prepare_bundle(state,[],None),(None,None,()))
        effect = fx.new_effect(); state['settings']['layers'].append(effect)
        with self.assertRaisesRegex(ValueError,'尚未选择'): fx.prepare_bundle(state,[],self.services)
        effect['enabled'] = False
        self.assertEqual(fx.prepare_bundle(state,[],None),(None,None,()))

    def test_component_does_not_execute_during_static_poster_and_no_geometry(self):
        state,effect,_ = self.save()
        with patch.object(fx,'build_input',side_effect=AssertionError('no graph in poster')):
            image,_ = render_overlay('','',320,568,state['settings'])
        self.assertFalse(image.isNull()); self.assertNotIn('x',effect)
        from app_plugins.builtin.batch_text_video.captions import static_planes
        self.assertIn('node_effect',[kind for kind,_value in static_planes('','',320,568,state['settings'],None)])

    def test_panel_select_save_remove_and_editor_entry(self):
        state,effect,graph = self.save()
        calls=[]; self.services.open_workflow_editor=lambda **kwargs:calls.append(kwargs)
        panel = LayerPanel(state['settings']['layers'], self.root, services=self.services); self.addCleanup(panel.deleteLater)
        panel.set_layers(state['settings']['layers']); panel.select_layer_id(effect['id'])
        self.assertEqual(panel.node_effect_properties.workflow.currentData(),graph['workflow_id'])
        self.assertTrue(('node' in panel.editor.sections)); self.assertFalse(('position' in panel.editor.sections))
        panel.node_effect_properties.open.click(); self.assertEqual(calls[-1]['workflow_id'],graph['workflow_id'])
        panel.node_effect_properties.new.click(); self.assertEqual(calls[-1]['workflow_type'],fx.TYPE_ID)
        panel.select_layer_id('title'); panel.select_layer_id(effect['id'])
        self.assertEqual(panel.current()['workflow_id'],graph['workflow_id'])
        if os.environ.get('NODE_QA'):
            destination=Path(os.environ['NODE_QA']); destination.mkdir(parents=True,exist_ok=True)
            panel.resize(650,950); panel.show(); self.app.processEvents()
            panel.grab().save(str(destination/'node-effect-component.png'))
        panel.remove_image(); self.assertFalse(any(x['kind']=='node_effect' for x in panel.values()))
        panel.add_node_effect(); self.assertEqual(panel.current()['kind'],'node_effect')

    def test_missing_selection_retained_and_filter_excludes_other_workflow_types(self):
        state,effect,graph = self.save()
        plain = empty_graph(); self.library.save(plain,'other',self.registry)
        widget = NodeEffectProperties(self.services); self.addCleanup(widget.deleteLater)
        changes=[]; widget.changed.connect(changes.append); widget.set_layer(effect)
        self.assertEqual(widget.workflow.count(),2)
        self.library._path(graph['workflow_id']).unlink(); widget.reload()
        self.assertEqual(widget.workflow.currentData(),graph['workflow_id']); self.assertFalse(changes)
        self.assertIn('已保留',widget.status.text())

    def test_workflow_selection_persists_in_independent_profile(self):
        state,effect,_ = self.save(); store=Store(self.root/'store')
        state=store.save(state,0); state=store.create_profile(state,'另一个',False)
        self.assertFalse(fx.active_effects(state['settings']))
        state=store.select_profile(state,'default')
        self.assertEqual(fx.active_effects(state['settings'])[0]['workflow_id'],effect['workflow_id'])

    def test_preview_cache_covers_srt_graph_and_ignores_unused_batch_time(self):
        state,_,_ = self.save(); bundle,_,_,_ = self.payload_context(state)
        key=preview_key(self.job,state,bundle)
        another,_,_=fx.prepare_bundle(state,['job-a'],self.services)
        self.assertEqual(key,preview_key(self.job,state,another))
        self.srt.write_text(self.srt.read_text('utf8')+'\n','utf8')
        self.assertNotEqual(key,preview_key(self.job,state,bundle))
        changed=copy.deepcopy(bundle)
        next(iter(changed['jobs']['job-a'].values()))['graph']['nodes'][1]['params']['seed']='2'
        self.assertNotEqual(preview_key(self.job,state,bundle),preview_key(self.job,state,changed))

    def test_source_verification_and_output_protection(self):
        runtime=fx.EffectRuntime(None,self.registry); runtime.refs=[ResourceRef.capture(self.video,'video')]
        runtime.verify()
        with self.assertRaisesRegex(ValueError,'阻止覆盖'): runtime.protect_output(self.video)
        self.video.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'发生变化'): runtime.verify()


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(),'需要真实FFmpeg')
class RealEffectsTests(NodeEffectsTests):
    def test_ai_effect_real_preview_optin_and_export_reuse_encrypted_cache(self):
        from model.AIWorkflowService import AIWorkflowService
        from model.AppDataStorage import ApplicationStorage
        from app_plugins.builtin.ai_model_client.nodes import register_nodes, KIND
        self.media()
        client = SimpleNamespace(chat=unittest.mock.Mock(return_value={
            'text':'Božia láska', 'provider':'mistral', 'model':'synthetic-model',
            'usage':{'input':2,'output':4},'elapsed':.01,'warning':''}))
        service = AIWorkflowService(client, ApplicationStorage(self.root).for_plugin('ai_model_client').directory('cache'))
        register_nodes(SimpleNamespace(ai_workflows=service,
            register_node=lambda definition:self.registry.register('ai_model_client',definition)))
        self.services.prepare_ai_workflow = lambda provider:service.bind(provider,{})
        graph = self.text_graph(('placeholder',))
        entry = graph['nodes'][0]
        layer = next(n for n in graph['nodes'] if n['kind']=='batch_text_video.text_layer')
        ai = new_node(self.registry.definitions[KIND]); ai['params'].update(provider='mistral',model='synthetic-model')
        graph['nodes'].append(ai)
        graph['connections'].extend([[entry['id'],'title',ai['id'],'text'],[ai['id'],'text',layer['id'],'content']])
        state,_,saved = self.save(graph)
        with patch('model.AIWorkflowService._dpapi',side_effect=lambda raw,decrypt=False:bytes(b^173 for b in raw)), \
                patch('model.AIWorkflowService.require_online'):
            bundle,registry,_ = fx.prepare_bundle(state,['job-a'],self.services)
            with self.assertRaisesRegex(ValueError,'预览默认不联网'):
                render_static_text(self.job,state,self.root,preview=True,node_effects=fx.EffectRuntime(bundle,registry))
            client.chat.assert_not_called()
            ai = next(n for n in saved['nodes'] if n['kind']==KIND)
            ai['params']['preview_network']='yes'
            self.library.save(saved,'字幕配色',self.registry)
            bundle,registry,_ = fx.prepare_bundle(state,['job-a'],self.services)
            output,_ = render_static_text(self.job,state,self.root,preview=True,node_effects=fx.EffectRuntime(bundle,registry))
            self.assertTrue(output.is_file())
            self.assertTrue(client.chat.call_args.args[-1].workflow_globals.is_preview)
            self.assertGreater(max(ImageStat.Stat(self.frame(output)).stddev),5)
            state['settings']['overwrite']=True
            render_static_text(self.job,state,self.root,node_effects=fx.EffectRuntime(bundle,registry))
            self.assertEqual(client.chat.call_count,1)

    def ffmpeg(self,args):
        result=subprocess.run([FFMPEG,'-hide_banner','-nostdin','-loglevel','error',*args],capture_output=True,
            timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace')); return result.stdout

    def media(self):
        self.ffmpeg(['-y','-f','lavfi','-i','color=c=0x345e70:s=320x568:r=10:d=1',
            '-c:v','libx264','-pix_fmt','yuv420p',str(self.video)])
        self.ffmpeg(['-y','-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','pcm_s16le',str(self.voice)])

    def frame(self,path):
        raw=self.ffmpeg(['-ss','0.5','-i',str(path),'-frames:v','1','-vcodec','png','-f','image2pipe','-'])
        return Image.open(io.BytesIO(raw)).convert('RGB')

    def test_real_example_caption_random_palette_no_model_one_final_encoding(self):
        self.media(); state,_,_=self.save(); state['settings']['use_voice']=True
        bundle,registry,_=fx.prepare_bundle(state,['job-a'],self.services)
        actual=[]
        def record_input(inputs, params, c):
            values=fx.build_input(inputs,params,c); actual.append(values); return values
        registry.definitions[fx.INPUT_KIND]=replace(registry.definitions[fx.INPUT_KIND],build=record_input)
        before=(self.video.read_bytes(),self.srt.read_bytes(),self.voice.read_bytes())
        with patch('app_plugins.builtin.batch_text_video.engine._run',wraps=_run) as run, \
             patch('app_plugins.builtin.batch_text_video.captions.load_word_timings',side_effect=AssertionError('no model')):
            output,_=render_static_text(self.job,state,self.root,node_effects=fx.EffectRuntime(bundle,registry))
        self.assertEqual(sum('-c:v' in call.args[0] and 'libx264' in call.args[0] for call in run.call_args_list),1)
        info=probe(output,str(Path(FFMPEG).with_name('ffprobe.exe')))
        self.assertTrue(info['audio']); self.assertAlmostEqual(info['duration'],1.,delta=.15)
        frame=self.frame(output); self.assertGreater(ImageStat.Stat(frame).stddev[0],5)
        self.assertEqual(before,(self.video.read_bytes(),self.srt.read_bytes(),self.voice.read_bytes()))
        self.assertFalse(list(self.root.glob('.text-video-*')))
        self.assertEqual(actual[-1]['subtitles'].path,str(self.srt))
        self.assertEqual(actual[-1]['background_path'],str(self.video))
        self.assertEqual(actual[-1]['voice_path'],str(self.voice))
        self.assertEqual(actual[-1]['task_directory'],str(self.root))
        self.assertEqual(actual[-1]['task_id'],'8009')

    def test_real_playback_uses_same_inputs_effects_and_does_not_advance_pool(self):
        self.media(); state,_,_=self.save(); state['settings']['use_voice']=True
        store=Store(self.root/'store'); state=store.save(state,0); before=store.path.read_bytes()
        bundle,registry,_=fx.prepare_bundle(state,['job-a'],self.services)
        key=preview_key(self.job,state,bundle); results=[]
        worker=PlaybackWorker(self.job,state,store.directory,key,node_bundle=bundle,node_registry=registry)
        self.addCleanup(worker.deleteLater); worker.ready.connect(results.append); worker.run()
        self.assertTrue(results); self.assertNotIn('error',results[0],results[0].get('error'))
        output,_=render_static_text(self.job,state,self.root,node_effects=fx.EffectRuntime(bundle,registry))
        a,b=self.frame(results[0]['path']),self.frame(output)
        self.assertLess(max(ImageStat.Stat(ImageChops.difference(a,b)).mean),3)
        self.assertEqual(store.path.read_bytes(),before)

    def test_real_graph_media_inputs_are_rebased_before_voice_and_music(self):
        self.media(); graph=self.text_graph(('标题',))
        image=self.root/'effect.png'; Image.new('RGB',(32,32),'yellow').save(image)
        media_node=new_node(layer_node('image'),(340,300))
        media_node['params']['layer'].update(source='file',source_file=str(image),path=str(image),x=50,y=70,width=25,height=20)
        graph['nodes'].append(media_node); graph['connections'].append([media_node['id'],'operations',graph['output_node'],'operations'])
        state,_,_=self.save(graph); state['settings'].update(use_voice=True,music_enabled=True,skip_silence=False)
        state['music']=[dict(id='m',path=str(self.voice),name='music',cursor=0)]
        bundle,registry,_=fx.prepare_bundle(state,['job-a'],self.services)
        output,_=render_static_text(self.job,state,self.root,node_effects=fx.EffectRuntime(bundle,registry))
        info=probe(output,str(Path(FFMPEG).with_name('ffprobe.exe')))
        self.assertTrue(info['audio'])
        pixel=self.frame(output).getpixel((160,398)); self.assertGreater(pixel[0],180); self.assertGreater(pixel[1],180)

    def test_real_simulated_output_and_operations_preview(self):
        graph=fx.example(self.registry)
        graph['simulation']={'width':320,'height':568,'duration':2,'color':'#345e70'}
        task=fx.preview_task(graph); payload,_=prepare_job(graph,self.registry,task,None)
        payload.update(ffmpeg=FFMPEG,preview_node=graph['output_node'])
        result=run_preview(payload,context(),registry=self.registry)
        self.assertTrue(result['simulation']); self.assertTrue(result['png'])
        image=Image.open(io.BytesIO(base64.b64decode(result['png'])))
        self.assertEqual(image.size,(320,568)); self.assertGreater(ImageStat.Stat(image).stddev[0],5)
        caption=next(n for n in graph['nodes'] if n['kind']=='batch_text_video.caption')
        payload,_=prepare_job(graph,self.registry,task,None,root_id=caption['id'])
        payload.update(ffmpeg=FFMPEG,preview_node=caption['id'])
        self.assertTrue(run_preview(payload,context(),registry=self.registry)['png'])

    def test_real_legacy_srt_relative_asset_and_multiple_operation_order(self):
        self.media(); graph=fx.example(self.registry)
        caption=next(n for n in graph['nodes'] if n['kind']=='batch_text_video.caption')
        caption['params']['caption']=None
        graph['connections']=[e for e in graph['connections'] if e[3]!='background_color']
        state,_,_=self.save(graph); bundle,registry,_=fx.prepare_bundle(state,['job-a'],self.services)
        output,_=render_static_text(self.job,state,self.root,node_effects=fx.EffectRuntime(bundle,registry))
        self.assertGreater(max(ImageStat.Stat(self.frame(output)).stddev),5)
        state,_,graph=self.save(self.text_graph())
        bundle,registry,payload,c=self.payload_context(state)
        operations=evaluate_graph(payload['graph'],registry,c)['operations']
        self.assertEqual([json.loads(dict(op.options)['layer'])['text'] for op in operations],['第一层','第二层'])
        state['settings']['overwrite']=True
        output,_=render_static_text(self.job,state,self.root,node_effects=fx.EffectRuntime(bundle,registry))
        self.assertGreater(max(ImageStat.Stat(self.frame(output)).stddev),5)

    def test_real_component_opacity_and_missing_subtitle_failure_cleanup(self):
        self.media(); state,effect,_=self.save(self.text_graph(('文字',)))
        effect=next(v for v in state['settings']['layers'] if v['kind']=='node_effect')
        bundle,registry,_=fx.prepare_bundle(state,['job-a'],self.services)
        effect['opacity']=100
        full,_=render_static_text(self.job,state,self.root,node_effects=fx.EffectRuntime(bundle,registry))
        full_image=self.frame(full); effect['opacity']=25; state['settings']['overwrite']=True
        faded,_=render_static_text(self.job,state,self.root,node_effects=fx.EffectRuntime(bundle,registry))
        base=self.frame(self.video)
        self.assertLess(sum(ImageStat.Stat(ImageChops.difference(base,self.frame(faded))).mean),
                        sum(ImageStat.Stat(ImageChops.difference(base,full_image)).mean)*.5)
        state,_,_=self.save(); self.srt.unlink()
        bundle,registry,_=fx.prepare_bundle(state,['job-a'],self.services)
        job=dict(self.job,name='missing')
        with self.assertRaisesRegex(GraphError,'空|缺|字幕'):
            render_static_text(job,state,self.root,node_effects=fx.EffectRuntime(bundle,registry))
        self.assertFalse((self.root/'missing.mp4').exists()); self.assertFalse(list(self.root.glob('.text-video-*')))


if __name__=='__main__': unittest.main()
