"""Background A/B starts on first use, stays project-owned, survives loops."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import engine, background_trim as trim
from app_plugins.builtin.batch_text_video.visual_loop_region import stamp
from app_plugins.builtin.batch_text_video.visual_loop_region_ui import LoopRegionDialog
from app_plugins.builtin.batch_text_video.image_background import probe_background
from app_plugins.builtin.batch_text_video.matting_live import Inputs
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import PreviewWorker, BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.layout import ensure_fonts

FF=os.environ.get('NODE_FFMPEG','')


class BackgroundTrimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([]);ensure_fonts()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def ff(self,args,data=None):
        result=subprocess.run([FF,'-nostdin','-v','error','-filter_complex_threads','1',*args],input=data,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'));return result.stdout

    def fixture(self):
        path=self.root/"背景's 原片.mp4"
        rgb=np.zeros((30,180,240,3),np.uint8)
        rgb[:10]=[220,20,20];rgb[10:20]=[20,220,20];rgb[20:]=[20,20,220]
        self.ff(['-y','-f','rawvideo','-pix_fmt','rgb24','-s','240x180','-r','10','-i','pipe:0',
                 '-f','lavfi','-i','aevalsrc=sin(2*PI*(if(lt(t\,1)\,200\,if(lt(t\,2)\,800\,1600)))*t):s=48000:d=3',
                 '-c:v','libx264','-g','100','-bf','3','-crf','8','-pix_fmt','yuv420p','-c:a','aac',str(path)],rgb.tobytes())
        rule=dict(path=str(path),start=1.,end=2.,duration=3.,**stamp(path))
        return path,rule

    def state(self,path,rule):
        clear=QtGui.QImage(2,2,QtGui.QImage.Format.Format_RGBA8888);clear.fill(QtCore.Qt.GlobalColor.transparent);clear.save(str(self.root/'clear.png'))
        state=fresh_state();state['backgrounds']=[str(path)]
        state['settings'].update(ffmpeg_path=FF,video_encoder='cpu',encoder_fallback=False,
            output_dir=str(self.root/'outputs'),overwrite=True,music_enabled=False,use_voice=False,keep_audio=True,
            aspect='source',darkness=0,background_blur=0,auto_crop_black_bars=False,layers=[dict(id='tiny',kind='image',path=str(self.root/'clear.png'),width=10,height=10)],
            background_trims=[rule],unvoiced_duration_mode='fixed',unvoiced_duration_seconds=2.5,loop_transition='none')
        return state

    def test_native_long_gop_trim_starts_with_a_and_repeats_only_ab(self):
        path,rule=self.fixture();spec=trim.TrimInput(str(path),rule,2.5)
        args=[FF,'-v','error','-i',spec,'-vf',trim.prefix({'_background_trim':rule}).rstrip(','),'-t','2.5',
              '-fps_mode','passthrough','-f','rawvideo','-pix_fmt','rgb24','pipe:1']
        with Inputs(args) as inputs: result=subprocess.run(inputs.args,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        frames=np.frombuffer(result.stdout,np.uint8).reshape(-1,180,240,3)
        self.assertEqual(len(frames),25);self.assertGreater(frames[:,:,:,1].min(),210)
        self.assertLess(frames[:,:,:,[0,2]].max(),30)
        self.assertEqual(str(spec),str(path));self.assertEqual(spec.repeats,2)

    def test_project_roundtrip_same_path_named_pool_and_stale_source(self):
        path,rule=self.fixture();state=self.state(path,rule);store=Store(self.root/'private');store.save(state,0)
        loaded=store.load();self.assertEqual(loaded['settings']['background_trims'],[rule])
        self.assertEqual(trim.selected(loaded['settings'],str(path)),rule)
        _,fp=engine.resolve_tools(FF);media=probe_background(path,fp,loaded['settings'])
        self.assertEqual(media['duration'],1.);self.assertEqual(media['_background_trim'],rule)
        self.assertNotIn('_background_trim',probe_background('',fp,loaded['settings']))
        with patch('pathlib.Path.stat',side_effect=AssertionError('normalization must not stat pools')):
            self.assertEqual(trim.normalized(state['settings']),[rule])
        state['settings']['background_trims'][0]['size']+=1
        with self.assertRaisesRegex(ValueError,'裁切'):probe_background(path,fp,state['settings'])

    def test_preview_poster_uses_trim_start_not_discarded_intro(self):
        path,rule=self.fixture();state=self.state(path,rule);results=[]
        worker=PreviewWorker(str(path),FF,frame_settings=state['settings'],cache_directory=self.root)
        worker.ready.connect(results.append);worker.run()
        self.assertNotIn('error',results[0]);image=QtGui.QImage.fromData(results[0]['image'])
        pixel=image.pixelColor(image.width()//2,image.height()//2)
        self.assertGreater(pixel.green(),200);self.assertLess(pixel.red(),40)
        self.assertEqual(results[0]['media']['duration'],1.)

    def test_real_engine_trim_loop_slow_dissolve_and_original_audio(self):
        path,rule=self.fixture();state=self.state(path,rule);before=path.read_bytes()
        job=dict(id='trim',path=str(path),name='trim',title='',body='',status='待生成')
        state['jobs']=[job]
        for strategy,transition in (('loop','none'),('loop','fade'),('slow','none'),('hybrid','fade')):
            state['settings'].update(duration_strategy=strategy,loop_transition=transition,transition_seconds=.2)
            with engine.prepare_static_text(job,state,self.root/'private') as prepared:
                self.assertTrue(any(isinstance(a,trim.TrimInput) for a in prepared.args))
                prepared.encode();output=prepared.publish()
            data=self.ff(['-i',str(output),'-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
            frames=np.frombuffer(data,np.uint8).reshape(-1,180,240,3)
            self.assertGreaterEqual(len(frames),24);self.assertGreater(frames[:,:,:,1].mean(),200)
            self.assertLess(frames[:,:,:,[0,2]].max(),45)
            audio=np.frombuffer(self.ff(['-i',str(output),'-vn','-ac','1','-ar','48000','-f','f32le','pipe:1']),np.float32)
            # Slow playback changes pitch only through atempo (pitch preserved).
            spectrum=np.abs(np.fft.rfft(audio[4000:28000]));freq=np.fft.rfftfreq(24000,1/48000)[spectrum.argmax()]
            self.assertAlmostEqual(freq,800,delta=4)
        self.assertEqual(path.read_bytes(),before)

    def test_trim_dialog_and_right_click_markers(self):
        from app_plugins.builtin.batch_text_video.background_trim_ui import add_actions
        from app_plugins.builtin.batch_text_video.pool_ui import ResourceTable
        from types import SimpleNamespace
        path,rule=self.fixture();table=ResourceTable(['background']);table.populate([(str(path),[path.name],str(path),'')]);table.selectRow(0)
        fake=SimpleNamespace(table_for=lambda key:table,state=self.state(path,rule))
        menu=QtWidgets.QMenu();add_actions(fake,menu,'backgrounds')
        self.assertEqual(menu.actions()[0].text(),'裁切视频…');self.assertTrue(menu.actions()[0].isEnabled())
        self.assertTrue(menu.actions()[1].isEnabled())
        name,tip=trim.display(str(path),path.name,trim.lookup(fake.state['settings']))
        self.assertIn('已裁切',name);self.assertIn('1.000–2.000',tip)
        dialog=LoopRegionDialog(dict(kind='image',source='fixed',path=str(path),media_loop_regions=[rule]),trim_mode=True)
        try:
            deadline=time.monotonic()+10
            while dialog.info is None and time.monotonic()<deadline:self.app.processEvents();time.sleep(.01)
            self.assertIsNotNone(dialog.info);self.assertEqual(dialog.windowTitle(),'裁切视频')
            self.assertTrue(dialog.enabled.isChecked());self.assertTrue(dialog.join.isHidden())
            self.assertIn('仅使用',dialog.summary.text());self.assertTrue(dialog.capture());self.assertEqual(dialog.rules,[rule])
            dialog.enabled.setChecked(False);self.assertTrue(dialog.capture());self.assertEqual(dialog.rules,[])
        finally:
            dialog.reject()
            deadline=time.monotonic()+10
            while (dialog.worker or dialog.loader) and time.monotonic()<deadline:self.app.processEvents();time.sleep(.01)
            dialog.deleteLater();menu.deleteLater();table.deleteLater()

    def test_editor_background_request_rejects_obsolete_range(self):
        path,rule=self.fixture();state=self.state(path,rule)
        store=Store(self.root/'editor');store.save(state,0)
        editor=BatchTextVideoDialog(store=store)
        try:
            editor.edit_timer.stop()
            editor.preview_video=str(path)
            editor.request_background(str(path),force=True)
            deadline=time.monotonic()+15
            while editor.preview_worker and time.monotonic()<deadline:self.app.processEvents();time.sleep(.01)
            self.assertFalse(editor.preview_worker)
            self.assertFalse(editor._background_error)
            self.assertEqual(editor._media['duration'],1.)
            self.assertIn('已裁切',editor.video_pool.item(0,0).text())
            # A result from the old interval must not replace a newer request.
            previous=editor.preview.background.cacheKey()
            old={'path':str(path),'background_trims':[rule],'image':b'bad','media':None}
            editor.state['settings']['background_trims']=[dict(rule,start=.5)]
            editor._background_ready(old)
            self.assertEqual(editor.preview.background.cacheKey(),previous)
        finally:
            editor.edit_timer.stop();editor.close()
            deadline=time.monotonic()+15
            while (editor.preview_worker or editor.overlay_worker) and time.monotonic()<deadline:self.app.processEvents();time.sleep(.01)
            editor.deleteLater()
