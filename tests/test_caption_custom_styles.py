"""User-owned Fusion recipe: same five-point curve in preview, node and ASS."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.caption_custom_styles import LZX_WORD_POP, pop_curve
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, effects, preset_choices, description
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives, motion_at, render_design
from app_plugins.builtin.batch_text_video.caption_effect_ui import CaptionEffectEditor
from app_plugins.builtin.batch_text_video.caption_gallery import sample
from app_plugins.builtin.batch_text_video.captions import new_caption, write_caption_ass, filter_path
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.layers import normalize_layers

WORDS=[dict(text='Božia',start=.25,end=.6),dict(text='láska',start=1.,end=1.35),
       dict(text='dáva',start=1.6,end=1.9),dict(text='nádej.',start=2.2,end=2.6)]


class CustomCaptionStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def tearDown(self):
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def layer(self):
        return normalize_layers([apply_style(new_caption(),LZX_WORD_POP,reset_layout=True)])[0]

    def test_recipe_kept_separate_from_upstream_and_preserves_timing_selection(self):
        original=new_caption(); original.update(caption_timing='model',caption_language='sk',caption_source='full')
        value=apply_style(original,LZX_WORD_POP,reset_layout=True)
        self.assertIn(('自制达芬奇字幕-逐词柔弹',LZX_WORD_POP),preset_choices())
        for key in ('caption_timing','caption_language','caption_source'): self.assertEqual(value[key],original[key])
        self.assertFalse(value['caption_random_size'])
        self.assertEqual(value['caption_words'],6)
        self.assertEqual(value['caption_hold_ms'],1000)
        self.assertEqual(value['outline_color'],'#0d1f4d')
        self.assertIn('五段缩放',description(value))

    def test_exact_five_points_and_shared_motion_implementation(self):
        for progress,scale,alpha in ((0,.55,0),(.18,.86,1),(.55,1.28,1),(.78,.94,1),(1,1,1)):
            with self.subTest(progress=progress):
                actual,opacity=pop_curve(progress)
                self.assertAlmostEqual(actual,scale); self.assertAlmostEqual(opacity,alpha)
                primitive=dict(start=1.,end=3.,motion='lzx_word_pop',motion_ms=1000,blur=0,lzx_peak=1.28)
                output=motion_at(primitive,1+progress)
                self.assertAlmostEqual(output[0],scale); self.assertAlmostEqual(output[3],alpha)
        self.assertAlmostEqual(pop_curve(.55,1.8)[0],1.8)

    def test_authoritative_word_times_reveal_and_hold_without_remapping(self):
        primitives,events=make_primitives(WORDS,self.layer(),1080,1920,4.)
        self.assertEqual([(e['start'],e['spoken_end']) for e in events],[(w['start'],w['end']) for w in WORDS])
        glyphs=[p for p in primitives if p['z']==0]
        self.assertEqual([p['start'] for p in glyphs],[w['start'] for w in WORDS])
        self.assertEqual({e['end'] for e in events},{3.6})
        self.assertTrue(all(p['motion']=='lzx_word_pop' for p in glyphs))

    def test_effect_dialog_exposes_peak_and_rejects_out_of_range(self):
        value=self.layer(); editor=CaptionEffectEditor(value)
        self.addCleanup(editor.deleteLater)
        self.assertIn('lzx_pop_scale',editor.controls)
        editor.controls['lzx_pop_scale'].setValue(1.75)
        self.assertEqual(effects(dict(value,caption_effects=editor.value()))['lzx_pop_scale'],1.75)
        with self.assertRaisesRegex(ValueError,'峰值'):
            effects(dict(value,caption_effects={'lzx_pop_scale':2.5}))

    def test_gallery_and_design_use_actual_renderer(self):
        self.assertFalse(sample(LZX_WORD_POP).isNull())
        value=self.layer(); value.update(text='Božia láska dáva nádej.',x=50,y=50,width=90,height=80)
        image,_=render_design(value,360,640)
        self.assertFalse(image.isNull())
        self.assertTrue(any(image.pixelColor(x,y).alpha()>0 for y in range(0,640,4) for x in range(0,360,4)))

    def test_ass_contains_all_scale_and_opacity_keyframes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'caption.ass'
            write_caption_ass(path,WORDS,self.layer(),1080,1920,4.)
            data=path.read_text('utf8')
            for size in ('55.000','86.000','128.000','94.000','100.000'):
                self.assertIn('\\fscx'+size,data)
            self.assertIn('\\1a&HFF&',data)
            self.assertIn('\\1a&H00&',data)

    @unittest.skipUnless(os.environ.get('BATCH_TEST_FFMPEG'),'needs FFmpeg')
    def test_real_render_reveals_words_only_at_their_times(self):
        ffmpeg=os.environ['BATCH_TEST_FFMPEG']
        def run(arguments):
            result=subprocess.run([ffmpeg,'-hide_banner','-loglevel','error',*arguments],capture_output=True,timeout=45,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
            return result.stdout
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'captions.ass'; value=self.layer()
            events=write_caption_ass(path,WORDS,value,360,640,4.)
            output=Path(directory)/'result.mp4'
            run(['-y','-f','lavfi','-i','color=c=0x263748:s=360x640:r=30:d=4',
                 '-vf','ass=filename='+filter_path(path),'-c:v','libx264','-pix_fmt','yuv420p',str(output)])
            images=[QtGui.QImage.fromData(run(['-ss',str(t),'-i',str(output),'-frames:v','1','-f','image2pipe','-c:v','png','-']))
                    for t in (.1,.55,1.35)]
            def bright(image,event=None):
                rect=QtCore.QRectF(0,0,360,640) if event is None else QtCore.QRectF(event['x']-2,event['y']-2,event['width']+4,event['height']+4)
                rect=rect.toAlignedRect().intersected(image.rect())
                return sum(image.pixelColor(x,y).red()>210 and image.pixelColor(x,y).green()>210
                           for y in range(rect.top(),rect.bottom()+1) for x in range(rect.left(),rect.right()+1))
            self.assertEqual(bright(images[0]),0)
            self.assertGreater(bright(images[1],events[0]),20)
            self.assertEqual(bright(images[1],events[1]),0)
            self.assertGreater(bright(images[2],events[1]),20)
            if os.environ.get('LZX_CAPTION_QA_IMAGE'):
                images[2].save(os.environ['LZX_CAPTION_QA_IMAGE'])
