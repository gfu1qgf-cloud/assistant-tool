import copy
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video import cursor_pool as cp,render_assignment as assignment
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
from app_plugins.builtin.batch_text_video.layers import normalize_layers,DEFAULT_LAYERS
from app_plugins.builtin.batch_text_video.pools import bind_pool_layers
from app_plugins.builtin.batch_text_video.components import resolve_sources,commit_sequences
from app_plugins.builtin.batch_text_video.custom_pools import create_pool
from app_plugins.builtin.batch_text_video.store import Store,fresh_state
from app_plugins.builtin.batch_text_video.pool_preview import PoolPreview
from app_plugins.builtin.batch_text_video import engine
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog

FFMPEG=os.environ.get('NODE_FFMPEG','')


class CursorPoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.paths=[]
        for color in ('red','blue'):
            path=self.root/(color+'.png');image=QtGui.QImage(24,32,QtGui.QImage.Format.Format_ARGB32)
            image.fill(QtGui.QColor(color));image.save(str(path));self.paths.append(str(path))

    def state(self):
        state=fresh_state();pool=create_pool(state,'Cursor pictures','visual')
        pool['items']=[dict(id=str(i),path=path,name=Path(path).name) for i,path in enumerate(self.paths)]
        layer=new_follow_caption();layer.update(id='caption',caption_display_mode='char',caption_cursor=True,
            caption_cursor_mode='image',caption_cursor_source='pool',caption_cursor_pool_id=pool['id'],
            font='Arial',font_min=70,font_max=70,outline_width=0,shadow_enabled=False,background_enabled=False,
            x=50,y=50,width=85,height=50,caption_timing='task')
        layer['caption_effects'].update(anim_in_type='none',anim_out_type='none',karaoke_highlight=False)
        state['settings']['layers']=bind_pool_layers(normalize_layers([layer]+[dict(v,enabled=False) for v in DEFAULT_LAYERS]),state)
        return state,pool['id']

    def test_rotation_is_read_only_and_commits_once_independent_of_subtitle_source(self):
        state,_=self.state();before=copy.deepcopy(state)
        layers,plan=resolve_sources(state['settings']['layers'])
        self.assertEqual(layers[0]['caption_cursor_image'],self.paths[0]);self.assertEqual(state,before)
        committed=commit_sequences(state['settings']['layers'],plan)
        self.assertEqual(committed[0]['caption_cursor_cursor'],1)
        self.assertEqual(resolve_sources(committed)[0][0]['caption_cursor_image'],self.paths[1])
        with self.assertRaises(ValueError):commit_sequences(committed,plan)

    def test_subtitle_pool_and_cursor_pool_can_advance_on_same_caption(self):
        state,_=self.state();pool=create_pool(state,'Subtitles','subtitle')
        pool['items']=[dict(id='s'+str(i),path=str(self.root/(str(i)+'.srt'))) for i in range(2)]
        layer=state['settings']['layers'][0];layer.update(source='pool',pool_id=pool['id'],caption_timing='task')
        state['settings']['layers']=bind_pool_layers(state['settings']['layers'],state)
        layers,plan=resolve_sources(state['settings']['layers'])
        self.assertEqual(len(plan),2)
        committed=commit_sequences(state['settings']['layers'],plan)
        self.assertEqual((committed[0]['sequence_cursor'],committed[0]['caption_cursor_cursor']),(1,1))

    def test_fixed_and_disabled_cursor_dont_consume_and_mixed_pool_filters_video(self):
        state,pid=self.state();state['resource_pools'][0]['items'].append(dict(id='video',path='ignored.mp4'))
        state['settings']['layers']=bind_pool_layers(state['settings']['layers'],state)
        self.assertEqual(len(state['settings']['layers'][0]['caption_cursor_items']),2)
        for changes in (dict(caption_cursor=False),dict(caption_cursor_mode='bar'),dict(enabled=False),
                        dict(caption_cursor_source='file',caption_cursor_image=self.paths[0])):
            layer=dict(state['settings']['layers'][0],**changes)
            self.assertFalse(resolve_sources([layer])[1])
        state['settings']['layers'][0]['caption_cursor_items']=[]
        with self.assertRaisesRegex(ValueError,'没有可用图片'):resolve_sources(state['settings']['layers'])

    def test_assignment_freezes_retry_and_legacy_assignment_gets_cursor_only_reservation(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy):
                state,_=self.state();store=Store(self.root/str(legacy))
                job=dict(id='job',path='',name='job',title='',body='',status='待生成')
                if legacy:job['render_assignment']=dict(version=1,sources={},music=None,background='',voice='')
                state['jobs']=[job];state=store.save(state,0)
                resolved,plan=resolve_sources(state['settings']['layers'])
                value=assignment.capture(job,state,resolved,None,'')
                prepared=SimpleNamespace(assignment=value,sequence_plan=plan,music=None,output=self.root/'out.mp4')
                saved,ok=assignment.reserve(store,state,'job',prepared)
                self.assertTrue(ok);self.assertEqual(saved['settings']['layers'][0]['caption_cursor_cursor'],1)
                assigned=saved['jobs'][0];self.assertEqual(assigned['render_assignment']['cursor_sources']['caption'],self.paths[0])
                retry_job,retry_state=assignment.apply(assigned,saved)
                retry,steps=resolve_sources(retry_state['settings']['layers'])
                self.assertEqual(retry[0]['caption_cursor_image'],self.paths[0]);self.assertFalse(steps)
                self.assertFalse(prepared.sequence_plan)

    def test_preview_switches_images_and_assigned_playback_without_persisting_cursors(self):
        state,_=self.state();before=copy.deepcopy(state)
        widget=PoolPreview();self.addCleanup(widget.deleteLater)
        value=dict(version=1,sources={},cursor_sources={'caption':self.paths[0]},music=None,background='',voice='')
        widget.sync(state['settings']['layers'],'caption:cursor',scope='p',assignment=value)
        widget.step(1)
        trial=widget.apply(state['settings']);self.assertEqual(trial['layers'][0]['caption_cursor_cursor'],1)
        job,trial_state=widget.prepare(dict(render_assignment=value),state)
        self.assertEqual(resolve_sources(trial_state['settings']['layers'])[0][0]['caption_cursor_image'],self.paths[1])
        self.assertEqual(state,before);self.assertEqual(value['cursor_sources']['caption'],self.paths[0])
        widget.restore();self.assertFalse(widget.choices)

    def test_order_changes_and_cross_project_component_copy_keep_independent_rotation(self):
        from app_plugins.builtin.batch_text_video.pool_order import restart_layers
        from app_plugins.builtin.batch_text_video import layer_clipboard as clipboard
        state,pid=self.state();state['resource_pools'][0]['items'].reverse()
        bound=bind_pool_layers(state['settings']['layers'],state)
        self.assertEqual(bound[0]['caption_cursor_cursor'],1)
        restarted=bind_pool_layers(restart_layers(bound,'pool:'+pid),state)
        self.assertEqual(restarted[0]['caption_cursor_cursor'],0)
        payload=clipboard.capture(bound[0],state,'scope')
        result=clipboard.prepare([],{},payload,profile='target',scope='scope')
        copied=next(x for x in result['layers'] if x['id']==result['selected'])
        self.assertEqual(copied['caption_cursor_source'],'sequence')
        self.assertEqual(resolve_sources([copied])[0][0]['caption_cursor_image'],self.paths[0])

    def test_ui_source_selector_trial_and_undo_save(self):
        state,pid=self.state();store=Store(self.root/'private');store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=store)
            try:
                panel=dialog.layer_panel;panel.select_layer_id('caption');editor=panel.caption_properties
                self.assertEqual(editor.controls['caption_cursor_source'].currentData(),'pool')
                self.assertEqual(editor.controls['caption_cursor_pool_id'].currentData(),pid)
                dialog._sync_preview_layers();self.assertIs(dialog.pool_preview.parentWidget(),editor.cursor_pool_editor)
                before=panel.current()['caption_cursor_cursor'];dialog.pool_preview.step(1)
                self.assertEqual(panel.current()['caption_cursor_cursor'],before)
                dialog.pool_preview.restore()
                editor.controls['caption_cursor_source'].setCurrentIndex(editor.controls['caption_cursor_source'].findData('file'))
                self.assertTrue(dialog.persist());self.assertTrue(dialog.undo_edit())
                self.assertEqual(panel.current()['caption_cursor_source'],'pool')
                if os.environ.get('LZX_CURSOR_POOL_QA'):
                    editor.resize(600,1200);editor.show();self.app.processEvents()
                    editor.grab().save(str(Path(os.environ['LZX_CURSOR_POOL_QA'])/'cursor-pool-ui.png'))
            finally:
                dialog.cancel_work();dialog.deleteLater();QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_node_uses_resource_snapshot_and_existing_effect_rotation_ledger(self):
        from app_plugins.builtin.batch_text_video.project_runtime import ProjectServices
        from app_plugins.builtin.batch_text_video.workflow_component_source import commit_steps
        state,_=self.state();layer=state['settings']['layers'][0]
        prepared=cp.prepare_node(layer,ProjectServices(None,state))
        owner=dict(id='effect',kind='node_effect',enabled=True,opacity=100,workflow_id='a'*32,source_rotations={})
        for index,path in enumerate(self.paths):
            context=SimpleNamespace(node_id='caption_node',node_prepared=prepared,workflow_input=dict(effect_layer=owner))
            value=copy.deepcopy(layer);cp.resolve_node(value,context)
            self.assertEqual(value['caption_cursor_image'],path)
            commit_steps([owner],list(context.component_source_steps.values()))

    @unittest.skipUnless(FFMPEG,'Explicit real FFmpeg required')
    def test_two_actual_exports_use_different_cursor_images_and_keep_motion(self):
        state,_=self.state();voice=self.root/'voice.wav'
        with wave.open(str(voice),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(np.zeros(32000,np.int16).tobytes())
        srt=self.root/'voice.srt';srt.write_text('1\n00:00:00,000 --> 00:00:01,900\nWORD\n',encoding='utf8')
        state['settings'].update(ffmpeg_path=FFMPEG,music_enabled=False,darkness=0,background_tint_strength=0,
            background_blur=0,output_dir=str(self.root),overwrite=True,video_encoder='cpu',use_voice=True,aspect='landscape')
        for index in range(2):
            job=dict(id=str(index),path='',name='cursor'+str(index),title='',body='WORD',voice_path=str(voice),subtitle_path=str(srt),status='待生成')
            state['jobs']=[job]
            with patch.object(engine,'output_size',return_value=(320,180)):
                with engine.prepare_static_text(job,state,self.root/'private') as prepared:
                    plan=copy.deepcopy(prepared.sequence_plan)
                    prepared.encode();output=prepared.publish()
            run=subprocess.run([FFMPEG,'-v','error','-ss','0.8','-i',str(output),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],
                capture_output=True,timeout=40,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(run.returncode,0,run.stderr.decode('utf8','replace'))
            pixels=np.frombuffer(run.stdout,np.uint8).reshape(180,320,3).astype(int)
            channel=0 if index==0 else 2;other=2 if index==0 else 0
            self.assertGreater(np.count_nonzero((pixels[:,:,channel]>220)&(pixels[:,:,other]<60)&(pixels[:,:,1]<60)),10)
            self.assertLess(np.count_nonzero((pixels[:,:,other]>220)&(pixels[:,:,channel]<60)&(pixels[:,:,1]<60)),5)
            state['settings']['layers']=commit_sequences(state['settings']['layers'],plan)


if __name__=='__main__':unittest.main()
