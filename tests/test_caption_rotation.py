import copy
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave
import numpy as np
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video import caption_rotation as rotation,render_assignment as assignment,engine
from app_plugins.builtin.batch_text_video.captions import new_caption,design_layers
from app_plugins.builtin.batch_text_video.layers import normalize_layers,DEFAULT_LAYERS
from app_plugins.builtin.batch_text_video.components import resolve_sources,commit_sequences
from app_plugins.builtin.batch_text_video.pools import bind_pool_layers
from app_plugins.builtin.batch_text_video.store import Store
from app_plugins.builtin.batch_text_video.caption_rotation_ui import RotationEditor,StyleDialog
from app_plugins.builtin.batch_text_video.caption_ui import caption_property_panel
import test_cursor_pool as fixtures


class RotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):fixtures.CursorPoolTests.setUp(self)

    def state(self,cursor=False):
        state,_=fixtures.CursorPoolTests.state(self);layer=state['settings']['layers'][0]
        layer.update(caption_cursor=cursor,caption_preset='appear',caption_display_mode='group',caption_follow_mode='none')
        entries=[]
        for i,color in enumerate(('#ff0000','#0000ff')):
            style=rotation.snapshot(layer);style.update(color=color,color_mode='fixed',caption_cursor=cursor)
            if cursor:style['caption_display_mode']='char'
            entries.append(dict(id='style'+str(i),name='样式'+str(i+1),settings=style))
        layer.update(caption_rotation_enabled=True,caption_variants=entries)
        state['settings']['layers']=bind_pool_layers(normalize_layers(state['settings']['layers']),state)
        return state

    def test_old_caption_defaults_and_disabled_rotation_preserve_original(self):
        layer=normalize_layers([new_caption()])[0]
        self.assertEqual(layer['caption_variants'],[]);self.assertFalse(rotation.active(layer))
        self.assertIsNone(rotation.resolve(layer))
        state=self.state();layer=state['settings']['layers'][0];layer['caption_rotation_enabled']=False
        result,steps=resolve_sources([layer]);self.assertEqual(result[0]['color'],layer['color'])
        self.assertFalse(steps)

    def test_read_only_resolution_commits_once_and_wraps_independent_appearance(self):
        state=self.state();original=copy.deepcopy(state);layers=state['settings']['layers']
        for expected in ('#ff0000','#0000ff','#ff0000'):
            resolved,steps=resolve_sources(layers);self.assertEqual(resolved[0]['color'],expected)
            self.assertEqual(len(steps),1);next_layers=commit_sequences(layers,steps)
            with self.assertRaises(ValueError):commit_sequences(next_layers,steps)
            layers=next_layers
        self.assertEqual(state,original)

    def test_sources_geometry_times_stay_shared_opacity_multiplies(self):
        state=self.state();layer=state['settings']['layers'][0]
        layer.update(x=17,width=60,show_start=2,show_end=-1,caption_language='it',opacity=80)
        layer['caption_variants'][0]['settings']['opacity']=50
        result,steps=resolve_sources([layer]);value=result[0]
        self.assertEqual((value['x'],value['width'],value['show_start'],value['show_end'],value['caption_language'],value['opacity']),
                         (17,60,2,-1,'it',40))
        self.assertNotIn('x',layer['caption_variants'][0]['settings'])
        for change in (dict(enabled=False),dict(opacity=0)):
            self.assertFalse(resolve_sources([dict(layer,**change)])[1])

    def test_rejects_corrupt_variants_and_changed_configuration(self):
        layer=self.state()['settings']['layers'][0]
        for change in (dict(caption_rotation_enabled='yes'),dict(caption_variant_cursor=-1),dict(caption_variants={}),
                       dict(caption_variants=layer['caption_variants']*17)):
            with self.subTest(change=list(change)):
                with self.assertRaises(ValueError):normalize_layers([dict(layer,**change)])
        for field,value in (('source','voice'),('x',40),('caption_variants',[]),('_private',True),('font_min',float('nan'))):
            bad=copy.deepcopy(layer);bad['caption_variants'][0]['settings'][field]=value
            with self.assertRaises(ValueError):normalize_layers([bad])
        for key in ('id','name'):
            bad=copy.deepcopy(layer);bad['caption_variants'][1][key]=bad['caption_variants'][0][key]
            with self.assertRaises(ValueError):normalize_layers([bad])
        resolved,steps=resolve_sources([layer]);layer['caption_variants'][0]['settings']['color']='#112233'
        with self.assertRaises(ValueError):commit_sequences([layer],steps)

    def test_cursor_pool_advances_separately_for_each_style(self):
        state=self.state(cursor=True);layers=state['settings']['layers']
        for index in range(4):
            resolved,steps=resolve_sources(layers)
            self.assertEqual(resolved[0]['_caption_variant_id'],'style'+str(index%2))
            self.assertEqual(resolved[0]['caption_cursor_image'],self.paths[index//2])
            self.assertEqual(len(steps),2);layers=commit_sequences(layers,steps)
        self.assertEqual([e['settings']['caption_cursor_cursor'] for e in layers[0]['caption_variants']],[0,0])

    def test_retry_is_frozen_and_old_assignment_adds_only_new_styles(self):
        for legacy in (False,True):
            state=self.state(cursor=True);store=Store(self.root/str(legacy))
            job=dict(id='job',path='',name='job',title='',body='',status='待生成')
            if legacy:job['render_assignment']=dict(version=1,sources={},cursor_sources={'caption':'old.png'},music=None,background='',voice='')
            state['jobs']=[job];state=store.save(state,0)
            layers,steps=resolve_sources(state['settings']['layers']);frozen=assignment.capture(job,state,layers,None,'')
            prepared=SimpleNamespace(assignment=frozen,sequence_plan=steps,music=None,output=self.root/'out.mp4')
            saved,ok=assignment.reserve(store,state,'job',prepared);self.assertTrue(ok)
            self.assertEqual(saved['settings']['layers'][0]['caption_variant_cursor'],1)
            self.assertEqual(saved['settings']['layers'][0]['caption_variants'][0]['settings']['caption_cursor_cursor'],1)
            retry_job,retry_state=assignment.apply(saved['jobs'][0],saved)
            retry,steps=resolve_sources(retry_state['settings']['layers'])
            self.assertFalse(steps);self.assertEqual(retry[0]['color'],'#ff0000')
            self.assertEqual(retry[0]['caption_cursor_image'],self.paths[0])
            self.assertFalse(prepared.sequence_plan)

    def test_legacy_assignment_stale_cursor_removed_when_new_style_uses_bar(self):
        state=self.state();store=Store(self.root/'legacy')
        job=dict(id='job',path='',name='job',title='',body='',status='待生成',render_assignment=dict(version=1,
            sources={},cursor_sources={'caption':'old.png'},music=None,background='',voice=''))
        state['jobs']=[job];state=store.save(state,0);layers,steps=resolve_sources(state['settings']['layers'])
        prepared=SimpleNamespace(assignment=assignment.capture(job,state,layers,None,''),sequence_plan=steps,music=None,output=self.root/'out.mp4')
        saved,_=assignment.reserve(store,state,'job',prepared)
        self.assertNotIn('caption',saved['jobs'][0]['render_assignment']['cursor_sources'])

    def test_design_preview_uses_selected_style_without_consumption(self):
        layer=self.state()['settings']['layers'][0];layer['caption_variant_cursor']=1;before=copy.deepcopy(layer)
        value=design_layers([layer],'','One two three')[0]
        self.assertEqual(value['color'],'#0000ff');self.assertEqual(layer,before)

    def test_cross_project_clipboard_converts_nested_cursor_bindings(self):
        layer=self.state(cursor=True)['settings']['layers'][0];original=copy.deepcopy(layer)
        rotation.clipboard(layer)
        for index,entry in enumerate(layer['caption_variants']):
            self.assertEqual(entry['settings']['caption_cursor_source'],'sequence')
            self.assertFalse(entry['settings']['caption_cursor_pool_id'])
            self.assertNotEqual(entry['settings']['caption_cursor_items'][0]['id'],original['caption_variants'][index]['settings']['caption_cursor_items'][0]['id'])

    def test_node_styles_and_cursor_ledgers_advance_independently(self):
        from app_plugins.builtin.batch_text_video.project_runtime import ProjectServices
        from app_plugins.builtin.batch_text_video.workflow_component_source import commit_steps
        from app_plugins.builtin.batch_text_video.cursor_pool import resolve_node
        state=self.state(cursor=True);layer=state['settings']['layers'][0]
        prepared=rotation.prepare_node(layer,ProjectServices(None,state))
        owner=dict(id='effect',kind='node_effect',enabled=True,opacity=100,workflow_id='a'*32,source_rotations={})
        for index in range(4):
            context=SimpleNamespace(node_id='node',node_prepared=prepared,workflow_input=dict(effect_layer=owner))
            value=copy.deepcopy(layer);rotation.resolve_node(value,context);resolve_node(value,context)
            self.assertEqual(value['color'],('#ff0000','#0000ff')[index%2])
            self.assertEqual(value['caption_cursor_image'],self.paths[index//2])
            commit_steps([owner],list(context.component_source_steps.values()))

    def test_node_builder_serializes_only_selected_style_and_its_cursor(self):
        import json
        from model.node_workflow.contracts import ResourceRef
        from app_plugins.builtin.batch_text_video.project_runtime import ProjectServices
        from app_plugins.builtin.batch_text_video.workflow_caption import prepare_caption
        from app_plugins.builtin.batch_text_video.workflow_nodes import build_caption
        state=self.state(cursor=True);layer=state['settings']['layers'][0]
        prepared=prepare_caption(None,{'caption':layer},ProjectServices(None,state))
        subtitle=self.root/'node.srt';subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n',encoding='utf8')
        context=SimpleNamespace(node_id='node',node_prepared=prepared,workflow_input={})
        operation=build_caption({'subtitles':ResourceRef.capture(subtitle,'subtitles')},{'caption':layer},context)['operations'][0]
        selected=json.loads(dict(operation.options)['layer'])
        self.assertEqual(selected['color'],'#ff0000');self.assertFalse(selected['caption_variants'])
        self.assertFalse(selected['caption_rotation_enabled']);self.assertEqual(selected['caption_cursor_image'],self.paths[0])
        self.assertIn('cursor_image',dict(operation.assets))

    def panel(self):
        state=self.state();layer=state['settings']['layers'][0];panel=caption_property_panel()
        self.addCleanup(panel.deleteLater);panel.bind(layer,QtCore.QSize(1080,1920),state,'percent')
        return panel,state

    def test_mixed_old_loaded_caption_api_requests_restart(self):
        from app_plugins.builtin.batch_text_video import captions
        old={k:v for k,v in captions.DEFAULTS.items() if k not in rotation.DEFAULTS}
        with patch.object(captions,'DEFAULTS',old):
            with self.assertRaisesRegex(RuntimeError,'重启'):caption_property_panel()

    def test_manager_edit_duplicate_reorder_remove_and_cancel(self):
        panel,state=self.panel();manager=panel.rotation_editor;patches=[];panel.edited.connect(lambda _id,value:patches.append(value))
        self.assertFalse(panel.text_properties.isEnabled());self.assertTrue(panel.geometry_editor.isEnabled())
        self.assertTrue(panel.caption_properties.controls['caption_timing'].isEnabled())
        self.assertTrue(panel.caption_properties.controls['caption_cursor_color'].isEnabled()==False)
        with patch.object(manager,'dialog',return_value=None):manager.edit();manager.add()
        self.assertFalse(patches)
        changed=copy.deepcopy(manager.layer['caption_variants'][0]);changed['settings']['color']='#112233';changed['name']='Custom'
        with patch.object(manager,'dialog',return_value=changed):manager.edit()
        self.assertEqual(patches[-1]['caption_variants'][0]['settings']['color'],'#112233')
        manager.duplicate();self.assertEqual(manager.styles.count(),3)
        manager.move(1);self.assertEqual(manager.styles.currentIndex(),2)
        manager.remove();self.assertEqual(manager.styles.count(),2)
        manager.enable.setChecked(False);self.assertTrue(panel.text_properties.isEnabled())
        if os.environ.get('LZX_ROTATION_QA'):
            manager.enable.setChecked(True);panel.resize(560,960);panel.show();self.app.processEvents()
            panel.grab().save(str(Path(os.environ['LZX_ROTATION_QA'])/'rotation-list.png'))

    def test_style_dialog_uses_real_editors_preview_and_cancel_is_private(self):
        state=self.state();layer=state['settings']['layers'][0];before=copy.deepcopy(layer)
        dialog=StyleDialog(layer,layer['caption_variants'][0],state,QtCore.QSize(1080,1920))
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(hasattr(dialog.editor,'rotation_editor'))
        self.assertTrue(dialog.editor.geometry_editor.isHidden());self.assertTrue(dialog.editor.timing_editor.isHidden())
        self.assertTrue(dialog.editor.caption_properties.controls['caption_timing'].isHidden())
        dialog.edit(layer['id'],dict(color='#abcdef',font_min=64,font_max=64))
        dialog.refresh();self.assertFalse(dialog.preview.pixmap().isNull())
        self.assertEqual(dialog.note.text(),'');self.assertEqual(layer,before)
        if os.environ.get('LZX_ROTATION_QA'):
            dialog.show();self.app.processEvents();dialog.grab().save(str(Path(os.environ['LZX_ROTATION_QA'])/'rotation-editor.png'))
        dialog.accept();self.assertEqual(dialog.result_entry['settings']['color'],'#abcdef')
        self.assertNotIn('show_start',dialog.result_entry['settings'])

    def test_manager_changes_persist_and_undo(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        state=self.state();store=Store(self.root/'undo');store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'update_preview'):
            dialog=BatchTextVideoDialog(store=store)
            try:
                dialog.layer_panel.select_layer_id('caption')
                manager=dialog.layer_panel.inspector.active.rotation_editor
                manager.duplicate();self.assertTrue(dialog.persist())
                self.assertEqual(len(dialog.layer_panel.current()['caption_variants']),3)
                self.assertTrue(dialog.undo_edit());self.assertEqual(len(dialog.layer_panel.current()['caption_variants']),2)
                self.assertTrue(dialog.redo_edit());self.assertEqual(len(dialog.layer_panel.current()['caption_variants']),3)
            finally:
                dialog.cancel_work();dialog.deleteLater();QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    @unittest.skipUnless(fixtures.FFMPEG,'Explicit real FFmpeg required')
    def test_actual_exports_select_different_styles(self):
        state=self.state();voice=self.root/'voice.wav'
        with wave.open(str(voice),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(np.zeros(32000,np.int16).tobytes())
        srt=self.root/'voice.srt';srt.write_text('1\n00:00:00,000 --> 00:00:01,900\nWORD\n',encoding='utf8')
        state['settings'].update(ffmpeg_path=fixtures.FFMPEG,music_enabled=False,darkness=0,background_tint_strength=0,
            background_blur=0,output_dir=str(self.root),overwrite=True,video_encoder='cpu',use_voice=True,aspect='landscape')
        for index in range(2):
            job=dict(id=str(index),path='',name='style'+str(index),title='',body='WORD',voice_path=str(voice),subtitle_path=str(srt),status='待生成')
            state['jobs']=[job]
            with patch.object(engine,'output_size',return_value=(320,180)):
                with engine.prepare_static_text(job,state,self.root/'private') as prepared:
                    plan=copy.deepcopy(prepared.sequence_plan);prepared.encode();output=prepared.publish()
            for time in ('0.5','1.4'):
                run=subprocess.run([fixtures.FFMPEG,'-v','error','-ss',time,'-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],
                    capture_output=True,timeout=40,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(run.returncode,0,run.stderr.decode('utf8','replace'))
                pixels=np.frombuffer(run.stdout,np.uint8).reshape(180,320,3).astype(int);channel=0 if index==0 else 2;other=2 if index==0 else 0
                self.assertGreater(np.count_nonzero((pixels[:,:,channel]>180)&(pixels[:,:,other]<80)&(pixels[:,:,1]<80)),10)
            state['settings']['layers']=commit_sequences(state['settings']['layers'],plan)


if __name__=='__main__':unittest.main()
