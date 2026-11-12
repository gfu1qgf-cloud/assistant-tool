"""Shared visual lifetimes: signed endpoints, real composition, UI and old projects."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import engine, layer_timing as timing
from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS, ImageCache
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.captions import new_caption, static_planes
from app_plugins.builtin.batch_text_video.watermarks import new_watermark, expand_watermarks
from app_plugins.builtin.batch_text_video.node_effects import new_effect
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layout import render_overlay, ensure_fonts
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.visual_animation import show_window

FFMPEG = os.environ.get('NODE_FFMPEG', '')


class ControlTimeWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def text(self, kind='text', **values):
        layer = dict(new_text_component(), kind=kind, text='WORD', source='fixed', x=50, y=50,
                     width=90, height=70, font_min=100, font_max=100, color='#ffffff', outline_width=0,
                     background_enabled=False, shadow_enabled=False, padding=0, word_spacing=0)
        if kind in {'title', 'body'}:
            layer.update(id=kind, source='job')
        layer.update(values)
        return normalize_layers([layer])[0]

    def settings(self, layer):
        state = fresh_state()
        state['settings'].update(darkness=0, background_tint_strength=0, background_blur=0,
            music_enabled=False, use_voice=False, ffmpeg_path=FFMPEG, video_encoder='cpu',
            aspect='landscape', unvoiced_duration_mode='fixed', unvoiced_duration_seconds=2.4,
            layers=[layer]+[dict(x, enabled=False) for x in DEFAULT_LAYERS if x['id'] != layer['id']])
        return state

    def test_signed_endpoints_clamp_and_empty_intervals_do_not_extend_movie(self):
        for start, end, expected in ((0, 0, (0, 10)), (2, 5, (2, 5)), (0, -3, (0, 7)),
                                      (-3, 0, (7, 10)), (-5, -1, (5, 9)), (-50, 50, (0, 10)),
                                      (12, 0, (10, 10)), (4, -8, (4, 4))):
            with self.subTest(start=start, end=end):
                layer = timing.normalize(dict(show_start=start, show_end=end))
                self.assertEqual(timing.bounds(layer, 10), expected)
                self.assertEqual(timing.visible(layer, dict(duration=10, time=expected[1])), not timing.active(layer))
        for values in (dict(show_end=float('nan')), dict(show_start=True), dict(show_end=-86401),
                       dict(show_start=5, show_end=2), dict(show_start=-1, show_end=-3)):
            with self.assertRaises(ValueError):
                timing.normalize(values)

    def test_old_projects_default_to_full_visibility_and_old_enabled_ranges_migrate(self):
        for layer in normalize_layers():
            self.assertEqual((layer['show_start'], layer['show_end']), (0., 0.))
        old = dict(id='photo', kind='image', path='missing.png', visual_timing_enabled=True,
                   visual_timing=dict(start=2., end=8., enter='left', exit='cut'))
        layer = normalize_layers([old])[0]
        self.assertEqual((layer['show_start'], layer['show_end']), (2., 8.))
        self.assertEqual((show_window(layer, 10).start, show_window(layer, 10).end), (2., 8.))
        layer['visual_timing_enabled'] = False
        self.assertEqual((show_window(layer, 10).start, show_window(layer, 10).end), (2., 8.))
        layer.update(show_start=-3., show_end=0.)
        self.assertEqual((show_window(layer, 10).start, show_window(layer, 10).end), (7., 10.))
        self.assertEqual(normalize_layers([layer])[0], layer)

    def test_each_visual_kind_has_common_fields_and_watermark_children_inherit_group(self):
        obj = new_watermark(); obj.update(enabled=True, show_start=1., show_end=-2.)
        obj['corners']['top_left'].update(enabled=True, text='W')
        layers = normalize_layers([self.text(), new_caption(), obj, new_effect(),
                                   dict(id='pic', kind='image', path='missing.png')])
        self.assertEqual({l['kind'] for l in layers}, {'title','body','text','caption','watermark','node_effect','image'})
        for layer in layers:
            self.assertIn('show_start', layer); self.assertIn('show_end', layer)
        child = next(x for x in expand_watermarks(layers) if x.get('_watermark_corner') == 'top_left')
        self.assertEqual(timing.bounds(child, 10), (1., 8.))
        self.assertFalse(timing.visible(child, dict(duration=10, time=.5)))

    def test_common_controls_cover_all_factories_and_survive_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            pic = Path(root)/'pic.png'; image = QtGui.QImage(20,20,QtGui.QImage.Format.Format_ARGB32)
            image.fill(QtGui.QColor('red')); image.save(str(pic))
            obj = new_watermark()
            layers = normalize_layers([self.text(), new_caption(), obj, new_effect(),
                                       dict(id='pic', kind='image', path=str(pic))])
            panel = LayerPanel(layers, root, settings=fresh_state()['settings'])
            try:
                for layer in panel.values():
                    panel.select_layer_id(layer['id'])
                    editor = panel.inspector.active.timing_editor
                    editor.controls['show_end'].setValue(-.5)
                    self.assertEqual(panel.current()['show_end'], -.5, layer['kind'])
                    self.assertEqual(editor.controls['show_start'].minimum(), -86400)
                panel.select_background()
                panel.inspector.active.timing_editor.controls['show_end'].setValue(1.5)
                self.assertEqual(panel.background_values()['show_end'], 1.5)
                state = fresh_state(); state['settings']['layers'] = panel.values()
                state['settings']['background_visual'] = panel.background_values()
                store = Store(root); store.save(state, 0)
                loaded = store.load()
                self.assertTrue(all(l['show_end'] == -.5 for l in loaded['settings']['layers']))
                self.assertEqual(loaded['settings']['background_visual']['show_end'], 1.5)
                if os.environ.get('LZX_CONTROL_TIME_QA'):
                    panel.select_layer_id('title'); panel.resize(550,850); panel.show(); self.app.processEvents()
                    panel.grab().save(str(Path(os.environ['LZX_CONTROL_TIME_QA'])/'control-time-title.png'))
            finally:
                panel.deleteLater(); QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_design_preview_and_static_batch_planes_keep_timed_title_separate(self):
        layer = self.text('title', show_start=.6, show_end=1.8)
        state = self.settings(layer)
        state['settings']['layers'] = [x for x in state['settings']['layers'] if x['id'] != 'body']
        state['settings']['layers'].append(self.text('body', show_start=-.6, show_end=0.))
        for time, present in ((0., False), (.9, True), (2., False)):
            settings = copy.deepcopy(state['settings'])
            settings['layers'] = [layer]+[dict(l, enabled=False) for l in DEFAULT_LAYERS if l['id'] != 'title']
            settings['_editor_timing'] = dict(duration=2.4, time=time)
            image, _ = render_overlay('WORD', '', 320,180,settings,ImageCache())
            self.assertEqual(bool(np.asarray(self.rgba(image))[:,:,3].max()), present)
        planes = static_planes('WORD','BODY',320,180,state['settings'],ImageCache())
        timed = [value[1]['id'] for kind,value in planes if kind == 'timed_image']
        self.assertEqual(timed, ['title','body'])

    def rgba(self, image):
        image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        return np.frombuffer(image.constBits().asstring(image.sizeInBytes()),np.uint8).reshape(
            image.height(), image.bytesPerLine())[:,:image.width()*4].reshape(image.height(),image.width(),4).copy()

    def encode(self, root, state, job=None, runtime=None):
        job = job or dict(id='job', path='', name='timing', title='WORD', body='BODY', status='待生成')
        state['settings'].update(output_dir=str(root), overwrite=True)
        state['jobs'] = [job]
        before = copy.deepcopy(state)
        with patch.object(engine, 'output_size', return_value=(320,180)):
            with engine.prepare_static_text(job, state, Path(root)/'private', node_effects=runtime) as prepared:
                prepared.encode(); output = prepared.publish()
        self.assertEqual(state, before)
        args=[FFMPEG,'-v','error','-nostdin','-i',str(output),'-f','rawvideo','-pix_fmt','rgb24','pipe:1']
        result=subprocess.run(args,capture_output=True,timeout=40,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        frames=np.frombuffer(result.stdout,np.uint8).reshape(-1,180,320,3)
        self.assertEqual(len(frames),72)
        return frames

    def test_time_edit_is_saved_and_undo_redo_restores_value(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, '_start_overlay'):
            dialog=BatchTextVideoDialog(store=Store(root))
            try:
                dialog.layer_panel.select_layer_id('title')
                editor=dialog.layer_panel.inspector.active.timing_editor
                editor.controls['show_end'].setValue(5.)
                self.assertTrue(dialog.persist())
                self.assertEqual(dialog.layer_panel.current()['show_end'],5.)
                self.assertTrue(dialog.undo_edit())
                self.assertEqual(dialog.layer_panel.current()['show_end'],0.)
                self.assertTrue(dialog.redo_edit())
                self.assertEqual(dialog.layer_panel.current()['show_end'],5.)
                saved=next(x for x in dialog.store.load()['settings']['layers'] if x['id']=='title')
                self.assertEqual(saved['show_end'],5.)
            finally:
                dialog.cancel_work();dialog.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    @unittest.skipUnless(FFMPEG, 'Explicit real FFmpeg required')
    def test_real_title_body_text_and_watermark_end_on_their_own_window(self):
        obj = new_watermark(); obj.update(enabled=True, show_start=.6, show_end=-.6)
        obj['corners']['top_left'].update(enabled=True, text='WORD', font_min=90, font_max=90, outline_width=0,
            watermark_placement='free', x=50, y=50, width=90, height=70)
        for kind, layer in [('title',self.text('title')), ('body',self.text('body')),
                            ('text',self.text()), ('watermark',normalize_layers([obj])[0])]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                layer.update(show_start=.6,show_end=-.6)
                frames = self.encode(root,self.settings(layer))
                for index, present in ((0,False),(12,False),(30,True),(60,False)):
                    self.assertEqual(bool(frames[index].max()>80),present,(kind,index))

    @unittest.skipUnless(FFMPEG, 'Explicit real FFmpeg required')
    def test_real_visual_window_works_without_animation_and_empty_window_keeps_full_video(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'red.png'; image=QtGui.QImage(80,60,QtGui.QImage.Format.Format_RGB32)
            image.fill(QtGui.QColor('red')); image.save(str(path))
            layer=normalize_layers([dict(id='photo',kind='image',path=str(path),x=50,y=50,width=100,height=100,
                                        show_start=.6,show_end=-.6,visual_timing_enabled=False)])[0]
            frames=self.encode(root,self.settings(layer))
            for i, present in ((0,False),(30,True),(60,False)):
                self.assertEqual(bool(frames[i].max()>80),present,i)
            layer.update(show_start=5.,show_end=0.)
            frames=self.encode(root,self.settings(layer))
            self.assertLess(frames.max(),10)

    @unittest.skipUnless(FFMPEG, 'Explicit real FFmpeg required')
    def test_real_bottom_background_window_preserves_full_output_clock(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'background.png'; image=QtGui.QImage(320,180,QtGui.QImage.Format.Format_RGB32)
            image.fill(QtGui.QColor('#e02020')); image.save(str(path))
            layer=self.text(enabled=False)
            state=self.settings(layer)
            state['settings']['background_visual']=dict(show_start=.6,show_end=-.6)
            frames=self.encode(root,state,dict(id='bg',path=str(path),name='bottom',title='',body='',status='待生成'))
            for i,present in ((0,False),(30,True),(60,False)):
                self.assertEqual(bool(frames[i].max()>80),present,i)

    @unittest.skipUnless(FFMPEG, 'Explicit real FFmpeg required')
    def test_real_scrolling_text_has_a_complete_finite_global_clock(self):
        with tempfile.TemporaryDirectory() as root:
            layer=self.text(text_animation='up',scroll_start_anchor='top',scroll_end_anchor='top',
                            scroll_edge_fade=0,show_start=.6,show_end=-.6)
            frames=self.encode(root,self.settings(layer))
            for i,present in ((0,False),(30,True),(60,False)):
                self.assertEqual(bool(frames[i].max()>80),present,i)

    @unittest.skipUnless(FFMPEG, 'Explicit real FFmpeg required')
    def test_real_node_control_and_node_layer_both_honor_their_own_window(self):
        from app_plugins.builtin.batch_text_video import node_effects as fx
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from model.node_workflow.library import WorkflowLibrary
        for mode in ('outer_control','inner_layer'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                layer=self.text(show_start=.6,show_end=-.6)
                state=self.settings(layer)
                job=dict(id='node',path='',name='node',title='',body='',status='待生成')
                state['jobs']=[job]
                registry=fx.fallback_registry()
                graph,effect=component_graph(layer,state['settings'],registry)
                self.assertEqual((effect['show_start'],effect['show_end']),(.6,-.6))
                if mode=='outer_control':
                    for node in graph['nodes']:
                        if 'layer' in node.get('params',{}):node['params']['layer'].update(show_start=0.,show_end=0.)
                else:
                    effect.update(show_start=0.,show_end=0.)
                library=WorkflowLibrary(Path(root)/'workflows')
                record=library.save(graph,'Timed text',registry)
                effect['workflow_id']=record['workflow_id']
                state['settings']['layers']=normalize_layers([effect]+[dict(x,enabled=False) for x in DEFAULT_LAYERS])
                bundle,registry,_=fx.prepare_bundle(state,[job['id']],SimpleNamespace(workflow_library=library,available_nodes=lambda:registry))
                runtime=fx.EffectRuntime(bundle,registry)
                frames=self.encode(root,state,job,runtime)
                for i,present in ((0,False),(30,True),(60,False)):
                    self.assertEqual(bool(frames[i].max()>80),present,(mode,i))

    @unittest.skipUnless(FFMPEG, 'Explicit real FFmpeg required')
    def test_real_caption_text_and_continuous_cursor_hide_together(self):
        import wave
        with tempfile.TemporaryDirectory() as root:
            voice=Path(root)/'voice.wav'
            with wave.open(str(voice),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
                audio.writeframes(np.zeros(round(2.4*16000),np.int16).tobytes())
            srt=Path(root)/'words.srt';srt.write_text('1\n00:00:00,000 --> 00:00:02,300\nWORD\n',encoding='utf8')
            layer=new_caption();layer.update(show_start=.6,show_end=-.6,caption_timing='task',caption_display_mode='char',
                caption_cursor=True,caption_cursor_continuous=True,caption_follow_mode='line',
                font_min=100,font_max=100,outline_width=0)
            layer=normalize_layers([layer])[0]
            state=self.settings(layer);state['settings']['use_voice']=True
            job=dict(id='cap',path='',name='caption',title='',body='WORD',voice_path=str(voice),subtitle_path=str(srt),status='待生成')
            frames=self.encode(root,state,job)
            for i,present in ((0,False),(30,True),(60,False)):
                self.assertEqual(bool(frames[i].max()>80),present,i)

    @unittest.skipUnless(FFMPEG, 'Explicit real FFmpeg required')
    def test_timed_translucent_caption_keeps_color_and_opacity_after_node_conversion(self):
        import wave
        from app_plugins.builtin.batch_text_video import node_effects as fx
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from model.node_workflow.library import WorkflowLibrary
        with tempfile.TemporaryDirectory() as root:
            voice=Path(root)/'voice.wav'
            with wave.open(str(voice),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
                audio.writeframes(np.zeros(round(2.4*16000),np.int16).tobytes())
            srt=Path(root)/'voice.srt';srt.write_text('1\n00:00:00,000 --> 00:00:02,300\nWORD\n',encoding='utf8')
            layer=new_caption();layer.update(show_start=.6,show_end=-.6,caption_timing='task',opacity=50.,
                font_min=100,font_max=100,outline_width=0,caption_cursor=False)
            layer=normalize_layers([layer])[0]
            state=self.settings(layer);state['settings']['use_voice']=True
            job=dict(id='converted',path='',name='caption',title='',body='WORD',voice_path=str(voice),subtitle_path=str(srt),status='待生成')
            direct=self.encode(root,state,job)
            registry=fx.fallback_registry()
            graph,effect=component_graph(layer,state['settings'],registry)
            # Exercise the caption operation's own window, independently of the outer control.
            effect.update(show_start=0.,show_end=0.)
            library=WorkflowLibrary(Path(root)/'workflows');record=library.save(graph,'Timed caption',registry)
            effect['workflow_id']=record['workflow_id'];state['settings']['layers']=normalize_layers(
                [effect]+[dict(x,enabled=False) for x in DEFAULT_LAYERS])
            state['jobs']=[job]
            bundle,registry,_=fx.prepare_bundle(state,[job['id']],SimpleNamespace(workflow_library=library,available_nodes=lambda:registry))
            converted=self.encode(root,state,job,fx.EffectRuntime(bundle,registry))
            self.assertLess(np.abs(direct[30].astype(int)-converted[30].astype(int)).mean(),1.)
            self.assertLess(converted[0].max(),10);self.assertLess(converted[60].max(),10)


if __name__=='__main__': unittest.main()
