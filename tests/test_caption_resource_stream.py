import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video import caption_style_render as renderer
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
from app_plugins.builtin.batch_text_video.captions import filter_path,write_caption_ass
from app_plugins.builtin.batch_text_video.caption_resources import DrawingResources,ass_filter,FONT_FOLDER,memory_for_directory
from app_plugins.builtin.batch_text_video.caption_ass_stream import ActiveBudget,checking
from app_plugins.builtin.batch_text_video.caption_font_binary import checksum
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts

FFMPEG=os.environ.get('NODE_FFMPEG','')


class ResourceStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([]);ensure_fonts()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def layer(self,**values):
        layer=new_follow_caption();layer.update(font='Open Sans',font_min=90,font_max=90,caption_random_size=False,
            caption_follow_mode='line',caption_display_mode='char',caption_cursor=False,outline_position='outside',
            outline_width=10,shadow_enabled=False,background_enabled=False,x=50.2,y=60.3)
        layer['caption_effects'].update(anim_in_type='none',anim_out_type='none')
        layer.update(values);return normalize_layers([layer])[0]

    def words(self,count):return [dict(text=('Božia','láska','ochraňuj','nás')[i%4],start=i*.6,end=(i+1)*.6) for i in range(count)]

    def test_budget_is_concurrent_not_cumulative(self):
        budget=ActiveBudget(1,100)
        for i in range(100001):budget.add(i,i+.5,100)
        budget.validate();self.assertEqual(budget.rows,100001);self.assertEqual(budget.peak_rows,1)
        overlap=ActiveBudget(1,100);overlap.add(0,1,60);overlap.add(.5,1.5,60)
        with self.assertRaisesRegex(ValueError,'同一时刻'):overlap.validate()

    def test_resources_are_private_valid_fonts_with_holes_and_shared_geometry(self):
        layer=self.layer();p,e=renderer.make_primitives(self.words(3),layer,320,180,1.8)
        families=set(QtGui.QFontDatabase.families());bank=DrawingResources(self.root/'sample.ass',p,True)
        self.assertGreater(bank.glyphs,0);self.assertLess(bank.glyphs,len(p));self.assertEqual(set(QtGui.QFontDatabase.families()),families)
        for file in bank.files:
            data=(bank.folder/file).read_bytes();self.assertEqual(checksum(data),0xb1b0afba)
            raw=QtGui.QRawFont(str(bank.folder/file),256);self.assertTrue(raw.isValid())
            self.assertFalse(raw.pathForGlyph(1).isEmpty())
        self.assertTrue(all(bank.get(v) is None or len(bank.get(v)[1])==1 for v in p))

    def test_curves_and_out_of_range_geometry_keep_native_drawing(self):
        shape=QtGui.QPainterPath();shape.addEllipse(QtCore.QRectF(0,0,10,10))
        p=[dict(native=shape,end=40)]
        bank=DrawingResources(self.root/'native.ass',p,True)
        self.assertIsNone(bank.get(p[0]));self.assertFalse(bank.files)

    def test_short_captions_keep_existing_vector_backend(self):
        layer=self.layer();path=self.root/'short.ass';write_caption_ass(path,self.words(2),layer,320,180,1.2)
        self.assertFalse((self.root/FONT_FOLDER).exists());self.assertIn('\\p4',path.read_text('utf8'))

    @unittest.skipUnless(FFMPEG,'Explicit real FFmpeg required')
    def test_embedded_resources_work_without_external_font_folder(self):
        layer=self.layer();p,e=renderer.make_primitives(self.words(6),layer,320,180,3.6)
        path=self.root/'embedded.ass';renderer.write_primitives_ass(path,p,e,layer,320,180,use_resources=True)
        self.assertIn('[Fonts]',path.read_text('utf8'));(self.root/FONT_FOLDER).rename(self.root/'unavailable-font-folder')
        result=subprocess.run([FFMPEG,'-v','error','-f','lavfi','-i','color=c=0x335533:s=320x180:r=25:d=1',
            '-vf','setpts=PTS+1/TB,ass=filename='+filter_path(path),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],
            capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        pixels=np.frombuffer(result.stdout,np.uint8).reshape(180,320,3)
        self.assertGreater(np.count_nonzero(pixels[:,:,0]>100),100)

    def test_floating_uses_same_visible_samples_without_whole_transcript_sampling(self):
        layer=self.layer();layer['caption_effects'].update(anim_in_type='floating',anim_in_duration=.2)
        words=self.words(128);p,e=renderer.make_primitives(words,layer,320,180,76.8)
        scripts=[]
        for optimized in (False,True):
            path=self.root/('floating'+str(optimized)+'.ass')
            renderer.write_primitives_ass(path,p,e,layer,320,180,use_resources=False,optimize_follow=optimized)
            scripts.append(path.read_text('utf8'))
        self.assertEqual(scripts[0],scripts[1])

    def test_follow_visibility_optimization_preserves_all_written_events(self):
        layer=self.layer();words=self.words(512);p,e=renderer.make_primitives(words,layer,320,180,len(words)*.6)
        outputs=[];candidates=[]
        for enabled in (False,True):
            path=self.root/(str(enabled)+'.ass')
            renderer.write_primitives_ass(path,p,e,layer,320,180,use_resources=False,optimize_follow=enabled)
            outputs.append(path.read_text('utf8'));candidates.append(json.loads(path.with_suffix('.caption-data.json').read_text())['candidate_segments'])
        self.assertEqual(outputs[0],outputs[1]);self.assertLess(candidates[1],candidates[0]*.8)

    def test_resources_reduce_long_script_and_are_deterministic_across_runs(self):
        layer=self.layer();words=self.words(64);p,e=renderer.make_primitives(words,layer,320,180,38.4)
        sizes=[]
        for enabled in (False,True):
            path=self.root/(str(enabled)+'.ass')
            renderer.write_primitives_ass(path,p,e,layer,320,180,use_resources=enabled)
            sizes.append(path.stat().st_size)
        self.assertLess(sizes[1],sizes[0]*.5)
        path=self.root/'again.ass';renderer.write_primitives_ass(path,p,e,layer,320,180,use_resources=True)
        self.assertEqual(path.read_bytes(),(self.root/'True.ass').read_bytes())
        self.assertIn('fontsdir=',ass_filter(path));self.assertGreater(memory_for_directory(self.root),path.stat().st_size)

    def test_cancellation_preserves_existing_target(self):
        layer=self.layer();p,e=renderer.make_primitives(self.words(3),layer,320,180,1.8);path=self.root/'cancel.ass';path.write_text('before')
        calls=[]
        def check():
            calls.append(1)
            if len(calls)>=3:raise InterruptedError('cancel synthetic')
        with self.assertRaises(InterruptedError):renderer.write_primitives_ass(path,p*100,e,layer,320,180,use_resources=False,check=check)
        self.assertEqual(path.read_text(),'before')

    def test_streamed_file_can_exceed_old_64mib_without_accumulating_rows(self):
        # Opaque, sequential synthetic events: a large file, one active primitive.
        layer=self.layer();p,e=renderer.make_primitives(self.words(1),layer,320,180,.6)
        template=next(v for v in p if not v.get('outline_path'));shape=QtGui.QPainterPath();shape.moveTo(0,0)
        for i in range(700):shape.lineTo(i%200,(i*7)%120)
        shape.closeSubpath()
        template={**template,'native':shape,'motion':'none','follow_track':None,'screen_clip':None,'width':200,'height':120}
        count=8000;primitives=[dict(template,start=i,end=i+.5) for i in range(count)]
        path=self.root/'large.ass';tracemalloc.start()
        try:
            renderer.write_primitives_ass(path,primitives,[],layer,320,180,use_resources=False)
            _current,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertGreater(path.stat().st_size,64*1024*1024)
        self.assertLess(peak,32*1024*1024)
        info=json.loads(path.with_suffix('.caption-data.json').read_text());self.assertEqual(info['peak_rows'],1)

    @unittest.skipUnless(FFMPEG,'Explicit real FFmpeg required')
    def test_thirty_minute_caption_past_both_old_limits_renders_last_words(self):
        layer=self.layer();words=[dict(v,start=i*1800/3000,end=(i+1)*1800/3000) for i,v in enumerate(self.words(3000))]
        path=self.root/'thirty-minutes.ass';started=time.monotonic();write_caption_ass(path,words,layer,320,180,1800.)
        info=json.loads(path.with_suffix('.caption-data.json').read_text())
        self.assertGreater(info['rows'],80000);self.assertGreater(path.stat().st_size,64*1024*1024)
        self.assertLess(info['peak_rows'],80000)
        result=subprocess.run([FFMPEG,'-v','error','-f','lavfi','-i','color=c=0x335533:s=320x180:r=25:d=1',
            '-vf','setpts=PTS+1799.5/TB,'+ass_filter(path),'-frames:v','1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],
            capture_output=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        pixels=np.frombuffer(result.stdout,np.uint8).reshape(180,320,3)
        self.assertGreater(np.count_nonzero(pixels[:,:,0]>100),100)
        if os.environ.get('LZX_LONG_CAPTION_QA'):
            (Path(os.environ['LZX_LONG_CAPTION_QA'])/'thirty-minute-proof.json').write_text(json.dumps(dict(info,
                file_bytes=path.stat().st_size,seconds=time.monotonic()-started,late_frame_seconds=1799.5)),encoding='utf8')

    def test_long_image_cursor_track_is_not_rejected_by_total_events(self):
        from app_plugins.builtin.batch_text_video.caption_cursor import write_native_assets
        image=QtGui.QImage(10,10,QtGui.QImage.Format.Format_ARGB32_Premultiplied);image.fill(QtCore.Qt.GlobalColor.white)
        source=self.root/'cursor.png';image.save(str(source));count=80001
        primitives=[dict(cursor_image=str(source),start=i*.1,end=(i+1)*.1,height=10,x=0,y=0,opacity=1) for i in range(count)]
        path=self.root/'cursor.ass';write_native_assets(path,primitives,self.layer(),320,180)
        metadata=json.loads(path.with_suffix('.cursor.json').read_text());self.assertEqual(metadata['rows'],count*2)
        previous=-1.
        with Path(metadata['commands']).open(encoding='utf8') as commands:
            for line in commands:
                current=float(line.split(' ',1)[0]);self.assertGreaterEqual(current,previous);previous=current

    @unittest.skipUnless(FFMPEG,'Explicit real FFmpeg required')
    def test_real_render_preserves_motion_clips_alpha_and_glyph_bounds(self):
        layer=self.layer();words=self.words(6);p,e=renderer.make_primitives(words,layer,320,180,3.6)
        frames=[]
        for enabled in (False,True):
            path=self.root/(str(enabled)+'.ass');renderer.write_primitives_ass(path,p,e,layer,320,180,use_resources=enabled)
            result=subprocess.run([FFMPEG,'-v','error','-f','lavfi','-i','color=c=0x335533:s=320x180:r=12:d=3.6',
                '-vf',ass_filter(path),'-pix_fmt','rgb24','-f','rawvideo','pipe:1'],capture_output=True,timeout=40,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'));frames.append(np.frombuffer(result.stdout,np.uint8).astype(np.int16))
        self.assertEqual(len(frames[0]),len(frames[1]));difference=np.abs(frames[0]-frames[1])
        # Same quantized contours; font-vs-drawing raster edges can differ slightly.
        self.assertLessEqual(int(difference.max()),16)
        self.assertLess(float(np.mean(difference.astype(float)**2)),.3)


if __name__=='__main__':unittest.main()
