"""Conversion parity, frozen sources, successful-only rotation and UI undo."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets, QtGui, QtCore
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import ColorValue, ResourceRef
from model.node_workflow.graph import validate, evaluate_graph, new_node
from model.node_workflow.library import WorkflowLibrary
from model.node_workflow.scope import initialize_execution
from app_plugins.builtin.batch_text_video import node_effects as fx
from app_plugins.builtin.batch_text_video.component_nodes import component_graph, SOURCE_KIND
from app_plugins.builtin.batch_text_video.components import new_text_component, commit_sequences
from app_plugins.builtin.batch_text_video.layers import normalize_layers, componentize_layers, DEFAULT_LAYERS
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.workflow_component_source import build_source, prepare_source
from app_plugins.builtin.batch_text_video.workflow_component_source_ui import ComponentSourceEditor
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog

FFMPEG = os.environ.get('NODE_FFMPEG', '')


class ComponentNodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = fx.fallback_registry()
        self.library = WorkflowLibrary(self.root/'library')
        self.services = SimpleNamespace(available_nodes=lambda:self.registry, workflow_library=self.library,
            open_workflow_editor=lambda **kw:None, log=lambda *_,**kw:None)
        self.store = Store(self.root/'store')
        self.state = self.store.save(fresh_state(), 0)
        self.video = self.root/'background.mp4'; self.video.write_bytes(b'fixture')
        self.srt = self.root/'task_audio.srt'
        self.srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nBožia láska.\n', 'utf8')
        self.job = dict(id='job', path=str(self.video), title='Božia', body='láska.', name='final',
            task_dir=str(self.root), task_id='8001', subtitle_path=str(self.srt))

    def conversion(self, layer):
        graph, effect = component_graph(layer, self.state['settings'], self.registry)
        errors = validate(graph, self.registry)[0]
        self.assertEqual(errors, [])
        graph = self.library.save(graph, 'converted', self.registry)
        effect['workflow_id'] = graph['workflow_id']
        state = copy.deepcopy(self.state)
        state['jobs'] = [copy.deepcopy(self.job)]
        base = [dict(v, enabled=False) for v in DEFAULT_LAYERS if v['id'] != layer['id']]
        state['settings']['layers'] = normalize_layers(base+[effect])
        return graph, effect, state

    def execute(self, state, effect):
        bundle, registry, _ = fx.prepare_bundle(state, ['job'], self.services)
        payload = bundle['jobs']['job'][effect['workflow_id']]
        c = TaskRunContext(threading.Event(), lambda *_:None, lambda *_:None)
        initialize_execution(payload, c); c.prepared = payload['prepared']
        c.workflow_input = dict(type=fx.TYPE_ID, job=copy.deepcopy(self.job),
            video=ResourceRef.capture(self.video,'video'), subtitles=ResourceRef.capture(self.srt,'subtitles'),
            audio=None, voice_path='', duration=1., width=320, height=568, effect_layer=effect,
            alignment_directory=str(self.root))
        return evaluate_graph(payload['graph'],registry,c), c, bundle

    def text(self, **kw):
        layer = new_text_component(); layer.update(text='Ahoj', font='Open Sans', font_weight=650,
            x=31, y=42, width=73, height=38, outline_color='#bb2233', outline_width=3,
            background_enabled=True, background_color='#abddee', opacity=63, **kw)
        return layer

    def test_text_style_and_anchors_are_copied_not_shared(self):
        layer = self.text(); layer.update(x_anchor='left', x_margin=2)
        before = copy.deepcopy(layer)
        graph,effect,state = self.conversion(layer)
        node = next(n for n in graph['nodes'] if n['kind']=='batch_text_video.text_layer')
        normalized = next(v for v in componentize_layers([layer],state['settings']) if v['id']==layer['id'])
        self.assertEqual(node['params']['layer'], normalized)
        self.assertEqual(effect['opacity'],100)
        layer['x']=99
        self.assertEqual(node['params']['layer']['x'],before['x'])
        result,_,_=self.execute(state,effect)
        actual=json.loads(dict(result['operations'][0].options)['layer'])
        for key in ('font','font_weight','opacity','x','y','width','height','outline_color','background_color'):
            self.assertEqual(actual[key],before[key])

    def test_title_uses_each_current_job_and_does_not_resurrect_default(self):
        layer=next(v for v in componentize_layers(None,self.state['settings']) if v['kind']=='title')
        graph,effect,state=self.conversion(layer)
        self.assertEqual(effect['id'],'title')
        self.assertFalse(any(v['kind']=='title' for v in normalize_layers(state['settings']['layers'])))
        op=next(n for n in graph['nodes'] if n['kind']=='batch_text_video.text_layer')
        self.assertIn(['title',op['id'],'content'],[e[1:] for e in graph['connections'] if e[1]=='title'])
        result,_,_=self.execute(state,effect)
        self.assertEqual(json.loads(dict(result['operations'][0].options)['layer'])['text'],'Božia')
        self.job['title']='Ďalší titulok'
        result,_,_=self.execute(state,effect)
        self.assertEqual(json.loads(dict(result['operations'][0].options)['layer'])['text'],'Ďalší titulok')

    def test_random_palette_can_be_wired_without_losing_style(self):
        layer=self.text(); graph,effect,state=self.conversion(layer)
        palette=new_node(self.registry.definitions['core.text_palette'])
        graph['nodes'].append(palette)
        op=next(n for n in graph['nodes'] if n['kind']=='batch_text_video.text_layer')
        graph['connections'].append([palette['id'],'text',op['id'],'color'])
        self.library.save(graph,'converted',self.registry)
        first,_,_=self.execute(state,effect); second,_,_=self.execute(state,effect)
        self.assertEqual(first,second)
        actual=json.loads(dict(first['operations'][0].options)['layer'])
        self.assertNotEqual(actual['color'],layer['color'])
        self.assertEqual(actual['font_weight'],650)

    def test_missing_optional_task_text_remains_a_noop(self):
        layer=next(v for v in componentize_layers(None,self.state['settings']) if v['kind']=='title')
        _,effect,state=self.conversion(layer)
        self.job['title']=''
        result,_,_=self.execute(state,effect)
        self.assertEqual(result['operations'][0].name,'null')

    def test_fixed_text_file_remains_live_and_is_not_baked_into_graph(self):
        path=self.root/'copy.txt'; path.write_text('Prvý','utf8')
        layer=self.text(); layer.update(source='file',source_file=str(path))
        graph,effect,state=self.conversion(layer)
        for text in ('Prvý','Druhý'):
            path.write_text(text,'utf8')
            result,_,_=self.execute(state,effect)
            self.assertEqual(json.loads(dict(result['operations'][0].options)['layer'])['text'],text)

    def test_sequence_cursor_advances_only_with_completed_ledger(self):
        layer=self.text(); layer.update(source='sequence',sequence_items=[{'id':'a','text':'A'},
            {'id':'b','text':'B'},{'id':'c','text':'C'}],sequence_cursor=1)
        graph,effect,state=self.conversion(layer)
        result,c,_=self.execute(state,effect)
        self.assertEqual(json.loads(dict(result['operations'][0].options)['layer'])['text'],'B')
        plans=list(c.component_source_steps.values())
        self.assertNotIn('source_rotations',effect)
        again,_,_=self.execute(state,effect)
        self.assertEqual(result,again) # preview/failure does not consume it
        state['settings']['layers']=commit_sequences(state['settings']['layers'],plans)
        next_effect=next(v for v in state['settings']['layers'] if v['id']==effect['id'])
        result,_,_=self.execute(state,next_effect)
        self.assertEqual(json.loads(dict(result['operations'][0].options)['layer'])['text'],'C')
        with self.assertRaisesRegex(ValueError,'位置已改变'):
            commit_sequences(state['settings']['layers'],plans)

    def test_bound_pool_is_live_profile_specific_and_preserves_next_id(self):
        self.state['copy_pool']=[dict(id='a',title='A',body='一'),dict(id='b',title='B',body='二')]
        self.state=self.store.save(self.state,self.state['revision'])
        layer=self.text(); layer.update(source='pool',pool_field='title',copy_binding='rotate',sequence_items=[{'id':'a','text':'A'},
            {'id':'b','text':'B'}],sequence_cursor=1)
        graph,effect,state=self.conversion(layer)
        source=next(n['params']['source'] for n in graph['nodes'] if n['kind']==SOURCE_KIND)
        self.assertEqual(source['sequence_items'],[])
        self.assertEqual(source['pool_next_id'],'b')
        result,c,_=self.execute(state,effect)
        self.assertEqual(json.loads(dict(result['operations'][0].options)['layer'])['text'],'B')
        state['settings']['layers']=commit_sequences(state['settings']['layers'],list(c.component_source_steps.values()))
        self.state['copy_pool'].reverse(); self.state['copy_pool'][1]['title']='NEW A'
        self.state=self.store.save(self.state,self.state['revision'])
        state['copy_pool']=copy.deepcopy(self.state['copy_pool'])
        effect=next(v for v in state['settings']['layers'] if v['id']==effect['id'])
        result,_,_=self.execute(state,effect)
        self.assertEqual(json.loads(dict(result['operations'][0].options)['layer'])['text'],'NEW A')

    def test_caption_preserves_style_and_native_task_cue_grouping(self):
        layer=new_caption(); layer.update(caption_timing='task',caption_size_contrast=2.5,opacity=75)
        graph,effect,state=self.conversion(layer)
        styled=next(n for n in graph['nodes'] if n['kind']=='batch_text_video.caption')['params']['caption']
        self.assertEqual(styled['caption_size_contrast'],2.5); self.assertEqual(styled['opacity'],75)
        result,_,_=self.execute(state,effect)
        track=dict(result['operations'][0].assets)['filename']
        self.assertEqual(len(track.cues),1); self.assertEqual(track.cues[0].text,'Božia láska.')

    def test_converted_caption_reuses_ele_dynamic_words_without_model_or_http(self):
        from model.ElevenLabsTimings import save_timings
        from app_plugins.builtin.batch_text_video.workflow_caption import subtitle_words
        audio=self.root/'task_audio.mp3'; audio.write_bytes(b'fixture')
        words=[dict(text='Božia',start=.1,end=.4),dict(text='láska.',start=.5,end=.9)]
        save_timings(audio,words,1.,source='test')
        layer=new_caption(); layer.update(caption_timing='auto',caption_source='full')
        _,effect,state=self.conversion(layer)
        self.job.update(voice_path=str(audio),voice_override=True)
        # This test's execute helper passes explicit inputs; inject the real
        # voice path at the existing shared adapter, not a separate renderer.
        from app_plugins.builtin.batch_text_video.captions import task_word_timings as native
        def actual(job,voice,value,title,body,*args):
            return native(job,str(audio),value,title,body,*args)
        with patch('app_plugins.builtin.batch_text_video.captions.task_word_timings',side_effect=actual), \
                patch('app_plugins.builtin.batch_text_video.captions.load_word_timings',side_effect=AssertionError('local ASR')):
            result,_,_=self.execute(state,effect)
        track=dict(result['operations'][0].assets)['filename']
        self.assertEqual(track.timing,'word_aligned')
        self.assertEqual(subtitle_words(track,1.,False),words)
        self.assertEqual(subtitle_words(track,1.,True),words)

    def test_caption_model_and_uniform_use_original_shared_timing_function(self):
        for mode in ('model','uniform','auto'):
            layer=new_caption(); layer.update(caption_timing=mode,caption_language='sk',caption_source='full')
            graph,effect,state=self.conversion(layer)
            with patch('app_plugins.builtin.batch_text_video.captions.task_word_timings',
                    return_value=[dict(text='Božia',start=0.,end=.5),dict(text='láska.',start=.5,end=1.)]) as align:
                result,_,_=self.execute(state,effect)
            args=align.call_args.args
            self.assertEqual(args[2]['caption_timing'],mode)
            self.assertEqual(args[2]['caption_language'],'sk')
            self.assertEqual(args[3:5],('Božia','láska.'))
            self.assertEqual(len(dict(result['operations'][0].assets)['filename'].cues),2)

    def test_fixed_srt_and_caption_sequence_keep_their_original_file(self):
        other=self.root/'other.srt'; other.write_text(self.srt.read_text('utf8').replace('Božia láska.','Iný text.'),'utf8')
        for source in ('file','sequence'):
            layer=new_caption(); layer.update(source=source,caption_timing='file',source_file=str(other),
                caption_file=str(other),sequence_items=[{'id':'a','caption_file':str(other)}])
            _,effect,state=self.conversion(layer)
            result,_,_=self.execute(state,effect)
            self.assertEqual(dict(result['operations'][0].assets)['filename'].cues[0].text,'Iný text.')

    def test_visual_pool_handles_still_and_animated_without_reconfiguration(self):
        png=self.root/'a.png'; image=QtGui.QImage(20,20,QtGui.QImage.Format.Format_RGBA8888); image.fill(QtGui.QColor('#ee55aa')); image.save(str(png))
        mp4=self.root/'b.mp4'; mp4.write_bytes(b'fixture')
        layer=dict(id='visual',kind='image',name='叠加',source='sequence',path='',enabled=True,opacity=87,
            x=44,y=55,width=140,height=130,key_mode='green',sequence_cursor=0,
            sequence_items=[dict(id='a',path=str(png)),dict(id='b',path=str(mp4))])
        graph,effect,state=self.conversion(layer)
        self.assertTrue(any(n['kind']=='batch_text_video.visual_layer' for n in graph['nodes']))
        first,c,_=self.execute(state,effect)
        data=json.loads(dict(first['operations'][0].options)['layer'])
        self.assertEqual((data['path'],data['width'],data['key_mode']),(str(png),140,'green'))
        state['settings']['layers']=commit_sequences(state['settings']['layers'],list(c.component_source_steps.values()))
        effect=next(v for v in state['settings']['layers'] if v['id']=='visual')
        result,_,_=self.execute(state,effect)
        self.assertEqual(json.loads(dict(result['operations'][0].options)['layer'])['path'],str(mp4))

    def test_source_file_stamp_change_after_submission_is_rejected(self):
        path=self.root/'copy.txt'; path.write_text('one','utf8')
        params=dict(source=dict(id='s',kind='text',name='s',source='file',source_file=str(path)),store_file=str(self.store.path),profile_id='default')
        c=TaskRunContext(threading.Event(),lambda *_:None,lambda *_:None)
        c.node_prepared=prepare_source(None,params,None); c.node_id='source'
        c.workflow_input=dict(type=fx.TYPE_ID,job=self.job)
        path.write_text('changed after snapshot','utf8')
        with self.assertRaises((ValueError,RuntimeError)): build_source({},params,c)

    def test_watermark_remains_one_effect_and_keeps_native_corner_geometry(self):
        from app_plugins.builtin.batch_text_video.watermarks import new_watermark, expand_watermarks
        layer=new_watermark(); layer['enabled']=True
        for key in layer['corners']:
            layer['corners'][key].update(text=key,source='fixed',enabled=True)
        layer['opacity']=70
        graph,effect,state=self.conversion(layer)
        result,_,_=self.execute(state,effect)
        expected=expand_watermarks([next(v for v in normalize_layers([layer]) if v['kind']=='watermark')])
        self.assertEqual(len(result['operations']),4)
        for op, original in zip(result['operations'],expected):
            actual=json.loads(dict(op.options)['layer'])
            for key in ('x','y','width','height','opacity','_watermark_corner','text'):
                self.assertEqual(actual.get(key),original.get(key))
        self.assertEqual(len([v for v in state['settings']['layers'] if v['kind']=='node_effect']),1)

    def test_disabled_caption_needs_no_audio_or_subtitles_until_connected(self):
        layer=new_caption(); layer['enabled']=False
        graph,effect,state=self.conversion(layer)
        self.assertFalse(effect['enabled'])
        self.assertEqual(fx.prepare_bundle(state,['job'],self.services),(None,None,()))
        effect['enabled']=True
        state['settings']['layers']= [effect if v['id']==effect['id'] else v for v in state['settings']['layers']]
        result,_,_=self.execute(state,effect)
        self.assertEqual(len(result['operations']),1)

    def test_source_inspector_retains_timing_and_emits_only_source_patch(self):
        layer=new_caption(); layer.update(caption_timing='model',caption_language='sk')
        graph,_,_=self.conversion(layer)
        params=next(n['params'] for n in graph['nodes'] if n['kind']==SOURCE_KIND)
        changes=[]; widget=ComponentSourceEditor(params,changes.append); self.addCleanup(widget.deleteLater)
        self.assertFalse(changes)
        widget.timing.setCurrentIndex(widget.timing.findData('uniform'))
        self.assertEqual(set(changes[-1]),{'source'})
        self.assertEqual(changes[-1]['source']['caption_timing'],'uniform')
        self.assertEqual(changes[-1]['source']['caption_language'],'sk')

    def test_rotation_shape_is_checked(self):
        value=fx.new_effect(); value['source_rotations']={'bad':dict(signature='x',cursor=-1,next_id='a')}
        with self.assertRaises(ValueError): normalize_layers([value])

    def test_source_media_append_button_is_connected_and_bounded(self):
        layer=dict(id='v',kind='image',source='sequence',name='v',sequence_items=[],sequence_cursor=0)
        params=dict(source=layer,store_file=str(self.store.path),profile_id='default')
        changes=[]; widget=ComponentSourceEditor(params,changes.append); self.addCleanup(widget.deleteLater)
        with patch.object(QtWidgets.QFileDialog,'getOpenFileNames',return_value=([str(self.video)],'')):
            widget.content.add_images.click()
        self.assertEqual(changes[-1]['source']['sequence_items'][0]['path'],str(self.video))

    @unittest.skipUnless(FFMPEG,'FFmpeg not configured')
    def test_real_runtime_commits_rotations_only_for_successful_jobs(self):
        from dataclasses import replace
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        from app_plugins.builtin.batch_text_video.background import execute_batch
        p=subprocess.run([FFMPEG,'-y','-hide_banner','-loglevel','error','-f','lavfi','-i',
            'color=c=0x345e70:s=320x568:r=10:d=1','-c:v','libx264','-pix_fmt','yuv420p',str(self.video)],
            capture_output=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(p.returncode,0,p.stderr)
        layer=self.text(); layer.update(source='sequence',sequence_items=[dict(id='a',text='A'),dict(id='b',text='B')])
        _,effect,state=self.conversion(layer)
        state['jobs']=[dict(self.job,id='j'+str(i),name='video'+str(i)) for i in range(3)]
        state['settings'].update(music_enabled=False,use_voice=False,darkness=0,ffmpeg_path=FFMPEG,
            aspect='source',fit='cover',auto_crop_black_bars=False,overwrite=True)
        state=self.store.save(state,state['revision'])
        seen=[]; definition=self.registry.definitions[SOURCE_KIND]
        def source(i,p,c):
            values=definition.build(i,p,c); seen.append(values['content']); return values
        self.registry.definitions[SOURCE_KIND]=replace(definition,build=source)
        ids=[v['id'] for v in state['jobs']]
        bundle,registry,_=fx.prepare_bundle(state,ids,self.services)
        runtime=fx.EffectRuntime(bundle,registry)
        def renderer(job,*args,**kw):
            result=render_static_text(job,*args,**kw)
            if job['id']=='j1': raise RuntimeError('synthetic publish failure')
            return result
        result,final=execute_batch(self.store,state,ids,threading.Event(),lambda *_:None,lambda *_:None,
            lambda *_:None,renderers={state['settings']['scheme']:renderer},node_effects=runtime)
        self.assertEqual((result['completed'],result['failed']),(2,1),result)
        self.assertEqual(seen,['A','B','B'])
        actual=next(v for v in final['settings']['layers'] if v['id']==layer['id'])
        self.assertEqual(next(iter(actual['source_rotations'].values()))['next_id'],'a')

    @unittest.skipUnless(FFMPEG,'FFmpeg not configured')
    def test_real_title_image_and_caption_conversion_matches_native_export_and_preview(self):
        import io
        from PIL import Image, ImageChops, ImageStat
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        from app_plugins.builtin.batch_text_video.playback import PlaybackWorker, preview_key
        def ffmpeg(args):
            p=subprocess.run([FFMPEG,'-nostdin','-hide_banner','-loglevel','error',*args],capture_output=True,
                timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(p.returncode,0,p.stderr.decode('utf8','replace')); return p.stdout
        def frame(path):
            raw=ffmpeg(['-ss','0.5','-i',str(path),'-frames:v','1','-vcodec','png','-f','image2pipe','-'])
            return Image.open(io.BytesIO(raw)).convert('RGB')
        ffmpeg(['-y','-f','lavfi','-i','color=c=0x345e70:s=320x568:r=10:d=1','-c:v','libx264',
            '-pix_fmt','yuv420p',str(self.video)])
        voice=self.root/'task_audio.wav'
        ffmpeg(['-y','-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','pcm_s16le',str(voice)])
        self.job['voice_path']=str(voice)
        image=self.root/'overlay.png'; Image.new('RGBA',(60,80),(190,110,230,170)).save(image)
        fixtures=[next(v for v in componentize_layers(None,self.state['settings']) if v['kind']=='title'),
            dict(id='visual',kind='image',name='叠加',source='file',source_file=str(image),path=str(image),
                x=52,y=44,width=80,height=70,opacity=65,enabled=True),
            dict(new_caption(),caption_timing='task',font_min=20,font_max=30)]
        for layer in fixtures:
            _,effect,converted=self.conversion(layer)
            converted['settings'].update(music_enabled=False,use_voice=layer['kind']=='caption',darkness=0,ffmpeg_path=FFMPEG,
                aspect='source',fit='cover',auto_crop_black_bars=False,overwrite=True)
            native=copy.deepcopy(converted)
            native['settings']['layers']=[layer if v['id']==layer['id'] else v for v in converted['settings']['layers']]
            original_path,_=render_static_text(self.job,native,self.store.directory)
            expected=frame(original_path)
            bundle,registry,_=fx.prepare_bundle(converted,['job'],self.services)
            actual_path,_=render_static_text(self.job,converted,self.store.directory,
                node_effects=fx.EffectRuntime(bundle,registry))
            self.assertLess(max(ImageStat.Stat(ImageChops.difference(expected,frame(actual_path))).mean),3,layer['kind'])
            key=preview_key(self.job,converted,bundle)
            results=[]; worker=PlaybackWorker(self.job,converted,self.store.directory,key,node_bundle=bundle,node_registry=registry)
            self.addCleanup(worker.deleteLater); worker.ready.connect(results.append); worker.run()
            self.assertTrue(results); self.assertNotIn('error',results[0],results[0].get('error'))
            self.assertLess(max(ImageStat.Stat(ImageChops.difference(expected,frame(results[0]['path']))).mean),3,layer['kind'])
            self.assertFalse(list(self.root.glob('.text-video-*')))


class ComponentNodeUiTests(ComponentNodeTests):
    def dialog(self, layer):
        state=copy.deepcopy(self.state)
        state['settings']['layers']=normalize_layers([dict(v,enabled=False) for v in DEFAULT_LAYERS]+[layer])
        self.store.save(state,state['revision'])
        dialog=BatchTextVideoDialog(self.services,self.store)
        self.addCleanup(dialog.close)
        return dialog

    def test_convert_save_open_and_undo_redo_without_touching_resources(self):
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            layer=self.text(); dialog=self.dialog(layer)
            before=copy.deepcopy(dialog.state['copy_pool'])
            calls=[]; self.services.open_workflow_editor=lambda **kw:calls.append(kw)
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('我的文字节点',True)),patch.object(QtWidgets.QMessageBox,'warning') as warning:
                dialog.convert_component_to_nodes(layer['id'])
            self.assertFalse(warning.called, warning.call_args)
            actual=next(v for v in self.store.load()['settings']['layers'] if v['id']==layer['id'])
            self.assertEqual(actual['kind'],'node_effect'); self.assertEqual(actual['opacity'],100)
            self.assertEqual(calls[-1]['workflow_id'],actual['workflow_id'])
            self.assertEqual(self.library.list(fx.TYPE_ID)[0]['name'],'我的文字节点')
            self.assertTrue(dialog.undo_edit())
            restored=next(v for v in dialog.layer_panel.values() if v['id']==layer['id'])
            self.assertEqual(restored['kind'],'text'); self.assertEqual(restored['x'],31)
            self.assertTrue(dialog.redo_edit())
            self.assertEqual(next(v for v in dialog.layer_panel.values() if v['id']==layer['id'])['workflow_id'],actual['workflow_id'])
            self.assertEqual(dialog.state['copy_pool'],before)

    def test_cancel_and_library_failure_leave_component_unchanged(self):
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            layer=self.text(); dialog=self.dialog(layer)
            before=dialog.layer_panel.values()
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('',False)):
                dialog.convert_component_to_nodes(layer['id'])
            self.assertEqual(dialog.layer_panel.values(),before); self.assertEqual(self.library.list(),[])
            with patch.object(QtWidgets.QInputDialog,'getText',return_value=('name',True)),patch.object(self.library,'save',side_effect=OSError('full')),patch.object(QtWidgets.QMessageBox,'warning') as warning:
                dialog.convert_component_to_nodes(layer['id'])
            self.assertTrue(warning.called); self.assertEqual(dialog.layer_panel.values(),before)

    def test_layer_context_menu_has_direct_conversion_entry(self):
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            layer=self.text(); dialog=self.dialog(layer)
            dialog.layer_panel.select_layer_id(layer['id'])
            found=[]
            def menu_exec(menu, _point):
                actions=menu.actions(); found.extend(a.text() for a in actions)
                self.assertTrue(actions[0].isEnabled())
            rect=dialog.layer_panel.list.visualItemRect(dialog.layer_panel.list.currentItem())
            with patch.object(QtWidgets.QMenu,'exec',menu_exec):
                dialog.layer_panel._component_menu(rect.center())
            self.assertIn('转换为节点效果…',found)


if __name__=='__main__': unittest.main()
