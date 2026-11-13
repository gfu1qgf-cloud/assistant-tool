import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video import asset_replace as assets
from app_plugins.builtin.batch_text_video.store import Store,fresh_state,ProfileStore
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.task_render_config import capture,record,bound

FFMPEG=os.environ.get('NODE_FFMPEG','')


class AssetReplaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.red=self.image('red.png','red');self.blue=self.image('blue.png','blue');self.other=self.image('other.png','white')
        self.state=fresh_state();self.state['settings'].update(use_voice=False,music_enabled=False)
        self.state['settings']['layers']=normalize_layers([dict(id='picture',kind='image',name='Picture',path=str(self.red),source='fixed',enabled=True,matting_enabled=False)])
        for layer in self.state['settings']['layers']:
            if layer['kind']!='image':layer['enabled']=False
        self.state['image_pool']=[dict(id='asset',path=str(self.red),name='red.png',thumbnail='old-private-thumbnail.png')]
        self.state['jobs']=[self.job('used',self.red),self.job('unused',self.other)]
        for job in self.state['jobs']:record(self.state,job['id'],capture(job,self.state))
    def image(self,name,color):
        path=self.root/name;image=QtGui.QImage(160,90,QtGui.QImage.Format.Format_ARGB32);image.fill(QtGui.QColor(color));self.assertTrue(image.save(str(path)));return path
    def job(self,identifier,path):
        return dict(id=identifier,name=identifier,title='Title',body='Body',path='',status='已完成',output=str(self.root/(identifier+'.mp4')),error='',render_checked=False,
            render_timing=dict(elapsed_seconds=123),render_assignment=dict(version=1,sources={'picture':dict(field='path',value=str(path))},
                cursor_sources={},caption_styles={},background='',voice='',music=None))
    def replace(self,state=None,key='image_pool',identifier='asset',path=None):
        return assets.replace_project(state or self.state,key,identifier,path or self.blue)
    def test_only_effective_users_are_reset_and_checked(self):
        before=copy.deepcopy(self.state);value,info=self.replace()
        self.assertEqual(info['affected_ids'],['used']);self.assertEqual(self.state,before)
        job=value['jobs'][0];self.assertEqual(job['status'],'待生成');self.assertTrue(job['render_checked']);self.assertTrue(job['render_dirty'])
        self.assertTrue(job['render_from_snapshot']);self.assertNotIn('render_timing',job);self.assertTrue(job['render_cache_token'])
        self.assertEqual(value['jobs'][1],before['jobs'][1])
        self.assertEqual(job['output'],before['jobs'][0]['output'])
    def test_frozen_config_is_split_without_changing_other_tasks_shared_snapshot(self):
        old=self.state['jobs'][0]['render_config_id'];self.assertEqual(old,self.state['jobs'][1]['render_config_id'])
        value,_=self.replace();self.assertEqual(value['jobs'][1]['render_config_id'],old)
        self.assertEqual(value['render_configs'][old],self.state['render_configs'][old])
        job,state=bound(value['jobs'][0],value);self.assertEqual(state['settings']['layers'][0]['path'],str(self.blue))
        job,state=bound(value['jobs'][1],value);self.assertEqual(state['settings']['layers'][0]['path'],str(self.other))
    def test_all_visual_libraries_update_but_voice_music_and_other_projects_do_not(self):
        self.state['backgrounds']=[str(self.red)]
        self.state['resource_pools']=[dict(id='v',kind='visual',name='视觉',items=[dict(id='v1',path=str(self.red))]),
            dict(id='voice',kind='voice',name='人声',items=[dict(id='a',path=str(self.red))])]
        self.state['music']=[dict(id='music',path=str(self.red),cursor=21)]
        value,info=self.replace();self.assertEqual(info['updated_records'],3)
        self.assertEqual(value['backgrounds'],[str(self.blue)]);self.assertEqual(value['resource_pools'][0]['items'][0]['path'],str(self.blue))
        self.assertEqual(value['resource_pools'][1],self.state['resource_pools'][1]);self.assertEqual(value['music'],self.state['music'])
    def test_same_original_aliases_match_without_comparing_contents(self):
        self.state['jobs'][0]['render_assignment']['sources']['picture']['value']=str(self.root/'missing'/'..'/'red.png')
        value,info=self.replace();self.assertEqual(info['affected_ids'],['used'])
        self.assertEqual(value['jobs'][0]['render_assignment']['sources']['picture']['value'],str(self.blue))
    def test_other_frozen_content_voice_music_and_cursor_are_preserved(self):
        original=self.state['jobs'][0]['render_assignment']
        original.update(voice='voice.wav',music=dict(item=dict(id='m',path='music.wav',cursor=18,audio_key='key'),settings=dict(skip_silence=False,silence_db=-50,silence_seconds=.6)))
        original['sources']['body']={'field':'text','value':'fixed pool text'}
        original['cursor_sources']={'caption':'unchanged-cursor.png'}
        before=copy.deepcopy(original);value,_=self.replace();after=value['jobs'][0]['render_assignment']
        expected=copy.deepcopy(before);expected['sources']['picture']['value']=str(self.blue)
        self.assertEqual(after,expected)
    def test_background_usage_and_original_random_order_are_updated(self):
        self.state['backgrounds']=[str(self.red),str(self.other)];self.state['resource_orders']={'backgrounds':[str(self.other),str(self.red)]}
        self.state['jobs'][0]['render_assignment']['background']=str(self.red)
        self.state['jobs'][0]['render_assignment']['sources']['picture']['value']=str(self.other)
        value,info=self.replace(key='backgrounds',identifier=str(self.red))
        self.assertEqual(info['affected_ids'],['used']);self.assertEqual(value['resource_orders']['backgrounds'],[str(self.other),str(self.blue)])
        self.assertEqual(value['jobs'][0]['render_assignment']['background'],str(self.blue))
    def test_repairing_replaced_background_updates_existing_task_instead_of_duplicating(self):
        from app_plugins.builtin.batch_text_video.pools import apply_pairs,pair_identity
        job=self.state['jobs'][0];job.update(path=str(self.red),copy_id='copy',pair_key=list(pair_identity('copy',self.red)))
        job['render_assignment']['background']=str(self.red)
        self.state['backgrounds']=[str(self.red)]
        value,_=self.replace(key='backgrounds',identifier=str(self.red))
        added,updated=apply_pairs(value,[dict(copy=dict(id='copy',title='Title',body='Body'),path=str(self.blue))])
        self.assertEqual(added,0);self.assertEqual(len(value['jobs']),2)
        self.assertEqual(value['jobs'][0]['id'],'used')
    def test_library_ids_usage_order_and_layer_geometry_are_preserved(self):
        self.state['settings']['layers'][0]['sequence_cursor']=7
        self.state['resource_orders']={'image_pool':['asset']}
        value,_=self.replace();self.assertEqual(value['image_pool'][0]['id'],'asset');self.assertNotIn('thumbnail',value['image_pool'][0])
        self.assertEqual(value['resource_orders'],self.state['resource_orders'])
        for key in ('x','y','width','height','sequence_cursor'):self.assertEqual(value['settings']['layers'][0][key],self.state['settings']['layers'][0][key])
    def test_disabled_unassigned_visual_is_not_a_render_dependency(self):
        self.state['settings']['layers'][0]['enabled']=False
        for job in self.state['jobs']:
            job.pop('render_config_id');job.pop('render_assignment')
        value,info=self.replace();self.assertFalse(info['affected_ids']);self.assertTrue(all(v['status']=='已完成' for v in value['jobs']))
    def test_legacy_and_pending_jobs_work_without_historical_assignment(self):
        job=self.state['jobs'][0];job.pop('render_config_id');job.pop('render_assignment')
        value,info=self.replace();self.assertEqual(info['legacy_ids'],['used']);self.assertTrue(value['jobs'][0]['render_dirty'])
        self.assertNotIn('render_from_snapshot',value['jobs'][0]);self.assertFalse(value['jobs'][1].get('render_dirty'))
    def test_old_assignment_without_config_keeps_binding_and_records_current_settings(self):
        self.state['jobs'][0].pop('render_config_id')
        value,info=self.replace();self.assertEqual(info['legacy_ids'],['used'])
        self.assertIn(value['jobs'][0]['render_config_id'],value['render_configs'])
        self.assertEqual(value['jobs'][0]['render_assignment']['sources']['picture']['value'],str(self.blue))
    def test_cursor_reference_updates_frozen_style_and_rejects_video(self):
        from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
        layer=new_follow_caption();layer.update(caption_cursor_mode='image',caption_cursor=True,caption_display_mode='char',caption_cursor_image=str(self.red))
        self.state['settings']['layers'].append(layer)
        for job in self.state['jobs']:job.pop('render_config_id')
        value,info=self.replace();self.assertEqual(set(info['affected_ids']),{'used','unused'})
        job,bound_state=bound(value['jobs'][1],value)
        caption=next(v for v in bound_state['settings']['layers'] if v['id']==layer['id'])
        self.assertEqual(caption['caption_cursor_image'],str(self.blue))
        video=self.root/'placeholder.mov';video.write_bytes(b'video')
        before=copy.deepcopy(self.state)
        with self.assertRaisesRegex(ValueError,'图片光标'):self.replace(path=video)
        self.assertEqual(self.state,before)
    def test_unknown_missing_duplicate_and_stale_sources_roll_back(self):
        before=copy.deepcopy(self.state)
        for key,identifier,path in [('music','asset',self.blue),('image_pool','unknown',self.blue),('image_pool','asset',self.root/'missing.png')]:
            with self.assertRaises(ValueError):self.replace(key=key,identifier=identifier,path=path)
        with self.assertRaisesRegex(ValueError,'其他窗口'):assets.replace_project(self.state,'image_pool','asset',self.blue,expected_source=str(self.other))
        self.assertEqual(self.state,before)
        self.state['image_pool'].append(dict(id='existing',path=str(self.blue)))
        with self.assertRaisesRegex(ValueError,'同一素材池'):self.replace()
    def test_missing_original_is_replaceable_and_same_path_can_force_refresh(self):
        self.red.unlink();value,info=self.replace();self.assertEqual(info['affected_ids'],['used'])
        self.assertEqual(value['image_pool'][0]['path'],str(self.blue))
        value,info=assets.replace_project(value,'image_pool','asset',self.blue)
        self.assertEqual(info['affected_ids'],['used']);self.assertTrue(value['jobs'][0]['render_cache_token'])
    def test_per_file_loop_and_trim_rules_are_not_rebound_to_new_media(self):
        rule=dict(path=str(self.red),start=1.,end=2.)
        self.state['settings']['layers'][0]['media_loop_regions']=[rule]
        self.state['settings']['background_trims']=[rule]
        value,_=self.replace()
        self.assertEqual(value['settings']['layers'][0]['media_loop_regions'],[rule]);self.assertEqual(value['settings']['background_trims'],[rule])
    def test_atomic_store_save_rechecks_revision_running_plan_and_preserves_other_profile(self):
        store=Store(self.root/'private');state=store.save(self.state,0);state=store.create_profile(state,'其他',clone=True)
        other=state['active_profile'];state=store.select_profile(state,'default')
        other_before=copy.deepcopy(next(p for p in state['profiles'] if p['id']==other))
        revision=next(p for p in state['profiles'] if p['id']=='default')['revision'];before=copy.deepcopy(state)
        with self.assertRaisesRegex(ValueError,'其他窗口'):store.replace_visual_asset('default','image_pool','asset',self.blue,expected_profile_revision=revision+1)
        self.assertEqual(store.load(),before)
        result,info=store.replace_visual_asset('default','image_pool','asset',self.blue,expected_profile_revision=revision)
        self.assertEqual(next(p for p in result['profiles'] if p['id']==other),other_before)
        self.assertEqual(info['affected_ids'],['used']);self.assertEqual(result['active_profile'],'default')
        pid_revision=next(p for p in result['profiles'] if p['id']=='default')['revision']
        result['render_plans'][0]['status']='running';store.save(result,result['revision'])
        with self.assertRaises(ValueError):store.replace_visual_asset('default','image_pool','asset',self.other,expected_profile_revision=pid_revision)
    def test_restage_merges_checked_tasks_and_clears_old_error_without_starting_work(self):
        self.state['jobs'][1]['render_checked']=True
        self.state['render_plans']=[dict(id='pending',profile_id='default',name='默认配置',ids=['used','unused'],known_ids=['used','unused'],status='staged',task_id='',error='old error')]
        value,info=assets.replace(self.state,'default','image_pool','asset',self.blue,expected_revision=0)
        plan=next(p for p in value['render_plans'] if p['status']=='staged')
        self.assertEqual(plan['ids'],['used','unused']);self.assertEqual(plan['error'],'');self.assertEqual(plan['task_id'],'')
        self.assertEqual(value['jobs'][1]['status'],'已完成');self.assertTrue(value['jobs'][1]['render_checked'])
    def test_replace_in_inactive_project_does_not_switch_or_mutate_active_editor(self):
        store=Store(self.root/'private');state=store.save(self.state,0);state=store.create_profile(state,'Other',clone=True)
        other=state['active_profile'];state=store.select_profile(state,'default');active=copy.deepcopy(state['jobs'])
        revision=next(p for p in state['profiles'] if p['id']==other).get('revision',0)
        result,info=store.replace_visual_asset(other,'image_pool','asset',self.blue,expected_profile_revision=revision)
        self.assertEqual(result['active_profile'],'default');self.assertEqual(result['jobs'],active)
        loaded=ProfileStore(store.directory,other).load();self.assertEqual(loaded['image_pool'][0]['path'],str(self.blue));self.assertTrue(loaded['jobs'][0]['render_dirty'])
    def test_failed_save_keeps_original_library_and_completed_status(self):
        from app_plugins.builtin.batch_text_video import store as store_module
        store=Store(self.root/'private');state=store.save(self.state,0);revision=state['profiles'][0]['revision']
        real=store_module.data_file
        def managed(path):
            result=real(path)
            def fail(*args,**kwargs):raise OSError('synthetic full disk')
            result.write_json=fail
            return result
        with patch.object(store_module,'data_file',side_effect=managed):
            with self.assertRaises(OSError):store.replace_visual_asset('default','image_pool','asset',self.blue,expected_profile_revision=revision)
        self.assertEqual(store.load(),state)
    def test_scaling_resolves_each_distinct_path_once_and_deduplicates_snapshots(self):
        from app_plugins.builtin.batch_text_video import store as store_module
        template=self.state['jobs'][0];self.state['jobs']=[dict(copy.deepcopy(template),id=str(i)) for i in range(782)]
        original=store_module.file_identity
        with patch.object(store_module,'file_identity',side_effect=original) as resolve:
            value,info=self.replace()
        self.assertEqual(len(info['affected_ids']),782);self.assertLess(resolve.call_count,35)
        self.assertEqual(len(value['render_configs']),1)
    def test_thumbnail_invalidation_cancels_stale_results_only_for_replaced_paths(self):
        from app_plugins.builtin.batch_text_video.thumbnails import ThumbnailService
        service=ThumbnailService();self.addCleanup(service.deleteLater)
        first=(str(self.red),'');other=(str(self.other),'');cancel=threading.Event()
        service.items[first]=(QtGui.QIcon(),100);service.items[other]=(QtGui.QIcon(),200);service.bytes=300
        service.running[first]=cancel;service.tried.update((first,other))
        service.invalidate([self.red,self.blue]);self.assertTrue(cancel.is_set());self.assertNotIn(first,service.items)
        service._ready(first,QtGui.QImage(10,10,QtGui.QImage.Format.Format_RGB32),cancel)
        self.assertNotIn(first,service.items);self.assertIn(other,service.items);self.assertEqual(service.bytes,200)
    def test_rightclick_entry_and_dirty_rows_are_visible_and_not_stuck_at_100_percent(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.asset_replace_ui import add_actions
        from app_plugins.builtin.batch_text_video.row_progress import PROGRESS_ROLE
        store=Store(self.root/'private');state,_=assets.replace(self.state,'default','image_pool','asset',self.blue,expected_revision=0);store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=store);self.addCleanup(dialog.deleteLater)
            try:
                dialog.image_pool.selectRow(0);menu=QtWidgets.QMenu(dialog);add_actions(dialog,menu,'image_pool')
                action=next(a for a in menu.actions() if a.text()=='替换素材…');self.assertTrue(action.isEnabled())
                panel=dialog.render_queue_panel.flat;panel.refresh(dialog.state)
                self.assertEqual(panel.status(('default','used')),'待重新渲染')
                row=panel.rows[('default','used')];self.assertEqual(panel.table.item(row,0).data(PROGRESS_ROLE),0)
                self.assertEqual(panel.table.item(row,3).text(),'0%')
                panel.filter.setCurrentIndex(panel.filter.findData('dirty'));self.assertEqual(panel.visible,{('default','used')})
                dialog.close()
            finally:dialog.close();self.app.processEvents()
    def test_ui_replacement_commits_once_and_reports_number_without_launching_render(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.asset_replace_ui import edit
        store=Store(self.root/'private');store.save(self.state,0)
        with patch.object(BatchTextVideoDialog,'request_background'),patch.object(BatchTextVideoDialog,'_start_overlay'):
            dialog=BatchTextVideoDialog(store=store);self.addCleanup(dialog.deleteLater)
            try:
                dialog.image_pool.selectRow(0)
                with patch.object(QtWidgets.QFileDialog,'getOpenFileName',return_value=(str(self.blue),'PNG')),patch.object(dialog,'_launch_batch') as launch:
                    self.assertTrue(edit(dialog,'image_pool'));launch.assert_not_called()
                loaded=store.load();self.assertEqual(loaded['jobs'][0]['status'],'待生成');self.assertTrue(loaded['jobs'][0]['render_checked'])
                self.assertIn('1条任务',dialog.status.text());self.assertEqual(dialog.layer_panel.values()[0]['path'],str(self.blue))
                self.assertEqual(dialog.render_queue_panel.flat.filter.currentData(),'dirty')
            finally:dialog.close();self.app.processEvents()
    @unittest.skipUnless(FFMPEG,'explicit FFmpeg required')
    def test_real_pool_replacement_rerenders_affected_only_with_identical_audio_and_rotation(self):
        from app_plugins.builtin.batch_text_video.background import execute_batch
        def ff(args):
            result=subprocess.run([FFMPEG,'-v','error','-y',*args],capture_output=True,timeout=50,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'));return result.stdout
        voice=self.root/'voice.wav';song=self.root/'song.wav'
        ff(['-f','lavfi','-i','sine=frequency=440:duration=0.8',str(voice)]);ff(['-f','lavfi','-i','sine=frequency=880:duration=4',str(song)])
        for job in self.state['jobs']:
            job.pop('render_assignment');job.pop('render_config_id');job.update(title='',body='',voice_path=str(voice),voice_override=True,status='待生成')
        self.state['settings'].update(ffmpeg_path=FFMPEG,aspect='custom',canvas_width=320,canvas_height=180,video_encoder='cpu',export_workers=2,
            encoder_fallback=False,output_dir=str(self.root/'out'),output_destination='directory',use_voice=True,music_enabled=True,
            skip_silence=False,auto_crop_black_bars=False,overwrite=False,darkness=0)
        layer=self.state['settings']['layers'][0];layer.update(source='sequence',sequence_items=[dict(id='r',path=str(self.red)),dict(id='o',path=str(self.other))],sequence_cursor=0)
        self.state['music']=[dict(id='song',path=str(song),cursor=0.,audio_key='')]
        store=Store(self.root/'private');state=store.save(self.state,0)
        def batch(state,ids):
            logs=[];result,value=execute_batch(store,state,ids,threading.Event(),lambda *_:None,logs.append,lambda *_:None)
            self.assertEqual((result['completed'],result['failed']),(len(ids),0),(result,logs[-4:]));return value,result
        state,first=batch(state,['used','unused']);original=copy.deepcopy(state);revision=state['profiles'][0]['revision']
        state,info=store.replace_visual_asset('default','image_pool','asset',self.blue,expected_profile_revision=revision)
        self.assertEqual(info['affected_ids'],['used']);self.assertEqual(state['jobs'][1],original['jobs'][1])
        state,second=batch(state,info['affected_ids'])
        self.assertEqual(state['music'],original['music']);self.assertEqual(state['settings']['layers'][0]['sequence_cursor'],original['settings']['layers'][0]['sequence_cursor'])
        self.assertNotIn('render_dirty',state['jobs'][0]);self.assertEqual(state['jobs'][1],original['jobs'][1])
        first_path=Path(first['paths'][0]);new_path=Path(second['paths'][0]);self.assertNotEqual(first_path,new_path);self.assertTrue(first_path.is_file())
        import numpy as np
        frame=lambda p:np.frombuffer(ff(['-ss','0.4','-i',str(p),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1']),np.uint8).reshape(180,320,3)
        self.assertGreater(int(frame(first_path)[90,160,0]),220);self.assertGreater(int(frame(new_path)[90,160,2]),220)
        audio=lambda p:ff(['-i',str(p),'-vn','-f','s16le','-c:a','pcm_s16le','pipe:1'])
        self.assertEqual(audio(first_path),audio(new_path))
