"""Soft shadows stay separate from crisp typography across render routes."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.store import Store
from app_plugins.builtin.batch_text_video.captions import new_caption, write_caption_ass, filter_path
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, PREFIX
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives, render_design


def component(**values):
    layer = new_text_component()
    layer.update(text='Božia nádej', font='Segoe UI', font_min=64, font_max=64,
        width=90, height=70, align='center', vertical='center', outline_width=0,
        color='#ffffff', shadow_enabled=True, shadow_color='#ff00ff', shadow_opacity=80,
        shadow_x=0, shadow_y=60, shadow_blur=0)
    layer.update(values)
    return normalize_layers([layer])[0]


def pixels(image):
    return [image.pixelColor(x,y) for y in range(image.height()) for x in range(image.width())]


class ShadowBlurTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_schema_default_validation_and_catalog_legacy_value(self):
        self.assertEqual(normalize_layers([new_text_component()])[0]['shadow_blur'], 0)
        for value in (-1, 151, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                component(shadow_blur=value)
        layer = apply_style(new_caption(), PREFIX+'默认白字')
        layer.pop('shadow_blur')
        layer['caption_effects']['shadow_blur'] = 13
        self.assertEqual(normalize_layers([layer])[0]['shadow_blur'], 13)
        layer['shadow_blur'] = 0
        self.assertEqual(normalize_layers([layer])[0]['shadow_blur'], 0)

    def test_shadow_control_is_nested_enabled_and_persists_between_selection(self):
        with tempfile.TemporaryDirectory() as root:
            layer = component(shadow_enabled=False)
            panel = LayerPanel([layer], root)
            self.addCleanup(panel.deleteLater)
            panel.select_layer_id(layer['id'])
            controls = panel.text_properties.controls
            blur = controls['shadow_blur']
            self.assertFalse(blur.isEnabled())
            controls['shadow_enabled'].setChecked(True)
            self.assertTrue(blur.isEnabled())
            blur.setValue(16.5)
            panel.select_layer_id('title'); panel.select_layer_id(layer['id'])
            self.assertEqual(panel.current()['shadow_blur'], 16.5)
            self.assertEqual(panel.text_properties.controls['shadow_blur'].value(), 16.5)
            store = Store(Path(root)/'state')
            state = store.load(); state['settings']['layers'] = panel.values()
            store.save(state, 0)
            self.assertEqual(store.load()['settings']['layers'][0]['shadow_blur'], 16.5)
            self.assertTrue(panel.text_properties.style_sections.sections['shadow'].content.isAncestorOf(blur))

    def test_soft_shadow_spreads_alpha_without_blurring_foreground_or_changing_hue(self):
        hard, _ = render_text_box(component(), 360, 640)
        soft, _ = render_text_box(component(shadow_blur=15), 360, 640)
        hard_pixels, soft_pixels = pixels(hard), pixels(soft)
        white = lambda p: p.alpha()>200 and min(p.red(),p.green(),p.blue())>245
        hard_core, soft_core = sum(map(white,hard_pixels)), sum(map(white,soft_pixels))
        self.assertLessEqual(abs(hard_core-soft_core), max(5, hard_core*.01))
        colored = lambda p: p.alpha()>0 and p.green()<5 and p.red()>80 and p.blue()>80
        self.assertGreater(sum(map(colored,soft_pixels)), sum(map(colored,hard_pixels))*1.3)
        self.assertTrue(all(abs(p.red()-p.blue())<=1 for p in soft_pixels if colored(p)))
        if os.environ.get('LZX_SHADOW_SAMPLE'):
            sample = QtGui.QImage(720,640,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            sample.fill(QtGui.QColor('#263748'))
            painter = QtGui.QPainter(sample)
            painter.drawImage(0,0,hard); painter.drawImage(360,0,soft)
            painter.setPen(QtGui.QColor('white'))
            painter.drawText(150,200,'Hard'); painter.drawText(510,200,'Soft')
            painter.end()
            self.assertTrue(sample.save(os.environ['LZX_SHADOW_SAMPLE']))

    def test_disabled_shadow_and_zero_blur_keep_existing_rendering(self):
        plain, _ = render_text_box(component(shadow_enabled=False), 360, 640)
        unused, _ = render_text_box(component(shadow_enabled=False,shadow_blur=80), 360, 640)
        self.assertEqual(plain, unused)

    def test_vertical_and_scrolling_text_use_same_soft_shadow(self):
        from app_plugins.builtin.batch_text_video.scrolling_text import render_scroll_preview
        for writing_mode in ('horizontal','vertical_rl'):
            layer = component(writing_mode=writing_mode, text='Božia\nnádej', shadow_blur=9)
            image, _ = render_text_box(layer, 360, 640)
            self.assertFalse(image.isNull())
        layer = component(text_animation='up', shadow_blur=12)
        image, _ = render_scroll_preview(layer, 360, 640, duration=15, time=8)
        self.assertFalse(image.isNull())

    def test_catalog_preview_and_ass_share_shadow_blur(self):
        layer = apply_style(new_caption(), PREFIX+'默认白字')
        layer.update(shadow_enabled=True, shadow_color='#ff00ff', shadow_opacity=80,
            shadow_blur=18, shadow_x=0, shadow_y=40, width=90, height=80)
        layer = normalize_layers([layer])[0]
        words = [dict(text='Božia',start=.2,end=1),dict(text='nádej',start=1,end=2)]
        primitives, _ = make_primitives(words,layer,360,640,2.5)
        self.assertEqual({p['blur'] for p in primitives if p['z']==-2},{6})
        self.assertEqual({p['blur'] for p in primitives if p['z']==0},{0})
        design = {**layer,'text':'Božia nádej'}
        soft, _ = render_design(design,360,640)
        hard, _ = render_design({**design,'shadow_blur':0},360,640)
        self.assertNotEqual(soft,hard)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'style.ass'
            write_caption_ass(path,words,layer,360,640,2.5)
            shadow = [line for line in path.read_text('utf8').splitlines() if line.startswith('Dialogue: -2,')]
            self.assertTrue(shadow)
            self.assertTrue(all('\\blur6.000' in line for line in shadow))

    def test_basic_caption_blurs_only_separate_shadow_not_text_or_outline(self):
        layer = new_caption()
        layer.update(shadow_enabled=True,shadow_blur=18,shadow_color='#ff00ff',
            shadow_x=0,shadow_y=50,width=90,height=80)
        layer = normalize_layers([layer])[0]
        words = [dict(text='Božia',start=.2,end=1),dict(text='nádej',start=1,end=2)]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'style.ass'
            write_caption_ass(path,words,layer,360,640,2.5)
            lines = path.read_text('utf8').splitlines()
            shadows = [line for line in lines if line.startswith('Dialogue: -2,')]
            glyphs = [line for line in lines if line.startswith('Dialogue: 0,')]
            self.assertEqual(len(shadows),len(words))
            self.assertTrue(all('\\blur6.000' in line for line in shadows))
            self.assertTrue(all('\\blur0.000' in line and '\\shad0' in line for line in glyphs))
            self.assertTrue(all('\\fscx72' in line for line in glyphs+shadows))

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'), 'Set NODE_FFMPEG for native renderer check')
    def test_real_ffmpeg_retains_soft_colored_shadow_and_sharp_text(self):
        words = [dict(text='Božia',start=.2,end=2)]
        images = []
        with tempfile.TemporaryDirectory() as root:
            for radius in (0,18):
                layer = new_caption()
                layer.update(caption_preset='appear',caption_words=1,caption_per_line=1,
                    caption_random_size=False,font_min=90,font_max=90,width=95,height=90,
                    outline_width=0,color='#ffffff',shadow_enabled=True,shadow_color='#ff00ff',
                    shadow_opacity=100,shadow_x=0,shadow_y=80,shadow_blur=radius)
                layer = normalize_layers([layer])[0]
                path = Path(root)/'style.ass'
                write_caption_ass(path,words,layer,360,640,2.5)
                result = subprocess.run([os.environ['NODE_FFMPEG'],'-hide_banner','-loglevel','error',
                    '-f','lavfi','-i','color=c=black:s=360x640:r=24:d=2.5',
                    '-vf','format=rgba,ass=filename='+filter_path(path),'-ss','1',
                    '-frames:v','1','-f','image2pipe','-vcodec','png','-'],capture_output=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
                image = QtGui.QImage.fromData(result.stdout)
                self.assertFalse(image.isNull()); images.append(image)
        hard,soft = map(pixels,images)
        white = lambda p:min(p.red(),p.green(),p.blue())>240
        self.assertEqual(sum(map(white,hard)),sum(map(white,soft)))
        magenta = lambda p:p.red()>5 and p.blue()>5 and p.green()<5
        self.assertGreater(sum(map(magenta,soft)),sum(map(magenta,hard)))
