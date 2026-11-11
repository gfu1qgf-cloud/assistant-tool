"""Mid-stream colour/geometry changes must not restart a shared render clock."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from app_plugins.builtin.batch_text_video.engine import _run, stable_video_inputs, probe

FF=os.environ.get('NODE_FFMPEG',''); FP=str(Path(FF).with_name('ffprobe.exe')) if FF else ''


class InputOptionsTests(unittest.TestCase):
    def test_video_only_option_is_attached_to_every_input_without_mutating_the_plan(self):
        args=['ffmpeg','-f','lavfi','-i','color=black','-stream_loop','-1','-i','source.mp4',
              '-i','voice.wav','-/filter_complex','render.filter','-map','[v]','out.mp4']
        before=args[:];actual=stable_video_inputs(args)
        self.assertEqual(args,before)
        self.assertEqual(actual.count('-reinit_filter:v'),3)
        for i,value in enumerate(actual):
            if value=='-i':self.assertEqual(actual[i-2:i],['-reinit_filter:v','0'])
        self.assertEqual(actual[-5:],args[-5:])

    def test_unfiltered_commands_and_non_ffmpeg_test_children_are_untouched(self):
        for args in (['ffmpeg','-i','source.wav','-c','copy','out.wav'],
                     ['python','-c','print(1)','-progress','pipe:1']):
            self.assertIs(stable_video_inputs(args),args)


@unittest.skipUnless(FF and Path(FF).is_file(),'explicit local FFmpeg required')
class ReinitClockTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def command(self,*args):
        result=subprocess.run([FF,'-nostdin','-v','error',*map(str,args)],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        return result.stdout

    def source(self,geometry=False):
        chunks=[]
        for number,(color,size,matrix,trc) in enumerate((('red','64x96','bt709','bt709'),
                ('blue','80x120' if geometry else '64x96','smpte170m','iec61966-2-1'))):
            path=self.root/f'{number}.h264';chunks.append(path)
            self.command('-f','lavfi','-i',f'color={color}:s={size}:r=24:d=2',
                '-c:v','libx264','-threads','1','-preset','ultrafast','-color_range','tv',
                '-colorspace',matrix,'-color_primaries','bt709','-color_trc',trc,'-f','h264',path)
        raw=self.root/'combined.h264';raw.write_bytes(b''.join(path.read_bytes() for path in chunks))
        path=self.root/'source.mp4';self.command('-r','24','-i',raw,'-c:v','copy',path)
        frames=json.loads(subprocess.check_output([FP,'-v','error','-threads','1','-select_streams','v:0',
            '-show_frames','-show_entries','frame=width,height,color_space,color_transfer','-of','json',str(path)]))['frames']
        self.assertEqual(len(frames),96)
        self.assertNotEqual(frames[0]['color_space'],frames[48]['color_space'])
        self.assertEqual(frames[48]['width'],80 if geometry else 64)
        return path

    def render(self,geometry=False,preview=False,original_audio=False):
        source=self.source(geometry);output=self.root/'output.mp4';duration=3.5 if preview else 8.
        args=[FF,'-y','-nostdin','-v','error','-progress','pipe:1','-filter_complex_threads','1',
              '-f','lavfi','-i','color=black:s=64x96:r=30:d=8','-threads','1','-stream_loop','-1','-i',str(source),
              '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=8']
        graph="[1:v]setpts=PTS-STARTPTS,fps=30,format=rgba,scale=64:96[fg];[0:v][fg]overlay=repeatlast=0:format=auto,drawbox=x=0:y=0:w=8:h=8:color=lime:t=fill:enable='between(t,3,3.3)',format=yuv420p[v];[2:a]asetpts=PTS-STARTPTS,apad=whole_dur=8,atrim=duration=8[a]"
        script=self.root/'render.filter';script.write_text(graph,encoding='utf8')
        args+=['-/filter_complex',str(script),'-map','[v]','-map','[a]','-t',str(duration),
               '-c:v','libx264','-threads','1','-preset','ultrafast','-crf','15','-c:a','aac',str(output)]
        _run(args,threading.Event(),None,duration,timeout=15)
        info=probe(output,FP);self.assertAlmostEqual(info['duration'],duration,delta=.06);self.assertTrue(info['audio'])
        self.assertEqual(int(info['video']['nb_frames']),round(duration*30))
        def pixel(at,x=32,y=48):
            data=self.command('-ss',at,'-i',output,'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1')
            return tuple(data[(y*64+x)*3:(y*64+x)*3+3])
        red,blue=pixel(.5),pixel(2.5)
        self.assertGreater(red[0],150);self.assertLess(red[2],40)
        self.assertGreater(blue[2],150);self.assertLess(blue[0],40)
        marker=pixel(3.1,3,3);self.assertGreater(marker[1],150)
        if not preview:
            self.assertGreater(pixel(4.5)[0],150)
            self.assertGreater(pixel(6.5)[2],150)

    def test_color_change_preserves_full_video_audio_animation_and_loop_clock(self):self.render()
    def test_dimension_and_color_change_remain_scaled_to_the_canvas(self):self.render(geometry=True)
    def test_limited_playback_preview_crosses_the_change_without_resetting(self):self.render(preview=True)


@unittest.skipUnless(os.environ.get('LZX_REINIT_REAL'),'explicit read-only reported fixtures required')
class ReportedSourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def render(self,number):
        from app_plugins.builtin.batch_text_video.engine import prepare_static_text,resolve_tools
        from app_plugins.builtin.batch_text_video.store import fresh_state
        fixture=json.loads((Path(os.environ['LZX_STALL_QA'])/f'failed-job-{number}.json').read_text(encoding='utf8'))
        before=copy.deepcopy(fixture)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=fresh_state();state['settings'].update(copy.deepcopy(fixture['settings']))
            state['settings'].update(video_encoder='cpu',export_workers=1,aspect='custom',canvas_width=128,canvas_height=192,
                margin_x=0,margin_top=0,margin_bottom=0,output_destination='directory',output_dir=str(root/'out'),
                keep_media_cache=False,ffmpeg_path=FF)
            for layer in state['settings']['layers']:
                if layer['kind']!='image':layer['enabled']=False
                layer['media_loop_transition']='none'
            job=copy.deepcopy(fixture['job']);job.update(name='reported-'+number,output='');state['jobs']=[job]
            with prepare_static_text(job,state,root/'private',threading.Event()) as prepared:
                args=list(prepared.args);args[1:1]=['-filter_complex_threads','1','-filter_threads','1']
                bounded=[]
                for value in args:
                    if value=='-i':bounded+=['-threads','1']
                    bounded.append(value)
                _run(bounded,threading.Event(),None,prepared.duration,timeout=120,cwd=prepared.cwd)
                _,fp=resolve_tools(FF);actual=probe(prepared.target,fp)
                self.assertAlmostEqual(actual['duration'],prepared.duration,delta=.12)
                self.assertTrue(actual['audio'])
            self.assertEqual(fixture,before)

    def test_original_247_native_path_completes(self):self.render('247')
    def test_original_253_native_path_completes(self):self.render('253')
