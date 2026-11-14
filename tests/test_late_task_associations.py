import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video import task_associations as assoc
from app_plugins.builtin.batch_text_video.audio_sources import resolve_voice
from app_plugins.builtin.batch_text_video.matching import folder_entry,merge_entries
from app_plugins.builtin.batch_text_video.task_texts import entry_from_record
from app_plugins.builtin.batch_text_video.store import Store,fresh_state
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.captions import new_caption,caption_subtitle_path

FFMPEG=os.environ.get('NODE_FFMPEG','')


class LateAssociationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.task=self.root/'task';self.task.mkdir()
        self.job=entry_from_record(dict(task_id='1000',task_type='reels',target_dir=str(self.task),task_audio_text='Title\nHello world',audio_name='Narration'))
        self.state=fresh_state();self.state['settings'].update(use_voice=True,music_enabled=False)
        self.state['jobs']=[self.job]
    def audio(self,name='task_audio.wav'):
        path=self.task/name
        with wave.open(str(path),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(8000);output.writeframes(b'\x01\x10'*6400)
        return path
    def subtitle(self,name='task_audio.srt'):
        path=self.task/name;path.write_text('1\n00:00:00,000 --> 00:00:00,350\nHello\n\n2\n00:00:00,350 --> 00:00:00,800\nworld\n',encoding='utf8');return path
    def settings(self):return self.state['settings']
    def assignment(self,voice='',background='',**extra):
        return dict(version=1,sources={},cursor_sources={},caption_styles={},music=None,voice=voice,background=background,**extra)

    def test_optional_import_no_longer_freezes_missing_audio(self):
        self.assertEqual(resolve_voice(self.job,self.state,discover=True),'')
        path=self.audio();self.assertEqual(resolve_voice(self.job,self.state,discover=True),str(path))
        self.assertEqual(self.job['task_voice_path'],'')
    def test_discovery_fills_audio_subtitle_and_background_without_changing_text_or_id(self):
        audio=self.audio();subtitle=self.subtitle();video=self.task/'background.mp4';video.write_bytes(b'video')
        before=copy.deepcopy(self.job);value=assoc.refresh(self.job,self.settings())['job']
        self.assertEqual(value['task_voice_path'],str(audio));self.assertEqual(value['voice_path'],str(audio))
        self.assertEqual(value['task_subtitle_path'],str(subtitle));self.assertEqual(value['path'],str(video))
        for field in ('id','title','body','name'):self.assertEqual(value[field],before[field])
        self.assertEqual(self.job,before)
    def test_named_audio_metadata_survives_pairing_and_later_creation(self):
        from app_plugins.builtin.batch_text_video.pools import apply_pairs
        self.assertEqual(self.job['task_audio_name'],'Narration')
        apply_pairs(self.state,[dict(copy=self.job,path='')]);paired=self.state['jobs'][-1]
        self.assertEqual(paired['task_audio_name'],'Narration')
        path=self.audio('Narration.wav');self.assertEqual(resolve_voice(paired,self.state,discover=True),str(path))
    def test_old_sheet_name_can_be_recovered_without_overwriting_manual_text(self):
        self.job.pop('task_audio_name');sheet=self.root/'sheet.ods';sheet.write_bytes(b'sheet');self.job['source_sheet']=str(sheet)
        self.job['body']='manually edited';audio=self.audio('Custom.wav')
        with patch('app_plugins.builtin.batch_text_video.task_texts.read_sheet_records',return_value=([dict(target_dir=str(self.task),audio_name='Custom')],{})):
            value=assoc.refresh(self.job,self.settings())['job']
        self.assertEqual(value['task_voice_path'],str(audio));self.assertEqual(value['body'],'manually edited')
    def test_missing_result_is_rechecked_without_reimport_or_restart(self):
        first=assoc.refresh(self.job,self.settings());self.assertEqual(first['job']['task_voice_path'],'')
        path=self.audio();second=assoc.refresh(first['job'],self.settings())
        self.assertEqual(second['job']['task_voice_path'],str(path))
        self.assertNotEqual(first['job']['association_signature'],second['job']['association_signature'])
    def test_ambiguity_is_not_guessed_and_resolves_after_duplicate_removed(self):
        first=self.audio();second=self.audio('task_audio.mp3')
        value=assoc.refresh(self.job,self.settings())['job'];self.assertFalse(value['task_voice_path']);self.assertIn('多个',value['match_error'])
        with self.assertRaisesRegex(ValueError,'多个'):resolve_voice(value,self.state,discover=True)
        second.unlink();value=assoc.refresh(value,self.settings())['job'];self.assertEqual(value['task_voice_path'],str(first));self.assertFalse(value['match_error'])
    def test_background_ambiguity_and_explicit_none_are_preserved(self):
        for name in ('one.mp4','two.mov'):(self.task/name).write_bytes(b'video')
        self.assertEqual(assoc.refresh(self.job,self.settings())['job']['path'],'')
        (self.task/'two.mov').unlink();settings={**self.settings(),'background_pool_id':'none'}
        value=assoc.refresh(self.job,settings)['job'];self.assertEqual(value['path'],'');self.assertTrue(value['task_background_path'])
    def test_explicit_voice_override_and_disabled_card_are_not_automatically_enabled(self):
        task_voice=self.audio();manual=self.audio('manual.wav');self.job.update(voice_override=True,voice_path=str(manual))
        value=assoc.refresh(self.job,self.settings())['job'];self.assertEqual(value['voice_path'],str(manual));self.assertEqual(value['task_voice_path'],str(task_voice))
        self.job['voice_path']='';value=assoc.refresh(self.job,self.settings())['job'];self.assertEqual(value['voice_path'],'')
        self.assertEqual(resolve_voice(value,self.state,discover=True),'')
        self.job.pop('voice_override');value=assoc.refresh(self.job,{**self.settings(),'use_voice':False})['job']
        self.assertEqual(value['voice_path'],'');self.assertEqual(value['task_voice_path'],str(task_voice))
    def test_empty_automatic_frozen_assignment_fills_without_resetting_other_allocations(self):
        self.job['render_assignment']=self.assignment(voice_origin='task');self.job['render_assignment']['sources']['picture']={'field':'path','value':'picture.png'}
        before=copy.deepcopy(self.job['render_assignment']);audio=self.audio()
        result=assoc.refresh(self.job,self.settings());after=result['job']['render_assignment']
        self.assertEqual(after['voice'],str(audio));before['voice']=str(audio);self.assertEqual(after,before)
        assoc.apply(self.job,result['proposal']);self.assertEqual(self.job['render_assignment']['voice'],str(audio))
    def test_valid_fixed_binding_and_explicit_import_are_not_replaced(self):
        fixed=self.audio('fixed.wav');self.audio()
        self.job['render_assignment']=self.assignment(voice=str(fixed),voice_origin='fixed')
        value=assoc.refresh(self.job,self.settings())['job'];self.assertEqual(value['render_assignment']['voice'],str(fixed))
        self.job['render_assignment']['voice']='';self.job['task_audio_auto']=False
        value=assoc.refresh(self.job,self.settings())['job'];self.assertEqual(value['render_assignment']['voice'],'')
    def test_stale_automatic_audio_extension_relinks_but_manual_path_does_not(self):
        old=self.task/'task_audio.mp3';self.job.update(task_voice_path=str(old),voice_path=str(old))
        self.job['render_assignment']=self.assignment(voice=str(old),voice_origin='task')
        new=self.audio();result=assoc.refresh(self.job,self.settings())
        self.assertEqual(result['job']['render_assignment']['voice'],str(new));self.assertEqual(result['job']['task_voice_path'],str(new))
        self.job.update(voice_override=True);result=assoc.refresh(self.job,self.settings())
        self.assertEqual(result['job']['voice_path'],str(old))
    def test_preflight_defers_missing_optional_voice_to_worker_not_gui_directory_scan(self):
        from app_plugins.builtin.batch_text_video.export_validation import validate_caption_voice
        self.state['settings']['layers']=normalize_layers([new_caption()]);self.job['render_assignment']=self.assignment(voice_origin='task')
        with patch.object(Path,'iterdir',side_effect=AssertionError('GUI scanned task folder')):validate_caption_voice(self.state,[self.job])
        self.job['voice_override']=True
        with self.assertRaisesRegex(ValueError,'动态字幕需要人声'):validate_caption_voice(self.state,[self.job])
    def test_reimport_refreshes_same_task_instead_of_duplicating_after_voice_appears(self):
        state={'jobs':[copy.deepcopy(self.job)],'backgrounds':[]};original=state['jobs'][0]['id']
        self.audio();self.subtitle();entry=folder_entry(self.task,dict(text_only=True))
        self.assertEqual(merge_entries(state,[entry]),1);self.assertEqual(len(state['jobs']),1);self.assertEqual(state['jobs'][0]['id'],original)
        self.assertEqual(merge_entries(state,[entry]),0)
    def test_explicit_multiple_audio_imports_in_one_folder_stay_distinct(self):
        first=self.audio('a.wav');second=self.audio('b.wav');state={'jobs':[],'backgrounds':[]}
        self.assertEqual(merge_entries(state,[folder_entry(self.task,explicit_voice=str(first)),folder_entry(self.task,explicit_voice=str(second))]),2)
        self.assertEqual(len(state['jobs']),2)
    def test_new_subtitle_and_word_records_invalidate_fingerprint_and_playback_cache(self):
        from app_plugins.builtin.batch_text_video.playback import preview_key
        from model.ElevenLabsTimings import save_timings
        self.audio();caption=new_caption();caption['caption_timing']='auto';self.state['settings']['layers']=normalize_layers([caption])
        first=preview_key(self.job,self.state);self.subtitle();second=preview_key(self.job,self.state);self.assertNotEqual(first,second)
        audio=self.task/'task_audio.wav';words=[dict(text='Hello',start=0.,end=.35),dict(text='world',start=.35,end=.8)]
        before=assoc.refresh(self.job,self.settings())['job']['association_signature']
        save_timings(audio,words,.8,source='offline test')
        after=assoc.refresh(self.job,self.settings())['job']['association_signature'];self.assertNotEqual(before,after)
    def test_late_bound_srt_and_text_files_are_read_again_on_use(self):
        from app_plugins.builtin.batch_text_video.components import new_text_component,resolve_sources
        source=self.root/'texts'/'content.txt';layer=new_text_component();layer.update(source='file',source_file=str(source))
        self.state['settings']['layers']=normalize_layers([layer])
        before=assoc.refresh(self.job,self.settings());self.assertIn(str(source),before['dependency_files'])
        source.parent.mkdir();source.write_text('Late text',encoding='utf8')
        after=assoc.refresh(self.job,self.settings());self.assertIn(str(source),after['dependency_files'])
        resolved,_=resolve_sources(self.settings()['layers']);self.assertEqual(resolved[0]['text'],'Late text')
        self.assertNotEqual(before['job']['association_signature'],after['job']['association_signature'])
        caption=new_caption();caption.update(caption_timing='file',caption_file=str(self.task/'later.srt'))
        with self.assertRaises(FileNotFoundError):caption_subtitle_path(self.job,'',caption)
        path=self.subtitle('later.srt');self.assertEqual(caption_subtitle_path(self.job,'',caption),path)
    def test_changed_manual_source_during_async_scan_is_not_overwritten(self):
        self.audio();result=assoc.refresh(self.job,self.settings());self.job.update(voice_override=True,voice_path='manual.wav')
        self.assertFalse(assoc.apply(self.job,result['proposal'],strict=False));self.assertEqual(self.job['voice_path'],'manual.wav')
    def test_cancel_and_private_preview_cache_never_become_new_task_sources(self):
        cancel=threading.Event();cancel.set()
        from app_plugins.builtin.batch_text_video.engine import Cancelled
        with self.assertRaises(Cancelled):assoc.refresh(self.job,self.settings(),cancel=cancel)
        (self.task/'old-preview.mp4').write_bytes(b'generated preview');frozen={**self.job,'_associations_frozen':True}
        self.assertEqual(assoc.refresh(frozen,self.settings())['job']['path'],'')

    @unittest.skipUnless(FFMPEG,'explicit FFmpeg required')
    def test_selected_task_rechecks_on_use_not_while_idle_or_via_file_watch(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        store=Store(self.root/'private');state=copy.deepcopy(self.state);state['settings'].update(ffmpeg_path=FFMPEG,layers=[]);store.save(state,0)
        with patch.object(BatchTextVideoDialog,'_start_overlay'):
            owner=BatchTextVideoDialog(store=store);owner.show();owner.jobs.setCurrentCell(0,0)
            def wait(condition,seconds=8):
                end=time.monotonic()+seconds
                while time.monotonic()<end:
                    self.app.processEvents()
                    if condition():return
                    time.sleep(.01)
                self.fail(owner.status.text()+' / '+owner.voice_label.text())
            try:
                from qt_compat import QtCore
                wait(lambda:owner.preview_worker is None and bool(owner._preview_path))
                self.assertFalse(owner.association_access.findChildren(QtCore.QFileSystemWatcher))
                self.assertFalse(owner.association_access.findChildren(QtCore.QTimer))
                active=owner.active_entry()
                self.assertNotEqual(owner.association_access.request_token(active),
                    owner.association_access.request_token({**active,'id':'another task using the same media'}))
                path=self.audio();subtitle=self.subtitle()
                end=time.monotonic()+.7
                while time.monotonic()<end:self.app.processEvents();time.sleep(.01)
                self.assertFalse(owner.state['jobs'][0].get('task_voice_path'))
                # Ordinary cached preview request must check first, even without force.
                owner.request_background(owner.active_entry()['path'])
                wait(lambda:owner.state['jobs'][0].get('task_voice_path')==str(path) and owner._voice_duration is not None)
                self.assertEqual(owner.state['jobs'][0]['task_subtitle_path'],str(subtitle));self.assertIn(path.name,owner.voice_label.text())
                self.assertEqual(store.load()['jobs'][0]['task_voice_path'],str(path))
            finally:
                owner.close();end=time.monotonic()+10
                while owner.has_local_work() and time.monotonic()<end:self.app.processEvents();time.sleep(.01)
                owner.deleteLater();self.app.processEvents()

    def test_relinked_audio_does_not_keep_the_previous_stems_subtitle(self):
        old=self.task/'Narration.mp3';old_srt=self.subtitle('Narration.srt')
        self.job.update(voice_path=str(old),task_voice_path=str(old),task_subtitle_path=str(old_srt))
        audio=self.audio();subtitle=self.subtitle()
        value=assoc.refresh(self.job,self.settings())['job']
        self.assertEqual(value['task_voice_path'],str(audio));self.assertEqual(value['task_subtitle_path'],str(subtitle))

    def test_dormant_task_audio_and_subtitle_do_not_change_disabled_output_signature(self):
        settings={**self.settings(),'use_voice':False}
        before=assoc.refresh(self.job,settings)['job']
        self.audio();self.subtitle()
        after=assoc.refresh(before,settings)['job']
        self.assertEqual(before['association_signature'],after['association_signature'])
        self.assertFalse(after['voice_path'])

    def test_manual_voice_signature_ignores_other_task_audio(self):
        manual=self.audio('manual.wav');self.job.update(voice_override=True,voice_path=str(manual))
        before=assoc.refresh(self.job,self.settings())['job']
        self.audio();self.subtitle()
        after=assoc.refresh(before,self.settings())['job']
        self.assertEqual(before['association_signature'],after['association_signature'])
        self.assertEqual(after['voice_path'],str(manual))

    def test_saved_composition_retains_disabled_voice_when_live_project_enables_it(self):
        self.audio();self.subtitle()
        config=copy.deepcopy(self.settings());config.update(use_voice=False,background_pool_id='none')
        self.state['render_configs']={'saved':config}
        self.job.update(render_config_id='saved',render_from_snapshot=True)
        value=assoc.for_state(self.job,self.state)['job']
        self.assertFalse(value['voice_path']);self.assertFalse(value['path'])

    def test_playback_discovery_failure_is_delivered_as_a_result_not_a_dead_worker(self):
        from app_plugins.builtin.batch_text_video.playback import PlaybackWorker
        worker=PlaybackWorker(self.job,self.state,self.root/'private','key');results=[]
        worker.ready.connect(results.append)
        with patch.object(assoc,'for_state',side_effect=OSError('synthetic inaccessible source')):
            worker.run()
        self.assertEqual(len(results),1);self.assertIn('inaccessible source',results[0]['error'])

    def test_editing_a_bound_file_in_place_invalidates_the_next_use(self):
        from app_plugins.builtin.batch_text_video.components import new_text_component
        source=self.root/'content.txt';source.write_text('first',encoding='utf8')
        layer=new_text_component();layer.update(source='file',source_file=str(source))
        self.state['settings']['layers']=normalize_layers([layer])
        first=assoc.refresh(self.job,self.settings())['job']
        source.write_text('updated longer content',encoding='utf8')
        second=assoc.refresh(first,self.settings())['job']
        self.assertNotEqual(first['association_signature'],second['association_signature'])

    @unittest.skipUnless(FFMPEG,'explicit FFmpeg required')
    def test_real_render_late_voice_video_and_srt_replace_old_empty_associations(self):
        from app_plugins.builtin.batch_text_video.background import execute_batch
        from app_plugins.builtin.batch_text_video.engine import probe
        from app_plugins.builtin.batch_text_video.components import new_text_component
        state=copy.deepcopy(self.state);layer=new_text_component();layer.update(text='Hello world',font_min=45,font_max=45)
        state['settings'].update(ffmpeg_path=FFMPEG,aspect='custom',canvas_width=320,canvas_height=180,video_encoder='cpu',export_workers=2,
            output_dir=str(self.root/'out'),output_destination='directory',skip_silence=False,auto_crop_black_bars=False,
            unvoiced_duration_mode='fixed',unvoiced_duration_seconds=1,encoder_fallback=False,keep_media_cache=False)
        state['settings']['layers']=normalize_layers([layer])
        for value in state['settings']['layers']:
            if value['id']!=layer['id']:value['enabled']=False
        store=Store(self.root/'private');state=store.save(state,0)
        def batch(value):
            messages=[];result,updated=execute_batch(store,value,[self.job['id']],threading.Event(),lambda *_:None,messages.append,lambda *_:None)
            self.assertEqual((result['completed'],result['failed']),(1,0),(result,messages[-5:]));return updated,Path(result['paths'][0])
        state,old=batch(state);self.assertFalse(probe(old,str(Path(FFMPEG).with_name('ffprobe.exe')))['audio'])
        audio=self.audio();subtitle=self.subtitle();video=self.task/'background.mp4'
        result=subprocess.run([FFMPEG,'-v','error','-y','-f','lavfi','-i','color=blue:s=320x180:r=25:d=1','-c:v','libx264',str(video)],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        caption=new_caption();caption.update(caption_timing='task',caption_source='body',font_min=45,font_max=45)
        state['settings']['layers']=normalize_layers([caption])
        for value in state['settings']['layers']:
            if value['id']!=caption['id']:value['enabled']=False
        state=store.save(state,state['revision'])
        with patch('app_plugins.builtin.batch_text_video.captions.load_word_timings',side_effect=AssertionError('re-aligned existing SRT')):
            state,new=batch(state)
        info=probe(new,str(Path(FFMPEG).with_name('ffprobe.exe')));self.assertTrue(info['audio']);self.assertAlmostEqual(info['duration'],.8,delta=.08)
        job=state['jobs'][0];self.assertEqual(job['task_voice_path'],str(audio));self.assertEqual(job['task_subtitle_path'],str(subtitle))
        self.assertEqual(job['render_assignment']['voice'],str(audio));self.assertEqual(job['render_assignment']['background'],str(video))
        self.assertTrue(old.is_file());self.assertNotEqual(old,new)
        self.assertEqual(assoc.refresh(job,state['settings'])['job']['association_signature'],job['association_signature'])
