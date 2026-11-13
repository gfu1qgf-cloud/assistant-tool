import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.store import Store,fresh_state,ProfileStore
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video import task_render_config as config
from app_plugins.builtin.batch_text_video.render_assignment import apply,reserve
from app_plugins.builtin.batch_text_video.playback import preview_key

FFMPEG=os.environ.get('NODE_FFMPEG','')


class TaskConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.red=self.image('red.png','#ff0000');self.blue=self.image('blue.png','#0000ff')
        self.state=fresh_state();self.state['settings'].update(music_enabled=False,use_voice=False)
        self.state['settings']['layers']=normalize_layers([dict(id='picture',kind='image',name='图片',path=str(self.red),source='fixed',enabled=True)])
        for layer in self.state['settings']['layers']:
            if layer['kind']!='image':layer['enabled']=False
        self.job=dict(id='j',name='sample',title='OLD TITLE',body='OLD BODY',path='',output='',error='',status='已完成',
            render_assignment=dict(version=1,sources={'picture':dict(field='path',value=str(self.red))},cursor_sources={},caption_styles={},music=None,background='',voice=''))
        self.state['jobs']=[self.job]
    def image(self,name,color):
        image=QtGui.QImage(160,90,QtGui.QImage.Format.Format_ARGB32);image.fill(QtGui.QColor(color))
        path=self.root/name;self.assertTrue(image.save(str(path)));return path
    def remembered(self):
        self.assertTrue(config.record(self.state,'j',config.capture(self.job,self.state)))
        return self.state['jobs'][0]
    def test_snapshot_deduplicates_settings_but_preserves_each_content(self):
        first=self.remembered();second=copy.deepcopy(first);second.update(id='other',body='OTHER')
        second.pop('render_config_id');self.state['jobs'].append(second)
        self.assertTrue(config.record(self.state,'other',config.capture(second,self.state)))
        self.assertEqual(len(self.state['render_configs']),1)
        self.assertEqual(first['render_config_id'],second['render_config_id'])
        self.assertNotEqual(first['render_content'],second['render_content'])
        self.assertFalse(config.record(self.state,'j',config.capture(first,self.state)))
    def test_bound_preview_freezes_settings_content_and_assignments_without_mutation(self):
        job=self.remembered();before=copy.deepcopy(self.state)
        self.state['settings']['layers'][0]['x']=4;job['body']='NEW BODY'
        frozen,state=config.bound(job,self.state)
        self.assertEqual(frozen['body'],'OLD BODY');self.assertEqual(state['settings']['layers'][0]['x'],50)
        self.assertEqual(state['settings']['layers'][0]['path'],str(self.red))
        self.assertEqual(self.state['settings']['layers'][0]['x'],4)
        ordinary,current=apply(job,self.state)
        self.assertEqual(ordinary['body'],'NEW BODY');self.assertEqual(current['settings']['layers'][0]['x'],4)
        self.assertEqual(before['jobs'][0]['render_assignment'],job['render_assignment'])
    def test_selective_replacement_preserves_all_other_frozen_sources(self):
        job=self.remembered();job['render_assignment']['sources']['another']={'field':'path','value':'original.mov'}
        # Remove unrepresented extra source only for preview validation.
        job['render_assignment']['sources'].pop('another')
        before=copy.deepcopy(job);edited=config.replace(job,self.state,'picture',self.blue)
        expected=copy.deepcopy(before['render_assignment']);expected['sources']['picture']['value']=str(self.blue)
        self.assertEqual(edited['render_assignment'],expected);self.assertEqual(job,before)
        self.assertTrue(edited['render_cache_token'])
    def test_background_and_cursor_replacement_are_independent(self):
        job=self.remembered();changed=config.replace(job,self.state,'@background',self.blue)
        self.assertEqual(changed['render_assignment']['background'],str(self.blue))
        self.assertEqual(changed['render_assignment']['sources'],job['render_assignment']['sources'])
        from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
        caption=new_follow_caption();caption.update(caption_cursor_mode='image',caption_cursor_image=str(self.red))
        self.state['settings']['layers'].append(caption);job.pop('render_config_id');config.record(self.state,'j',config.capture(job,self.state))
        changed=config.replace(job,self.state,'@cursor:'+caption['id'],self.blue)
        self.assertEqual(changed['render_assignment']['cursor_sources'][caption['id']],str(self.blue))
    def test_old_task_is_explicit_fallback_missing_record_is_not_silently_ignored(self):
        from app_plugins.builtin.batch_text_video.task_preview_ui import TaskPreviewDialog
        dialog=TaskPreviewDialog(self.job,self.state,self.root,autostart=False);self.addCleanup(dialog.deleteLater)
        self.assertIn('旧任务',dialog.notice.text())
        job=self.remembered();self.state['render_configs'].clear()
        with self.assertRaisesRegex(ValueError,'快照缺失'):config.bound(job,self.state)
    def test_replacement_validation_does_not_mutate_original(self):
        before=copy.deepcopy(self.state)
        with self.assertRaises(ValueError):config.replace(self.job,self.state,'picture',self.root/'missing.png')
        with self.assertRaises(ValueError):config.replace(self.job,self.state,'unknown',self.blue)
        old=copy.deepcopy(self.job);old.pop('render_assignment')
        with self.assertRaisesRegex(ValueError,'固定素材'):config.replace(old,self.state,'picture',self.blue)
        self.assertEqual(self.state,before)
    def test_stage_is_atomic_preserves_other_jobs_plans_music_and_original_output(self):
        job=self.remembered();self.state['jobs'].append(dict(id='other',name='other',path='',title='',body='',status='待生成'))
        self.state['render_plans']=[dict(id='pending',profile_id='default',name='默认',ids=['j','other'],known_ids=['j','other'],status='staged',task_id='',error='')]
        job['output']='old.mp4';edited=config.replace(job,self.state,'picture',self.blue);before=copy.deepcopy(self.state)
        result=config.stage(self.state,'default','j',edited,0)
        self.assertEqual(self.state,before);self.assertEqual(result['jobs'][0]['output'],'old.mp4')
        self.assertEqual(result['jobs'][0]['status'],'待生成');self.assertTrue(result['jobs'][0]['render_from_snapshot'])
        self.assertEqual(result['render_plans'][0]['ids'],['other']);self.assertEqual(result['render_plans'][1]['ids'],['j'])
        self.assertEqual(result['music'],before['music']);self.assertEqual(result['settings'],before['settings'])
    def test_running_project_stale_revision_and_missing_job_are_rejected(self):
        self.remembered();before=copy.deepcopy(self.state)
        with self.assertRaisesRegex(ValueError,'项目已修改'):config.stage(self.state,'default','j',self.job,88)
        with self.assertRaisesRegex(ValueError,'已删除'):config.stage(self.state,'default','missing',self.job,0)
        self.state['render_plans']=[dict(profile_id='default',status='running')]
        with self.assertRaisesRegex(ValueError,'正在渲染'):config.stage(self.state,'default','j',self.job,0)
        self.assertEqual(self.state['jobs'],before['jobs'])
    def test_inactive_profile_keeps_active_editor_and_snapshot_survives_reload(self):
        store=Store(self.root/'private');self.remembered();state=store.save(self.state,0)
        state=store.create_profile(state,'Other',clone=True);pid=state['active_profile']
        state=store.select_profile(state,'default');other=ProfileStore(store.directory,pid).load()
        edited=config.replace(other['jobs'][0],other,'picture',self.blue)
        expected=next(p for p in state['profiles'] if p['id']==pid).get('revision',0)
        state=store.stage_task_rerender(pid,'j',edited,expected_profile_revision=expected)
        self.assertEqual(state['active_profile'],'default');self.assertEqual(state['jobs'][0]['render_assignment']['sources']['picture']['value'],str(self.red))
        loaded=ProfileStore(store.directory,pid).load();job,view=config.bound(loaded['jobs'][0],loaded)
        self.assertEqual(view['settings']['layers'][0]['path'],str(self.blue))
    def test_cache_key_changes_for_forced_rerender_and_source_change(self):
        self.remembered();first=preview_key(self.job,self.state)
        changed=config.replace(self.job,self.state,'picture',self.blue)
        self.assertNotEqual(first,preview_key(changed,self.state))
        changed=copy.deepcopy(self.job);changed['render_cache_token']='force-refresh'
        self.assertNotEqual(first,preview_key(changed,self.state))
    def test_source_protection_includes_frozen_replacements(self):
        from app_plugins.builtin.batch_text_video.engine import source_paths,safe_output
        self.job['render_assignment']['sources']['picture']['value']=str(self.blue)
        self.assertIn(str(self.blue),source_paths(self.state))
        self.job['render_assignment']['background']=str(self.root/'source.mp4')
        with self.assertRaisesRegex(ValueError,'覆盖源文件'):safe_output(self.root,'source',source_paths(self.state))
    def test_voice_preflight_uses_frozen_voice_without_copying_unrelated_projects(self):
        from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
        from app_plugins.builtin.batch_text_video.export_validation import validate_caption_voice
        self.state['settings']['layers'].append(new_follow_caption())
        class Unrelated:
            def __deepcopy__(self,memo):raise AssertionError('preflight copied unrelated project')
        self.state['unrelated_project']=Unrelated()
        self.job['render_assignment']['voice']=str(self.red)
        validate_caption_voice(self.state,[self.job])
        self.job['render_assignment']['voice']=''
        with self.assertRaisesRegex(ValueError,'动态字幕需要人声'):validate_caption_voice(self.state,[self.job])
    def test_output_policy_remains_live_while_snapshot_visual_settings_are_frozen(self):
        job=self.remembered();self.state['settings'].update(overwrite=True,output_dir='new-output',darkness=70)
        frozen,view=config.bound(job,self.state)
        self.assertTrue(view['settings']['overwrite']);self.assertEqual(view['settings']['output_dir'],'new-output')
        self.assertNotEqual(view['settings']['darkness'],70)
    def test_reservation_records_config_in_same_native_save(self):
        store=Store(self.root/'private');state=store.save(self.state,0);writes=[]
        prepared=SimpleNamespace(assignment=copy.deepcopy(self.job['render_assignment']),render_config=config.capture(self.job,state),music=None,sequence_plan=[])
        original=store.save
        def save(value,revision):writes.append(1);return original(value,revision)
        with patch.object(store,'save',side_effect=save):state,reserved=reserve(store,state,'j',prepared)
        self.assertTrue(reserved);self.assertEqual(len(writes),1);self.assertTrue(state['jobs'][0].get('render_config_id'))
    def test_snapshot_registry_prunes_deleted_jobs_without_changing_live_settings(self):
        self.remembered();self.state['jobs']=[]
        config.normalize(self.state);self.assertEqual(self.state['render_configs'],{})
    def test_doubleclick_opens_configuration_preview_instead_of_result_file(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        store=Store(self.root/'private');store.save(self.state,0)
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=store);dialog.open_global_queue();self.app.processEvents()
            try:
                panel=dialog.render_queue_panel.flat;panel.table.setCurrentCell(0,0)
                with patch('app_plugins.builtin.batch_text_video.task_preview_ui.open_task') as opened,patch.object(panel,'open_result') as old:
                    panel.table.itemDoubleClicked.emit(panel.table.item(0,0))
                    opened.assert_called_once_with(panel,('default','j'),rerender=False);old.assert_not_called()
            finally:dialog.close();dialog.deleteLater();self.app.processEvents()
    def test_dialog_replacement_preview_and_doubleclick_do_not_write_store(self):
        from app_plugins.builtin.batch_text_video.task_preview_ui import TaskPreviewDialog
        self.remembered();before=copy.deepcopy(self.state);submits=[]
        dialog=TaskPreviewDialog(self.job,self.state,self.root,submit=submits.append,autostart=False)
        self.addCleanup(dialog.deleteLater)
        dialog.items.selectRow(next(i for i,v in enumerate(dialog.entries) if v['id']=='picture'))
        with patch.object(dialog,'refresh'):dialog.replace_media(self.blue)
        self.assertEqual(self.state,before);self.assertEqual(submits,[])
        dialog.rerender.click();self.assertEqual(len(submits),1);self.assertFalse(dialog.rerender.isEnabled())
    def test_stale_playback_after_replacement_releases_cache_instead_of_showing_old_picture(self):
        from app_plugins.builtin.batch_text_video.task_preview_ui import TaskPreviewDialog
        dialog=TaskPreviewDialog(self.job,self.state,self.root,autostart=False);self.addCleanup(dialog.deleteLater)
        released=[];dialog.pending=True
        dialog.play_ready(dict(cache_owner=SimpleNamespace(close=lambda:released.append(True)),path='stale.mp4',duration=1))
        self.assertEqual(released,[True]);self.assertIsNone(dialog.surface)
    def test_scrubbing_design_timeline_pauses_playback_and_returns_to_canvas(self):
        from app_plugins.builtin.batch_text_video.task_preview_ui import TaskPreviewDialog
        dialog=TaskPreviewDialog(self.job,self.state,self.root,autostart=False);self.addCleanup(dialog.deleteLater)
        paused=[];dialog.surface=SimpleNamespace(pause=lambda:paused.append(True))
        dialog.timeline_changed();dialog.debounce.stop()
        self.assertEqual(paused,[True]);self.assertIs(dialog.stack.currentWidget(),dialog.preview)
        dialog.surface=None
    def test_actual_preview_workers_complete_and_close_without_saving(self):
        if not FFMPEG:self.skipTest('explicit FFmpeg required')
        from app_plugins.builtin.batch_text_video.task_preview_ui import TaskPreviewDialog
        self.remembered();self.state['settings'].update(ffmpeg_path=FFMPEG)
        before=copy.deepcopy(self.state);dialog=TaskPreviewDialog(self.job,self.state,self.root,autostart=False)
        self.addCleanup(dialog.deleteLater);dialog.refresh();end=time.monotonic()+30
        while dialog.workers() and time.monotonic()<end:self.app.processEvents();time.sleep(.01)
        self.app.processEvents();self.assertFalse(dialog.workers(),dialog.status.text())
        self.assertFalse(dialog.preview.overlay.isNull(),dialog.status.text());self.assertEqual(self.state,before)
        if os.environ.get('LZX_TASK_PREVIEW_QA'):
            dialog.show();self.app.processEvents();dialog.grab().save(str(Path(os.environ['LZX_TASK_PREVIEW_QA'])/'task-preview.png'))
        dialog.close()

    @unittest.skipUnless(FFMPEG,'explicit FFmpeg required')
    def test_real_rerender_changes_only_picture_preserves_audio_geometry_and_rotation(self):
        from app_plugins.builtin.batch_text_video.background import execute_batch
        from app_plugins.builtin.batch_text_video.engine import probe
        def ff(args):
            result=subprocess.run([FFMPEG,'-v','error','-y',*args],capture_output=True,timeout=50,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'));return result.stdout
        voice=self.root/'voice.wav';song=self.root/'song.wav'
        ff(['-f','lavfi','-i','sine=frequency=440:duration=0.8',str(voice)])
        ff(['-f','lavfi','-i','sine=frequency=880:duration=4',str(song)])
        self.job.pop('render_assignment');self.job.update(voice_path=str(voice),voice_override=True,title='',body='',status='待生成')
        self.state['settings'].update(ffmpeg_path=FFMPEG,aspect='custom',canvas_width=320,canvas_height=180,
            video_encoder='cpu',export_workers=2,encoder_fallback=False,output_dir=str(self.root/'out'),use_voice=True,
            output_destination='directory',music_enabled=True,skip_silence=False,auto_crop_black_bars=False,overwrite=False,darkness=0)
        self.state['music']=[dict(id='song',path=str(song),cursor=0.,audio_key='')]
        store=Store(self.root/'private');state=store.save(self.state,0)
        def batch(state):
            logs=[];result,value=execute_batch(store,state,['j'],threading.Event(),lambda *_:None,logs.append,lambda *_:None)
            self.assertEqual((result['completed'],result['failed']),(1,0),(result,logs[-4:]));return value,Path(result['paths'][0])
        state,first=batch(state);original=copy.deepcopy(state['jobs'][0]['render_assignment']);cursor=state['music'][0]['cursor']
        state['settings']['layers'][0]['x']=5;state['jobs'][0]['body']='SHOULD NOT APPEAR'
        state=store.save(state,state['revision']);edited=config.replace(state['jobs'][0],state,'picture',self.blue)
        revision=next(p for p in state['profiles'] if p['id']=='default')['revision']
        state=store.stage_task_rerender('default','j',edited,expected_profile_revision=revision)
        state,second=batch(state)
        self.assertNotIn('render_from_snapshot',state['jobs'][0])
        self.assertNotEqual(first,second);self.assertTrue(first.exists());self.assertEqual(state['music'][0]['cursor'],cursor)
        new=state['jobs'][0]['render_assignment'];self.assertEqual(new['voice'],original['voice']);self.assertEqual(new['music'],original['music'])
        for file,color in ((first,'red'),(second,'blue')):
            raw=ff(['-ss','0.4','-i',str(file),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'])
            import numpy as np
            image=np.frombuffer(raw,np.uint8).reshape(180,320,3);pixel=image[90,160]
            self.assertGreater(int(pixel[0 if color=='red' else 2]),220,pixel)
        audio=lambda p:ff(['-i',str(p),'-vn','-f','s16le','-c:a','pcm_s16le','pipe:1'])
        self.assertEqual(audio(first),audio(second))
