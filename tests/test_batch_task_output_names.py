from datetime import date
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video.task_naming import output_stem, refresh_task_names, naming_rules, _existing_year
from app_plugins.builtin.batch_text_video.task_texts import entry_from_record
from app_plugins.builtin.batch_text_video.matching import folder_entry
from app_plugins.builtin.batch_text_video.pools import apply_pairs, merge_copies
from app_plugins.builtin.batch_text_video.store import Store,fresh_state
from model.TaskResultExporter import export_task,first_existing_file


class TaskOutputNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            widget.close();widget.deleteLater()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def job(self,root,number='1000',kind='reels'):
        folder=Path(root)/'1002'/kind/number
        folder.mkdir(parents=True,exist_ok=True)
        return {'task_dir':str(folder),'task_id':number,'task_type':kind,
                'name':number+'_文字版','task_year':2026,'title':'Title','body':'Body'}

    def test_task_directory_and_type_produce_the_exact_delivery_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            job=self.job(root)
            self.assertEqual(output_stem(job),'2026年10月2日-reels')
            self.assertEqual(output_stem(self.job(root,kind='口播')),'2026年10月2日-口播')
            templates={'reels':{'mode':'single','candidate_names':['{year}年{month}月{day}日-reels.mp4']}}
            rules=naming_rules({'task_export_profiles':templates,'gemini_api_keys':['synthetic-private']})
            self.assertEqual(rules,{'reels':['{year}年{month}月{day}日-reels.mp4']})
            self.assertEqual(output_stem(job,rules),'2026年10月2日-reels')

    def test_old_project_year_is_read_from_existing_final_names_not_today(self):
        with tempfile.TemporaryDirectory() as root:
            job=self.job(root);job.pop('task_year')
            reference=Path(root)/'1002/reels/01';reference.mkdir()
            (reference/'2025年10月2日-reels.mp4').touch()
            _existing_year.cache_clear()
            self.assertEqual(output_stem(job),'2025年10月2日-reels')
            job['task_date']='2027-10-02'
            self.assertEqual(output_stem(job),'2027年10月2日-reels')

    def test_manual_names_non_task_outputs_and_invalid_layout_are_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            job=self.job(root)
            self.assertEqual(output_stem({**job,'name':'My custom video'}),'My custom video')
            self.assertEqual(output_stem({**job,'task_filename_auto':False}),'1000_文字版')
            self.assertEqual(output_stem({'name':'video_文字版','path':'x.mp4'}),'video_文字版')
            self.assertEqual(output_stem({**job,'task_dir':str(Path(root)/'1000')}),'1000_文字版')

    def test_loaded_project_year_and_month_day_override_stale_table_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            job=self.job(root)
            job['task_date']='2027-11-02'
            self.assertEqual(output_stem(job),'2026年10月2日-reels')
            job['task_date']='2026-02-30'
            self.assertEqual(output_stem(job),'2026年10月2日-reels')

    def test_names_migrate_in_memory_without_renaming_or_deleting_finished_videos(self):
        with tempfile.TemporaryDirectory() as root:
            job=self.job(root)
            old=Path(job['task_dir'])/'1000_文字版.mp4';old.write_bytes(b'existing result')
            job.update(id='old',path='background.mp4',output=str(old),status='已完成')
            state=fresh_state();state['jobs']=[job];state['copy_pool']=[dict(job)]
            refresh_task_names(state)
            first=state['jobs'][0]['name']
            refresh_task_names(state)
            self.assertEqual(first,'2026年10月2日-reels')
            self.assertEqual(state['jobs'][0]['name'],first)
            self.assertEqual(state['jobs'][0]['output'],str(old))
            self.assertEqual(state['jobs'][0]['status'],'已完成')
            self.assertEqual(old.read_bytes(),b'existing result')
            self.assertFalse((old.parent/(first+'.mp4')).exists())

    def test_table_import_pairing_and_profile_roundtrip_keep_date_and_recognition(self):
        with tempfile.TemporaryDirectory() as root:
            job=self.job(root)
            background=Path(root)/'bg.mp4';background.touch()
            record={'task_id':'1000','task_type':'reels','target_dir':job['task_dir'],
                    'task_audio_text':'Title\nBody','task_date':'2026-10-02'}
            entry=entry_from_record(record)
            self.assertEqual(entry['name'],'2026年10月2日-reels')
            state=fresh_state();merge_copies(state,[entry],sheet=True)
            apply_pairs(state,[{'copy':state['copy_pool'][0],'path':str(background)}])
            saved=Store(Path(root)/'private').save(state,0)
            self.assertEqual(saved['jobs'][0]['name'],'2026年10月2日-reels')
            self.assertEqual(saved['jobs'][0]['task_date'],'2026-10-02')
            self.assertEqual(saved['jobs'][0]['task_dir'],job['task_dir'])
            for number in (2,3):
                variant={**job,'name':f'1000_文字版_{number}'}
                state['jobs']=[variant];refresh_task_names(state);refresh_task_names(state)
                self.assertEqual(state['jobs'][0]['name'],f'2026年10月2日-reels{number}')

    def test_folder_import_does_not_misuse_generated_final_videos_as_background(self):
        with tempfile.TemporaryDirectory() as root:
            job=self.job(root);folder=Path(job['task_dir'])
            for name in ('2026年10月2日-reels.mp4','2026年10月2日-reels12.mp4','1000_文字版.mp4','1000_文字版_2.mp4','source.mp4'):
                (folder/name).touch()
            entry=folder_entry(folder,{'text_only':True})
            self.assertEqual(Path(entry['path']).name,'source.mp4')

    def test_real_generated_video_is_picked_up_by_existing_task_delivery(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        from app_plugins.builtin.batch_text_video.layers import componentize_layers
        from app_plugins.builtin.batch_text_video.store import DEFAULTS
        from app_plugins.builtin.batch_text_video.watermarks import apply_corner_watermarks
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()
        ffmpeg=Path(__file__).resolve().parents[1]/'runtime/ffmpeg/ffmpeg.exe'
        if not ffmpeg.is_file(): self.skipTest('Requires pinned local FFmpeg')
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);job=self.job(root)
            background=root/'background.mp4'
            subprocess.run([str(ffmpeg),'-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i',
                            'color=c=0x32665e:s=320x480:r=10:d=0.3','-c:v','libx264','-pix_fmt','yuv420p',str(background)],check=True,capture_output=True,timeout=40)
            job.update(id='task',path=str(background),requires_title=True)
            state=fresh_state();state['jobs']=[job]
            layers=componentize_layers(None,DEFAULTS)
            for layer in layers: layer['enabled']=False
            layers=apply_corner_watermarks(layers,{'top_right':'AI LZX'})
            layers[-1]['corners']['top_right'].update(watermark_inset_x=0,watermark_inset_y=0)
            state['settings'].update(ffmpeg_path=str(ffmpeg),aspect='source',use_voice=False,music_enabled=False,
                                     keep_audio=False,darkness=0,layers=layers)
            output,_=render_static_text(job,state,root/'cache')
            output=Path(output)
            self.assertEqual(output.name,'2026年10月2日-reels.mp4')
            self.assertEqual(output.parent,Path(job['task_dir']))
            candidate=first_existing_file(output.parent,['2026年10月2日-reels.mp4'])
            self.assertEqual(candidate,output)
            result=root/'result';result.mkdir()
            # Respect the production writer guard; this synthetic file is now
            # complete, but its timestamp must be beyond the settling interval.
            os.utime(output,(time.time()-10,time.time()-10))
            task=SimpleNamespace(task_id='1000',admin='Admin',creator='LZX',task_date='1002',task_name='Example',task_type='reels')
            exported=export_task(task,date(2026,10,2),root/'1002',result,root/'result-wsp',
                {'task_dir':'reels','mode':'single','candidate_names':['{year}年{month}月{day}日-reels.mp4']},
                {'task_output_name_template':'{task_id}.mp4'},False)
            self.assertEqual(len(exported),1)
            self.assertEqual(exported[0].read_bytes(),output.read_bytes())
            self.assertTrue(background.is_file())
            self.assertTrue(output.is_file())


if __name__=='__main__': unittest.main()
