"""Offline contracts: mock the installed SDK; never send real task audio/keys."""
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx
from elevenlabs.client import ElevenLabs
from model.ElevenLabsTimings import (character_words, load_timings, save_timings,
    matching_words, validate_words)
from model.ElevenLabsService import ElevenLabsService, ALIGNMENT_KEY, alignment_session
from model.ApiKeyHelper import api_key_id

TEXT = 'Chlieb náš každodenný.'
WORDS = [dict(text='Chlieb', start=.1, end=.3), dict(text='náš', start=.4, end=.6),
         dict(text='každodenný.', start=.7, end=.95)]
PREFIX = 'app_plugins.builtin.batch_text_video.'


def aligned(text=TEXT):
    step=.8 / len(text)
    return dict(characters=list(text), character_start_times_seconds=[.1+i*step for i in range(len(text))],
                character_end_times_seconds=[.1+(i+1)*step for i in range(len(text))])


class TimingArtifactTests(unittest.TestCase):
    def test_original_spelling_diacritics_and_subset(self):
        words=character_words(aligned('ÁMEN. Chlieb náš každodenný. 2026'))
        self.assertEqual([w['text'] for w in words], ['ÁMEN.', 'Chlieb', 'náš', 'každodenný.', '2026'])
        self.assertEqual([w['text'] for w in matching_words(words,TEXT)], TEXT.split())
        with self.assertRaises(ValueError): matching_words(words, 'Chlieb iný')
        with self.assertRaises(ValueError): matching_words(WORDS+WORDS,TEXT)

    def test_zero_duration_words_rejected_not_guessed(self):
        words=copy.deepcopy(WORDS); words[1]['end']=words[1]['start']
        with self.assertRaises(ValueError): validate_words(words,1.)

    def test_silent_audio_tags_are_not_displayed(self):
        result=aligned('[laughs] Ámen.')
        result['character_start_times_seconds'][:8]=[0.]*8
        result['character_end_times_seconds'][:8]=[0.]*8
        self.assertEqual([w['text'] for w in character_words(result)], ['Ámen.'])

    def test_incremental_and_original_srt_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            audio=Path(directory)/'task_audio.mp3'; audio.write_bytes(b'fixture')
            original=audio.with_suffix('.srt'); original.write_bytes(b'original-davinci-subtitles')
            first=save_timings(audio,WORDS,1.,source='test')
            second=save_timings(audio,WORDS,1.,source='test')
            self.assertEqual(first.name,'task_audio[动态].srt')
            self.assertEqual(second.name,'task_audio[动态2].srt')
            self.assertEqual(load_timings(audio,TEXT),WORDS)
            self.assertEqual(original.read_bytes(),b'original-davinci-subtitles')
            second.write_text('changed',encoding='utf8')
            self.assertEqual(load_timings(audio,TEXT,1.),WORDS)  # falls back to valid first pair
            first.write_text('changed',encoding='utf8')
            self.assertIsNone(load_timings(audio,TEXT,1.))

    def test_changed_audio_and_changed_text_do_not_reuse_old_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            audio=Path(directory)/'task_audio.mp3'; audio.write_bytes(b'fixture')
            save_timings(audio,WORDS,1.,source='test')
            self.assertIsNone(load_timings(audio,'Other text',1.))
            audio.write_bytes(b'new-fixture')
            self.assertIsNone(load_timings(audio,TEXT,1.))

    def test_malformed_metadata_is_a_cache_miss_not_an_audio_error(self):
        with tempfile.TemporaryDirectory() as directory:
            audio=Path(directory)/'task_audio.mp3'; audio.write_bytes(b'fixture')
            audio.with_name('task_audio[动态].json').write_text('[]',encoding='utf8')
            self.assertIsNone(load_timings(audio,TEXT))


class SpeechTimestampTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg=os.environ.get('BATCH_TEST_FFMPEG') or os.environ.get('NODE_FFMPEG')
        if not cls.ffmpeg: raise unittest.SkipTest('native FFmpeg unavailable')
        cls.folder=tempfile.TemporaryDirectory()
        fixture=Path(cls.folder.name)/'fixture.mp3'
        result=subprocess.run([cls.ffmpeg,'-hide_banner','-nostdin','-loglevel','error','-y',
            '-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','libmp3lame','-b:a','128k',
            '-ar','44100',str(fixture)],capture_output=True)
        if result.returncode: raise AssertionError(result.stderr)
        cls.audio=fixture.read_bytes()

    @classmethod
    def tearDownClass(cls): cls.folder.cleanup()

    def run_tts(self, function, *, alignment=True, text=TEXT, save_error=False):
        from model.AudioHelper import CreateAudio, CreateAudio3, CreateAudioLatest
        functions=[CreateAudio,CreateAudio3,CreateAudioLatest]
        requests=[]
        def transport(request):
            requests.append(request)
            value=dict(audio_base64=base64.b64encode(self.audio).decode(),alignment=aligned(text) if alignment else None)
            return httpx.Response(200,json=value)
        with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(transport)) as http:
            target=Path(directory)/'task_audio.mp3'
            original=target.with_suffix('.srt'); original.write_bytes(b'ORIGINAL\r\n')
            client=ElevenLabs(api_key='dummy-not-real',httpx_client=http)
            with patch('model.AudioHelper.ElevenLabs',return_value=client), \
                    patch('model.AudioHelper._speech_ffmpeg',return_value=self.ffmpeg):
                if save_error:
                    with patch('model.ElevenLabsTimings.save_timings',side_effect=OSError('disk')):
                        success=functions[function](text,str(target),voice_id='voice',api_keys=['dummy'],api_key_status_config=None)
                else:
                    success=functions[function](text,str(target),voice_id='voice',api_keys=['dummy'],api_key_status_config=None)
            self.assertTrue(success)
            self.assertEqual(target.read_bytes(),self.audio)
            self.assertEqual(original.read_bytes(),b'ORIGINAL\r\n')
            words=load_timings(target,text)
            self.assertEqual(words is not None,alignment and not save_error)
            self.assertEqual(len(requests),1)
            self.assertTrue(requests[0].url.path.endswith('/with-timestamps'))
            self.assertNotIn('/forced-alignment',requests[0].url.path)
            return requests[0]

    def test_all_existing_ele_generation_routes_have_same_request_timestamps(self):
        for function in range(3):
            with self.subTest(function=function): self.run_tts(function)

    def test_missing_alignment_keeps_audio_and_never_sends_another_request(self):
        self.run_tts(2,alignment=False)

    def test_sidecar_failure_does_not_repeat_paid_tts_or_lose_audio(self):
        self.run_tts(2,save_error=True)

    def test_long_tts_offsets_follow_decoded_samples_not_mp3_container_padding(self):
        from model.AudioHelper import CreateAudioLatest
        client=MagicMock()
        def speech(**kwargs):
            text=kwargs['inputs'][0].text
            return dict(audio_base64=base64.b64encode(self.audio).decode(),alignment=aligned(text))
        client.text_to_dialogue.convert_with_timestamps.side_effect=speech
        with tempfile.TemporaryDirectory() as directory, patch('model.AudioHelper.ElevenLabs',return_value=client), \
                patch('model.AudioHelper._speech_ffmpeg',return_value=self.ffmpeg):
            target=Path(directory)/'task_audio.mp3'; text=('slovo '*500).strip()
            self.assertTrue(CreateAudioLatest(text,str(target),'voice',api_keys=['dummy'],api_key_status_config=None))
            words=load_timings(target,text)
            self.assertEqual(len(words),500)
            self.assertAlmostEqual(words[-1]['end'],1.9,delta=.01)
            self.assertEqual(client.text_to_dialogue.convert_with_timestamps.call_count,2)
            self.assertFalse(list(Path(directory).glob('.ele-audio-*')))


class ForcedAlignmentTests(unittest.TestCase):
    def setup_service(self,directory):
        service=ElevenLabsService(); config=Path(directory)/'config.json'
        config.write_text('{}',encoding='utf8')
        token=service.bind(dict(elevenlabs_api_keys=['dummy1','dummy2'],
            elevenlabs_alignment_key_id=api_key_id('dummy2')),config)
        audio=Path(directory)/'task_audio.mp3'; audio.write_bytes(b'fixture')
        return service,token,audio,config

    def test_selected_key_only_and_persistent_permission_denial_does_not_disable_tts(self):
        class Denied(Exception):
            status_code=403
            body={'detail':{'status':'missing_permissions'}}
        client=MagicMock(); client.forced_alignment.create.side_effect=Denied()
        with tempfile.TemporaryDirectory() as directory:
            service,token,audio,config=self.setup_service(directory)
            with patch('elevenlabs.client.ElevenLabs',return_value=client) as factory:
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError,'权限'): service.forced_align(token,audio,TEXT,1.)
            self.assertEqual(factory.call_count,1)
            self.assertEqual(factory.call_args.kwargs['api_key'],'dummy2')
            self.assertEqual(client.forced_alignment.create.call_count,1)
            self.assertEqual(set(service.tts_credentials(token)[0]),{'dummy1','dummy2'})
            status=json.loads(config.read_text('utf8'))['elevenlabs_api_key_statuses'][api_key_id('dummy2')]
            self.assertEqual(status['forced_alignment_status'],'permission_denied')
            self.assertNotEqual(status.get('status'),'invalid')

    def test_success_cached_and_no_original_srt_changes(self):
        requests=[]
        def transport(request):
            requests.append(request)
            return httpx.Response(200,json={'words':[dict(w,loss=0.) for w in WORDS], 'characters':[], 'loss':0.})
        with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(transport)) as http:
            service,token,audio,_config=self.setup_service(directory)
            original=audio.with_suffix('.srt'); original.write_bytes(b'DaVinci original')
            with patch('elevenlabs.client.ElevenLabs',return_value=ElevenLabs(api_key='dummy2',httpx_client=http)):
                self.assertEqual(service.forced_align(token,audio,TEXT,1.),WORDS)
                self.assertEqual(service.forced_align(token,audio,TEXT,1.),WORDS)
            self.assertEqual(len(requests),1)
            self.assertEqual(requests[0].url.path,'/v1/forced-alignment')
            self.assertEqual(original.read_bytes(),b'DaVinci original')

    def test_two_identical_jobs_upload_only_once(self):
        client=MagicMock()
        def response(**kwargs):
            time.sleep(.1)
            return dict(words=WORDS)
        client.forced_alignment.create.side_effect=response
        with tempfile.TemporaryDirectory() as directory:
            service,token,audio,_config=self.setup_service(directory)
            with patch('elevenlabs.client.ElevenLabs',return_value=client), ThreadPoolExecutor(2) as pool:
                futures=[pool.submit(service.forced_align,token,audio,TEXT,1.) for _ in range(2)]
                self.assertEqual([f.result() for f in futures],[WORDS,WORDS])
            self.assertEqual(client.forced_alignment.create.call_count,1)
            self.assertEqual(service._input_locks,{})

    def test_no_designated_key_no_upload_or_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,audio,config=self.setup_service(directory)
            token=service.bind({'elevenlabs_api_keys':['dummy1','dummy2']},config)
            with patch('elevenlabs.client.ElevenLabs') as factory:
                with self.assertRaisesRegex(ValueError,'指定'): service.forced_align(token,audio,TEXT,1.)
            factory.assert_not_called()

    def test_offline_and_cancel_prevent_http(self):
        from model.OfflineMode import OfflineModeError
        with tempfile.TemporaryDirectory() as directory:
            service,token,audio,_config=self.setup_service(directory)
            cancel=threading.Event(); cancel.set()
            with patch('elevenlabs.client.ElevenLabs') as factory:
                with self.assertRaises(InterruptedError): service.forced_align(token,audio,TEXT,1.,cancel=cancel)
                with patch('model.OfflineMode.require_online',side_effect=OfflineModeError('offline')):
                    with self.assertRaises(OfflineModeError): service.forced_align(token,audio,TEXT,1.)
            factory.assert_not_called()

    def test_changed_audio_during_request_does_not_publish_stale_subtitles(self):
        client=MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            service,token,audio,_config=self.setup_service(directory)
            def response(**kwargs):
                # Same open handle can coexist with writing in the mocked call.
                audio.write_bytes(b'changed audio')
                return dict(words=WORDS)
            client.forced_alignment.create.side_effect=response
            with patch('elevenlabs.client.ElevenLabs',return_value=client):
                with self.assertRaisesRegex(ValueError,'改变'): service.forced_align(token,audio,TEXT,1.)
            self.assertFalse(list(Path(directory).glob('*[[]动态*')))


class CaptionReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_preview_worker_keeps_actual_voice_and_reuses_dynamic_pair(self):
        from app_plugins.builtin.batch_text_video.playback import PlaybackWorker,preview_key
        from app_plugins.builtin.batch_text_video.store import fresh_state
        from app_plugins.builtin.batch_text_video.captions import new_caption,task_word_timings
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); audio=root/'task_audio.mp3'; audio.write_bytes(b'fixture')
            video=root/'video.mp4'; video.touch()
            state=fresh_state(); state['settings'].update(use_voice=True,layers=[new_caption()])
            job=dict(path=str(video),task_dir=directory,name='test',title='',body=TEXT,
                voice_path=str(audio),voice_override=True)
            before=preview_key(job,state)
            save_timings(audio,WORDS,1.,source='test')
            after=preview_key(job,state)
            self.assertNotEqual(before,after)
            result=[]
            worker=PlaybackWorker(job,state,root/'store',after,preview_limit=10)
            worker.ready.connect(result.append)
            def renderer(active,current,*args,**kwargs):
                words=task_word_timings(active,active['voice_path'],current['settings']['layers'][0],
                    '',TEXT,1.,directory)
                self.assertEqual(words,WORDS)
                output=Path(active['task_dir'])/(active['name']+'.mp4'); output.write_bytes(b'proxy')
                return output,{}
            with patch(PREFIX+'playback.resolve_tools',return_value=('ffmpeg','ffprobe')), \
                    patch(PREFIX+'playback.probe',return_value={'duration':1.}), \
                    patch(PREFIX+'playback.render_static_text',side_effect=renderer), \
                    patch(PREFIX+'captions.load_word_timings',side_effect=AssertionError('local ASR')):
                worker.run()
            self.assertNotIn('error',result[0])

    def test_batch_auto_reuses_dynamic_words_without_local_or_cloud_alignment(self):
        from app_plugins.builtin.batch_text_video.captions import new_caption,task_word_timings
        with tempfile.TemporaryDirectory() as directory:
            audio=Path(directory)/'task_audio.mp3'; audio.write_bytes(b'fixture')
            save_timings(audio,WORDS,1.,source='test')
            with patch(PREFIX+'captions.load_word_timings',side_effect=AssertionError('local ASR')), \
                    patch('model.ElevenLabsService.forced_align',side_effect=AssertionError('cloud request')):
                result=task_word_timings({},str(audio),new_caption(),'',TEXT,1.,directory)
            self.assertEqual(result,WORDS)

    def test_explicit_cloud_requires_consent_and_host_binding(self):
        from app_plugins.builtin.batch_text_video.captions import new_caption,task_word_timings
        layer=new_caption(); layer['caption_timing']='elevenlabs'
        with tempfile.TemporaryDirectory() as directory:
            audio=Path(directory)/'task_audio.mp3'; audio.write_bytes(b'fixture')
            with self.assertRaisesRegex(ValueError,'确认允许'):
                task_word_timings({},str(audio),layer,'',TEXT,1.,directory)
            layer['caption_cloud_consent']=True
            with alignment_session('opaque'), patch('model.ElevenLabsService.forced_align',return_value=WORDS) as cloud:
                self.assertEqual(task_word_timings({},str(audio),layer,'',TEXT,1.,directory),WORDS)
            self.assertEqual(cloud.call_args.args[0],'opaque')

    def test_ordinary_srt_uses_existing_formatting_without_loading_model(self):
        from app_plugins.builtin.task_audio_subtitle import TaskAudioSubtitlePlugin
        plugin=TaskAudioSubtitlePlugin()
        plugin.context=SimpleNamespace(whisper_model=MagicMock(side_effect=AssertionError('local model')))
        task=SimpleNamespace(task_audio_text=TEXT)
        with tempfile.TemporaryDirectory() as directory:
            audio=Path(directory)/'task_audio.mp3'; audio.write_bytes(b'fixture')
            dynamic=save_timings(audio,WORDS,1.,source='test'); before=dynamic.read_bytes()
            result,original=plugin._create_subtitle(audio,task,'sk',{'subtitle_max_words_per_block':2})
            self.assertEqual(result,'generated')
            self.assertIn('Chlieb',original.read_text('utf8'))
            self.assertIn('náš',original.read_text('utf8'))
            old=original.read_bytes()
            result,_=plugin._create_subtitle(audio,task,'sk',{'overwrite_existing_subtitles':True})
            self.assertEqual(result,'existing')
            self.assertEqual(original.read_bytes(),old)
            self.assertEqual(dynamic.read_bytes(),before)
            plugin.context.whisper_model.assert_not_called()

    def test_exclusive_subtitle_publication_preserves_existing(self):
        from model.SubtitleHelper import generate_srt_from_word_timings
        with tempfile.TemporaryDirectory() as directory:
            original=Path(directory)/'task_audio.srt'; original.write_bytes(b'old')
            self.assertFalse(generate_srt_from_word_timings(WORDS,original))
            self.assertEqual(original.read_bytes(),b'old')

    def test_main_legacy_subtitle_entry_also_reuses_timestamp_without_model(self):
        from PYUI.main_pyui import MainDialog
        from unittest.mock import Mock
        window=SimpleNamespace(subtitle_gap_ms_spinbox=SimpleNamespace(value=lambda:-1),
            subtitle_line_break_checkbox=SimpleNamespace(isChecked=lambda:False),
            subtitle_max_words_spinbox=SimpleNamespace(value=lambda:2),appendLog=Mock(),app_logger=Mock())
        with tempfile.TemporaryDirectory() as directory:
            audio=Path(directory)/'task_audio.mp3'; audio.write_bytes(b'fixture')
            save_timings(audio,WORDS,1.,source='test')
            with patch('PYUI.main_pyui.globalValue.get_whisper_model',side_effect=AssertionError('local model')):
                MainDialog.genSrt(window,audio,SimpleNamespace(task_audio_text=TEXT))
            self.assertTrue(audio.with_suffix('.srt').is_file())
            window.app_logger.exception.assert_not_called()


class HostCredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_host_binding_and_plugin_call_contain_no_raw_keys(self):
        from app_plugins.host import PluginHost
        from app_plugins.builtin.task_audio_subtitle import TaskAudioSubtitlePlugin
        config={'elevenlabs_api_keys':['synthetic-secret-not-real']}
        window=SimpleNamespace(config_name='',load_config=lambda:copy.deepcopy(config))
        host=SimpleNamespace(_elevenlabs=None,main_window=window)
        binding=PluginHost.prepare_elevenlabs(host)
        self.assertNotIn(config['elevenlabs_api_keys'][0],json.dumps({'elevenlabs_binding':binding}))
        plugin=TaskAudioSubtitlePlugin()
        plugin.context=SimpleNamespace(elevenlabs_binding=binding,parent_widget=window)
        task=SimpleNamespace(task_audio_voice_id='voice',task_audio_type='',task_audio_text=TEXT,task_name='test')
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'out.mp3'; target.touch()
            with patch('app_plugins.builtin.task_audio_subtitle.CreateAudioLatest',return_value=True) as create:
                self.assertTrue(plugin._create_audio(task,target,lambda *_:None))
            self.assertEqual(create.call_args.kwargs['api_keys'],[])
            self.assertEqual(create.call_args.kwargs['api_session'],binding)

    def test_status_updates_reuse_session_and_legacy_key_is_preserved(self):
        service=ElevenLabsService()
        config={'elevenlabs_api_keys':None,'elevenlabs_api_key':'dummy-legacy'}
        token=service.bind(config,'')
        self.assertEqual(service.tts_credentials(token)[0],['dummy-legacy'])
        for i in range(300):
            config['elevenlabs_api_key_statuses']={api_key_id('dummy-legacy'):{'last_success_unix':i}}
            self.assertEqual(service.bind(config,''),token)

    def test_main_settings_selects_only_fingerprint_and_keeps_key_list(self):
        from qt_compat import QtCore
        from PYUI.main_setting_pyui import MainSettingDialog
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.json'
            config.write_text(json.dumps({'elevenlabs_api_keys':['dummy1','dummy2'],
                ALIGNMENT_KEY:api_key_id('dummy2')}),encoding='utf8')
            with patch('PYUI.main_setting_pyui.CONFIG_PATH',config):
                dialog=MainSettingDialog()
                try:
                    self.assertEqual(dialog.elevenlabs_alignment_key.currentData(),api_key_id('dummy2'))
                    self.assertEqual(dialog.get_config()[ALIGNMENT_KEY],api_key_id('dummy2'))
                    self.assertEqual(dialog.get_api_keys(),['dummy1','dummy2'])
                finally:
                    dialog.deleteLater()
                    QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)


if __name__=='__main__': unittest.main()
