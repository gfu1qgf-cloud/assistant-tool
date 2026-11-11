"""Selected dissolve keeps one decode path, including a partial first pass."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.visual_media import add_plane,normalize_visual
from app_plugins.builtin.batch_text_video.visual_loop import LoopInput

FF=os.environ.get('NODE_FFMPEG','')


@unittest.skipUnless(FF,'explicit local FFmpeg required')
class LoopCompatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.path=self.root/'source.mov'
        subprocess.run([FF,'-nostdin','-v','error','-f','lavfi','-i','color=red:s=32x48:r=25:d=1',
            '-c:v','qtrle',str(self.path)],check=True,capture_output=True,timeout=15)

    def tearDown(self):self.temp.cleanup()

    def inputs(self,mode,duration):
        layer=dict(kind='image',path=str(self.path),x=50,y=50,width=100,height=100,opacity=100,
            media_loop=True,media_loop_transition=mode,media_loop_fade=.2,matting_enabled=False)
        normalize_visual(layer);args=[FF];filters=[]
        add_plane(args,filters,layer,0,'background','visual',128,192,duration,25,ffmpeg=FF)
        return args

    def test_partial_first_pass_dissolve_uses_stream_without_overlap(self):
        values=[v for v in self.inputs('fade',.5) if isinstance(v,LoopInput)]
        self.assertEqual(len(values),1)
        self.assertEqual(values[0].overlap,0)

    def test_actual_repeat_retains_overlap_and_direct_mode_stays_native(self):
        self.assertTrue(any(isinstance(v,LoopInput) and v.overlap>0 for v in self.inputs('fade',1.4)))
        self.assertFalse(any(isinstance(v,LoopInput) for v in self.inputs('none',.5)))


@unittest.skipUnless(os.environ.get('LZX_STALL_REAL'),'read-only reported source fixtures required')
class ReportedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def render(self,number):
        from app_plugins.builtin.batch_text_video.store import fresh_state
        from app_plugins.builtin.batch_text_video.engine import render_static_text,probe,resolve_tools
        fixture=json.loads((Path(os.environ['LZX_STALL_QA'])/f'failed-job-{number}.json').read_text(encoding='utf8'))
        before=copy.deepcopy(fixture)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=fresh_state();state['settings'].update(copy.deepcopy(fixture['settings']))
            settings=state['settings'];settings.update(video_encoder='cpu',export_workers=1,aspect='custom',
                canvas_width=128,canvas_height=192,margin_x=0,margin_top=0,margin_bottom=0,
                output_destination='directory',output_dir=str(root/'outputs'),keep_media_cache=False,ffmpeg_path=FF)
            for layer in settings['layers']:
                if layer['kind']!='image':layer['enabled']=False
                if layer.get('pool_id')=='6cb21304f10a48f08c9b3db5b207dde7':
                    layer.update(media_loop=True,media_loop_transition='fade',media_loop_fade=.3)
            job=copy.deepcopy(fixture['job']);job.update(name='reported-'+number,output='')
            state['jobs']=[job]
            output,_=render_static_text(job,state,root/'private',threading.Event())
            _,fp=resolve_tools(FF);actual=probe(output,fp)
            voice=probe(job['render_assignment']['voice'],fp,audio_only=True)
            self.assertAlmostEqual(actual['duration'],voice['duration'],delta=.12)
            self.assertTrue(actual['audio'])
            self.assertEqual(fixture,before)

    def test_247_passes_the_reported_stall_and_keeps_full_duration(self):self.render('247')
    def test_253_shorter_than_source_also_uses_compatible_decode(self):self.render('253')
