import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.store import Store,ProfileStore,fresh_state
from app_plugins.builtin.batch_text_video.export_destination import settings_for,output_stem
from app_plugins.builtin.batch_text_video.background import validate_batch_outputs

FF=os.environ.get('NODE_FFMPEG','')

class PrefixTests(unittest.TestCase):
    def test_default_is_unchanged_and_prefix_is_resolved_from_the_rendered_project(self):
        state=fresh_state();job={'name':'我的视频'}
        self.assertEqual(output_stem(job,settings_for(state)),'我的视频')
        state['settings']['output_project_prefix']=True
        state['profiles']=[dict(id='a',name='双人版'),dict(id='b',name='三人版')]
        for pid,name in (('a','双人版'),('b','三人版')):
            state['active_profile']=pid
            self.assertEqual(output_stem(job,settings_for(state)),name+'_我的视频')
        self.assertEqual(job,{'name':'我的视频'})

    def test_project_text_is_sanitized_and_cannot_escape_destination(self):
        from app_plugins.builtin.batch_text_video.engine import safe_output
        state=fresh_state();state['settings']['output_project_prefix']=True
        state['profiles'][0]['name']='../项目:甲/乙'
        with tempfile.TemporaryDirectory() as tmp:
            path=safe_output(tmp,output_stem({'name':'clip.mp4'},settings_for(state)),())
            self.assertEqual(path.parent,Path(tmp).resolve());self.assertNotIn('/',path.name)
            self.assertIn('项目_甲_乙_clip',path.name)
        state['profiles']=[]
        with self.assertRaisesRegex(ValueError,'项目名'):settings_for(state)

    def test_global_preference_survives_switch_clone_and_rename_without_rewriting_job_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp);state=store.load();state['settings']['output_project_prefix']=True
            state['jobs']=[dict(id='one',name='original',path='',title='',body='',status='待生成',output='',error='')]
            state=store.save(state,state['revision']);state=store.create_profile(state,'第二项目',clone=True)
            second=state['active_profile'];self.assertTrue(state['settings']['output_project_prefix'])
            rendered=ProfileStore(tmp,'default').load()
            self.assertEqual(output_stem(rendered['jobs'][0],settings_for(rendered)),'默认配置_original')
            self.assertEqual(rendered['jobs'][0]['name'],'original')
            state=store.select_profile(state,'default');state['profiles'][0]['name']='改名后';state=store.save(state,state['revision'])
            self.assertEqual(output_stem(state['jobs'][0],settings_for(state)),'改名后_original')
            self.assertTrue(ProfileStore(tmp,second).load()['settings']['output_project_prefix'])

    def test_preflight_still_rejects_sanitized_collisions_only_when_overwriting(self):
        state=fresh_state();state['settings'].update(output_project_prefix=True,overwrite=True)
        with tempfile.TemporaryDirectory() as tmp:
            state['settings']['output_dir']=tmp
            state['jobs']=[dict(id=str(i),name=name,path='') for i,name in enumerate(('a:b','a?b'))]
            with self.assertRaisesRegex(ValueError,'同一成品'):validate_batch_outputs(state,['0','1'])
            state['settings']['overwrite']=False;validate_batch_outputs(state,['0','1'])

    def test_prefixed_task_families_keep_numbering_and_are_not_reimported_as_raw_material(self):
        from app_plugins.builtin.batch_text_video.export_destination import numbered
        from app_plugins.builtin.batch_text_video.task_naming import is_task_video
        name='项目_甲_2026年11月12日-reels.mp4'
        self.assertEqual(numbered(Path(name),2).name,'项目_甲_2026年11月12日-reels2.mp4')
        self.assertTrue(is_task_video(name));self.assertTrue(is_task_video(numbered(Path(name),2)))
        self.assertFalse(is_task_video('我的原始素材.mp4'))
        state=fresh_state();state['settings']['output_project_prefix']=True
        with self.assertRaisesRegex(ValueError,'文件名过长'):
            output_stem({'name':'长'*250},settings_for(state))

    def test_naming_does_not_invalidate_playback_cache(self):
        from app_plugins.builtin.batch_text_video.playback import preview_key
        state=fresh_state();state['settings'].update(layers=[],music_enabled=False)
        job=dict(id='a',name='preview',path='',title='',body='')
        first=preview_key(job,state);state['settings']['output_project_prefix']=True
        self.assertEqual(first,preview_key(job,state))


@unittest.skipUnless(FF,'explicit local FFmpeg required')
class RealPrefixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def test_parallel_outputs_have_project_prefix_number_safely_and_preview_keeps_its_cache_name(self):
        from app_plugins.builtin.batch_text_video.components import new_text_component
        from app_plugins.builtin.batch_text_video.background import execute_batch
        from app_plugins.builtin.batch_text_video.playback import PlaybackWorker,preview_key
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.mp4'
            subprocess.run([FF,'-nostdin','-v','error','-f','lavfi','-i','color=red:s=128x128:r=10:d=0.3',
                '-c:v','libx264','-threads','1',str(source)],check=True,capture_output=True,timeout=20)
            text=new_text_component();text.update(text='TEST',font_min=12,font_max=16,width=90,height=90)
            state=fresh_state();state['profiles'][0]['name']='项目甲'
            state['settings'].update(ffmpeg_path=FF,output_dir=str(root/'output'),output_destination='directory',output_project_prefix=True,
                aspect='source',layers=[text],music_enabled=False,overwrite=False,auto_crop_black_bars=False,export_workers=2,
                margin_x=0,margin_top=0,margin_bottom=0)
            state['jobs']=[dict(id=str(i),name='clip',path=str(source),title='',body='',status='待生成',output='',error='') for i in range(3)]
            store=Store(root/'private');state=store.save(state,0);out=root/'output';out.mkdir();(out/'项目甲_clip.mp4').write_bytes(b'KEEP')
            result,saved=execute_batch(store,state,['0','1','2'],threading.Event(),lambda *_:None,lambda *_:None,lambda *_:None)
            self.assertEqual((result['completed'],result['failed']),(3,0),result)
            paths=[Path(j['output']) for j in saved['jobs']]
            self.assertEqual({p.name for p in paths},{'项目甲_clip_2.mp4','项目甲_clip_3.mp4','项目甲_clip_4.mp4'})
            self.assertEqual((out/'项目甲_clip.mp4').read_bytes(),b'KEEP')
            self.assertEqual([j['name'] for j in saved['jobs']],['clip']*3)
            key=preview_key(saved['jobs'][0],saved);results=[]
            worker=PlaybackWorker(saved['jobs'][0],saved,root/'preview',key);worker.ready.connect(results.append);worker.run();worker.deleteLater()
            self.assertNotIn('error',results[0],results);self.assertEqual(Path(results[0]['path']).name,key+'.mp4')
