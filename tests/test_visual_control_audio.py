import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.audio_sources import voice_library, voice_choices, resolve_voice
from app_plugins.builtin.batch_text_video.custom_pools import create_pool, add_pool_paths
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.visual_media import DEFAULTS, normalize_visual
from app_plugins.builtin.batch_text_video.visual_audio import AudioInput, AudioFeed, append, RATE

FF=os.environ.get('BATCH_TEST_FFMPEG') or os.environ.get('NODE_FFMPEG')


class VisualAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_defaults_validation_and_independent_volume_ui(self):
        from app_plugins.builtin.batch_text_video.visual_media import VisualProperties
        layer=dict(kind='image',path='test.mov')
        normalize_visual(layer)
        self.assertFalse(layer['media_audio_enabled'])
        for field,value in [('media_audio_enabled',1),('media_audio_volume',float('nan')),('media_audio_volume',201)]:
            with self.assertRaises(ValueError): normalize_visual(dict(layer,**{field:value}))
        editor=VisualProperties();editor.set_layer(layer)
        self.assertEqual(editor.controls['media_audio_enabled'].text(),'使用素材音频')
        editor.controls['media_audio_enabled'].setChecked(True)
        editor.controls['media_audio_volume'].setValue(37)
        self.assertEqual(editor.layer['media_audio_volume'],37)
        self.assertTrue(editor.layer['media_audio_enabled'])
        if os.environ.get('LZX_POOL_ORDER_QA'):
            editor.resize(480,700)
            editor.grab().save(str(Path(os.environ['LZX_POOL_ORDER_QA'])/'video-audio-ui.png'))
        editor.deleteLater()

    def test_voice_can_use_legacy_and_mixed_video_pools_and_filters_still_images(self):
        with tempfile.TemporaryDirectory() as root:
            state=fresh_state();paths=[]
            for name in ('a.png','b.mov','c.gif','d.mp4'):
                path=Path(root)/name;path.touch();paths.append(str(path))
            state['backgrounds']=paths
            for kind in ('image','video','visual'):
                pool=create_pool(state,kind,kind);self.assertEqual(add_pool_paths(pool,paths),4)
                _,items=voice_library(state,pool['id'])
                self.assertEqual([i['path'] for i in items],[paths[1],paths[3]])
                state['settings'].update(use_voice=True,voice_pool_mode='rotate',voice_pool_id=pool['id'])
                state['jobs']=[dict(id='one'),dict(id='two')]
                self.assertEqual(resolve_voice(state['jobs'][1],state),paths[3])
                from app_plugins.builtin.batch_text_video.audio_ui import AudioSourceCard
                card=AudioSourceCard('voice',state)
                self.assertGreaterEqual(card.pool.findData(pool['id']),0)
                self.assertIn('2 条',card.info.text())
                card.deleteLater()
            self.assertIn('builtin:background',dict(voice_choices(state)))
            state['settings']['voice_pool_id']='builtin:background'
            self.assertEqual(resolve_voice(state['jobs'][0],state),paths[1])

    def test_same_voice_file_is_not_mixed_twice_and_silent_inputs_are_skipped(self):
        layer=dict(DEFAULTS,kind='image',path='a.mov',media_audio_enabled=True)
        with patch('app_plugins.builtin.batch_text_video.engine.probe',side_effect=AssertionError('No duplicate decode')):
            self.assertEqual(append([],[],[(layer,None,0)],3,2,'ff','probe',voice_path='a.mov'),(3,[]))
        with patch('app_plugins.builtin.batch_text_video.engine.probe',return_value={'audio':False}):
            args=[];filters=[]
            self.assertEqual(append(args,filters,[(layer,None,0)],3,2,'ff','probe'),(3,[]))
            self.assertEqual(args,[])

    def test_cancelled_feed_never_starts_a_decoder(self):
        from app_plugins.builtin.batch_text_video.matting_models import MattingCancelled
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'video.mov';path.write_bytes(b'fixture')
            feed=AudioFeed(AudioInput(str(path),2,source_end=1,ffmpeg='ffmpeg'))
            feed.cancel.set()
            try:
                with patch('subprocess.Popen',side_effect=AssertionError('Decoder started after cancel')):
                    with self.assertRaises(MattingCancelled): next(feed.frames())
            finally: feed.close()

    def test_cancelled_feed_never_starts_a_decoder(self):
        from app_plugins.builtin.batch_text_video.matting_models import MattingCancelled
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'video.mov';path.write_bytes(b'fixture')
            feed=AudioFeed(AudioInput(str(path),2,source_end=1,ffmpeg='ffmpeg'))
            feed.cancel.set()
            try:
                with patch('subprocess.Popen',side_effect=AssertionError('Decoder started after cancel')):
                    with self.assertRaises(MattingCancelled): next(feed.frames())
            finally: feed.close()

    @unittest.skipUnless(FF,'real FFmpeg required')
    def test_bounded_loop_crossfade_matches_video_clock_and_skips_intro_on_repeats(self):
        # Synthetic PCM makes intro/loop and exact overlap samples observable.
        class Feed(AudioFeed):
            def decode(self,start,duration):
                times=np.arange(round(start*RATE),round((start+duration)*RATE))/RATE
                samples=np.where(times<.2, .1, np.where(times<.6,.4,.8)).astype(np.float32)
                values=np.repeat(samples[:,None],2,axis=1)
                for i in range(0,len(values),4096): yield values[i:i+4096].tobytes()
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'source.mov';path.write_bytes(b'fixture')
            spec=AudioInput(str(path),2,source_end=.8,loop_start=.2,loop=True,overlap=.1,ffmpeg=FF)
            feed=Feed(spec)
            try: samples=np.frombuffer(b''.join(feed.frames()),np.float32).reshape(-1,2)
            finally: feed.close()
            self.assertEqual(len(samples),2*RATE)
            self.assertAlmostEqual(float(samples[1000,0]),.1,places=5)
            self.assertTrue(np.all(samples[round(.8*RATE):,0]>.35))
            self.assertAlmostEqual(float(samples[round(.75*RATE),0]),.6,delta=.01)


@unittest.skipUnless(FF,'real FFmpeg required')
class RealVisualAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.video=self.root/'中文带声音.mov'
        self.run_ff('-f','lavfi','-i','color=red:s=96x96:r=10:d=0.8','-f','lavfi','-i',
            'sine=frequency=440:sample_rate=48000:duration=0.8','-c:v','qtrle','-c:a','pcm_s16le',str(self.video))

    def tearDown(self): self.temp.cleanup()

    def run_ff(self,*args):
        result=subprocess.run([FF,'-nostdin','-v','error','-y',*args],capture_output=True,timeout=45,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8',errors='replace'))
        return result.stdout

    def state(self,enabled=True,volume=100,**extra):
        from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS
        state=fresh_state()
        layer=dict(id='visual',kind='image',name='video',path=str(self.video),source='fixed',enabled=True,
            opacity=100,x=50,y=50,width=100,height=100,media_audio_enabled=enabled,media_audio_volume=volume,
            use_saved_matte=False,matting_enabled=False,**extra)
        state['settings'].update(aspect='custom',canvas_width=128,canvas_height=128,music_enabled=False,use_voice=False,
            overwrite=True,darkness=0,margin_top=0,margin_bottom=0,margin_x=0,video_encoder='cpu',ffmpeg_path=FF,output_dir=str(self.root/'out'),
            unvoiced_duration_mode='fixed',unvoiced_duration_seconds=2,
            layers=normalize_layers([layer]+[dict(d,enabled=False) for d in DEFAULT_LAYERS]))
        job=dict(id='job',path='',name='test',title='',body='',voice_path='',voice_override=True)
        state['jobs']=[job]
        return state,job

    def render(self,enabled=True,volume=100,**extra):
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        state,job=self.state(enabled,volume,**extra);before=copy.deepcopy(state)
        output,_=render_static_text(job,state,self.root/'cache')
        self.assertEqual(state,before)
        return output,state,job

    def samples(self,path):
        raw=self.run_ff('-i',str(path),'-vn','-ac','1','-ar',str(RATE),'-f','f32le','pipe:1')
        return np.frombuffer(raw,np.float32)

    def test_real_export_off_on_volume_loop_and_no_video_caches(self):
        from app_plugins.builtin.batch_text_video.engine import probe, resolve_tools
        _,fp=resolve_tools(FF)
        output,_,_=self.render(False)
        self.assertFalse(probe(output,fp)['audio'])
        output,_,_=self.render(True)
        full=self.samples(output).copy()
        self.assertGreater(float(np.std(full[round(1.2*RATE):round(1.8*RATE)])),.03)
        output,_,_=self.render(True,50)
        quiet=self.samples(output)
        self.assertAlmostEqual(float(np.std(quiet)/np.std(full)),.5,delta=.035)
        self.assertFalse(list((self.root/'cache').rglob('*.mov')))

    def test_non_loop_stops_audio_when_source_finishes_and_timing_window_is_respected(self):
        output,_,_=self.render(True,media_loop=False)
        samples=self.samples(output)
        self.assertLess(float(np.max(np.abs(samples[round(1.1*RATE):round(1.8*RATE)]))),.001)
        timing=dict(start=.5,end=1.5,enter='cut',exit='cut',clock='restart')
        output,_,_=self.render(True,visual_timing_enabled=True,visual_timing=timing)
        samples=self.samples(output)
        self.assertLess(float(np.max(np.abs(samples[:round(.4*RATE)]))),.001)
        self.assertGreater(float(np.std(samples[round(.6*RATE):round(1.4*RATE)])),.03)
        self.assertLess(float(np.max(np.abs(samples[round(1.6*RATE):round(1.9*RATE)]))),.001)

    def test_video_pool_voice_without_background_and_playback_preview_include_audio(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text,probe,resolve_tools
        from app_plugins.builtin.batch_text_video.playback import preview_key,PlaybackWorker
        state,job=self.state(False)
        pool=create_pool(state,'人物视频','visual');add_pool_paths(pool,[self.video])
        state['settings'].update(use_voice=True,voice_pool_mode='single',voice_pool_id=pool['id'])
        job['voice_override']=False
        output,_=render_static_text(job,state,self.root/'voice-cache')
        _,fp=resolve_tools(FF);info=probe(output,fp)
        self.assertTrue(info['audio']);self.assertAlmostEqual(info['duration'],.8,delta=.15)
        # Voice override is intentional in state(); use the pool for this run.
        state,job=self.state(True)
        result=[];worker=PlaybackWorker(job,state,self.root/'preview',preview_key(job,state))
        worker.ready.connect(result.append);worker.run();worker.deleteLater()
        self.assertEqual(len(result),1)
        self.assertNotIn('error',result[0],result)
        self.assertTrue(probe(result[0]['path'],fp)['audio'])

    def test_real_loop_region_with_crossfade_does_not_repeat_intro_or_audio_after_B(self):
        from app_plugins.builtin.batch_text_video.visual_loop_region import stamp
        self.run_ff('-f','lavfi','-i','color=red:s=96x96:r=10:d=0.8','-f','lavfi','-i',
            'aevalsrc=0.12*sin(2*PI*if(lt(t\\,0.2)\\,220\\,if(lt(t\\,0.7)\\,440\\,880))*t):s=48000:d=0.8',
            '-c:v','qtrle','-c:a','pcm_s16le',str(self.video))
        rule=dict(path=str(self.video),start=.2,end=.7,duration=.8,**stamp(self.video))
        output,_,_=self.render(True,media_loop_transition='fade',media_loop_fade=.1,media_loop_regions=[rule])
        data=self.samples(output)
        def peak(a,b):
            clip=data[round(a*RATE):round(b*RATE)]
            spectrum=np.abs(np.fft.rfft(clip*np.hanning(len(clip))))
            return np.argmax(spectrum)*RATE/len(clip)
        self.assertAlmostEqual(peak(.03,.17),220,delta=10)
        for a,b in ((.25,.45),(.8,1.0),(1.5,1.7)):
            self.assertAlmostEqual(peak(a,b),440,delta=10)

    def test_real_loop_region_with_crossfade_does_not_repeat_intro_or_audio_after_B(self):
        from app_plugins.builtin.batch_text_video.visual_loop_region import stamp
        self.run_ff('-f','lavfi','-i','color=red:s=96x96:r=10:d=0.8','-f','lavfi','-i',
            'aevalsrc=0.12*sin(2*PI*if(lt(t\\,0.2)\\,220\\,if(lt(t\\,0.7)\\,440\\,880))*t):s=48000:d=0.8',
            '-c:v','qtrle','-c:a','pcm_s16le',str(self.video))
        rule=dict(path=str(self.video),start=.2,end=.7,duration=.8,**stamp(self.video))
        output,_,_=self.render(True,media_loop_transition='fade',media_loop_fade=.1,media_loop_regions=[rule])
        data=self.samples(output)
        def peak(a,b):
            clip=data[round(a*RATE):round(b*RATE)]
            spectrum=np.abs(np.fft.rfft(clip*np.hanning(len(clip))))
            return np.argmax(spectrum)*RATE/len(clip)
        self.assertAlmostEqual(peak(.03,.17),220,delta=10)
        for a,b in ((.25,.45),(.8,1.0),(1.5,1.7)):
            self.assertAlmostEqual(peak(a,b),440,delta=10)


if __name__=='__main__': unittest.main()
