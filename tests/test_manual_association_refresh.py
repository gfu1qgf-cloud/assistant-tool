import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from app_plugins.builtin.batch_text_video.association_refresh import scan,apply_results
from app_plugins.builtin.batch_text_video.matching import folder_entry
from app_plugins.builtin.batch_text_video.store import fresh_state


class ManualAssociationRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.state=fresh_state();self.state['settings'].update(use_voice=False,music_enabled=False,layers=[])
    def tasks(self,count=15):
        entries=[]
        for i in range(count):
            folder=self.root/str(i);folder.mkdir()
            entry=folder_entry(folder,{'text_only':True});entry.update(title='manual title',body='manual body')
            entries.append(entry)
            (folder/'task_audio.mp3').write_bytes(b'synthetic media; no decoding')
            (folder/'task_audio.srt').write_text('synthetic subtitle',encoding='utf8')
        self.state['copy_pool']=entries
        self.state['jobs']=[{**copy.deepcopy(v),'id':'job-'+v['id']} for v in entries]
    def test_one_click_scans_all_fifteen_folders_even_with_voice_disabled(self):
        self.tasks();original=copy.deepcopy(self.state)
        result=scan(self.state,threading.Event());updated,changed=apply_results(self.state,result)
        self.assertTrue(changed);self.assertEqual((result['total'],result['voice_folders']),(30,15))
        for value in updated['copy_pool']+updated['jobs']:
            self.assertTrue(Path(value['task_voice_path']).is_file());self.assertTrue(value['task_subtitle_path'])
            self.assertFalse(value['voice_path']);self.assertEqual(value['body'],'manual body')
        self.assertFalse(updated['settings']['use_voice']);self.assertEqual(self.state,original)
    def test_enabled_voice_fills_all_tasks_without_changing_other_allocations(self):
        self.tasks(2);self.state['settings']['use_voice']=True
        assigned={'version':1,'sources':{},'cursor_sources':{},'caption_styles':{},'voice':'','voice_origin':'task','background':'','music':None}
        self.state['jobs'][0]['render_assignment']=assigned
        result=scan(self.state,threading.Event());updated,_=apply_results(self.state,result)
        self.assertTrue(all(v['voice_path'] for v in updated['copy_pool']+updated['jobs']))
        self.assertTrue(updated['jobs'][0]['render_assignment']['voice'])
        self.assertEqual(updated['jobs'][0]['render_assignment']['sources'],{})
    def test_duplicate_copy_and_job_records_share_a_single_folder_scan(self):
        self.tasks(3)
        from app_plugins.builtin.batch_text_video.matching import folder_entry as original
        with patch('app_plugins.builtin.batch_text_video.matching.folder_entry',wraps=original) as discovery:
            scan(self.state,threading.Event())
        self.assertEqual(discovery.call_count,3)
    def test_manual_voice_and_user_content_remain_unchanged(self):
        self.tasks(1);self.state['settings']['use_voice']=True
        value=self.state['jobs'][0];value.update(voice_override=True,voice_path='manual.wav')
        result=scan(self.state,threading.Event());updated,_=apply_results(self.state,result)
        self.assertEqual(updated['jobs'][0]['voice_path'],'manual.wav')
        self.assertEqual(updated['jobs'][0]['title'],'manual title')
    def test_one_unreadable_task_does_not_prevent_healthy_links(self):
        self.tasks(2)
        from app_plugins.builtin.batch_text_video.association_refresh import for_state as original
        def refresh(job,state,**options):
            if job['task_dir']==str(self.root/'0'):raise PermissionError('synthetic denied')
            return original(job,state,**options)
        with patch('app_plugins.builtin.batch_text_video.association_refresh.for_state',side_effect=refresh):
            result=scan(self.state,threading.Event())
        updated,_=apply_results(self.state,result)
        self.assertTrue(updated['jobs'][1]['task_voice_path']);self.assertFalse(updated['jobs'][0]['task_voice_path'])
        self.assertIn('synthetic denied','\n'.join(result['issues']))
    def test_cancellation_and_conflicting_manual_changes_do_not_partially_save(self):
        self.tasks(2);cancel=threading.Event();cancel.set()
        result=scan(self.state,cancel);self.assertEqual(apply_results(self.state,result),(self.state,False))
        result=scan(self.state,threading.Event());self.state['jobs'][0]['voice_override']=True
        before=copy.deepcopy(self.state)
        with self.assertRaisesRegex(ValueError,'手动修改'):apply_results(self.state,result)
        self.assertEqual(self.state,before)

    def test_missing_old_voice_is_not_reported_as_found(self):
        self.tasks(1);audio=self.root/'0'/'task_audio.mp3'
        for job in self.state['copy_pool']+self.state['jobs']:job['task_voice_path']=str(audio)
        audio.unlink();result=scan(self.state,threading.Event())
        self.assertEqual((result['voice_folders'],result['missing_folders']),(0,1))

    def test_old_video_rows_without_task_dir_report_their_discovered_voice(self):
        self.tasks(1);self.state['copy_pool']=[];self.state['settings']['use_voice']=True
        video=self.root/'0'/'background.mp4';video.write_bytes(b'synthetic')
        self.state['jobs'][0].update(task_dir='',path=str(video))
        result=scan(self.state,threading.Event())
        self.assertEqual(result['voice_folders'],1)
        updated,_=apply_results(self.state,result)
        self.assertTrue(updated['jobs'][0]['voice_path'])
