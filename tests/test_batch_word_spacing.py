"""Watermarks keep natural spaces; pop animation must not stretch resting words."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.watermarks import new_watermark, expand_watermarks
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, PREFIX
from app_plugins.builtin.batch_text_video.caption_custom_styles import LZX_WORD_POP
from app_plugins.builtin.batch_text_video.captions import new_caption, write_caption_ass
from app_plugins.builtin.batch_text_video.caption_style_render import groups_and_events, render_design
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.store import Store

WORDS = [dict(text=text, start=.25+i*.6, end=.75+i*.6)
         for i,text in enumerate(('Božia','milosrdenstvo','nádej','pokoj'))]


def caption(**values):
    layer = apply_style(new_caption(), LZX_WORD_POP)
    layer.update(font='Poppins',font_min=48,font_max=48,x=50,y=50,width=98,height=80,
        shadow_enabled=False,outline_width=0,caption_words=4,caption_per_line=4)
    layer.update(values)
    return normalize_layers([layer])[0]


class WordSpacingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_new_watermark_does_not_inherit_twenty_pixel_text_box_extra(self):
        watermark = new_watermark()
        self.assertTrue(all(part['word_spacing']==0 for part in watermark['corners'].values()))

    def test_legacy_watermark_corrects_only_old_default_and_retains_deliberate_spacing(self):
        watermark = new_watermark()
        for part in watermark['corners'].values():
            part.pop('word_spacing_version',None)
            part['word_spacing'] = 20
        watermark['corners']['top_left']['word_spacing'] = 7
        before = copy.deepcopy(watermark)
        normalized = normalize_layers([watermark])[0]
        self.assertEqual(watermark,before)
        self.assertEqual(normalized['corners']['top_left']['word_spacing'],7)
        self.assertEqual(normalized['corners']['top_right']['word_spacing'],0)
        normalized['corners']['top_right']['word_spacing'] = 20
        self.assertEqual(normalize_layers([normalized])[0]['corners']['top_right']['word_spacing'],20)

    def test_davinci_default_and_legacy_spacing_are_natural_but_generic_defaults_are_unchanged(self):
        self.assertEqual(caption()['word_spacing'],0)
        old = caption(); old.pop('word_spacing_version',None); old['word_spacing'] = 20
        self.assertEqual(normalize_layers([old])[0]['word_spacing'],0)
        old['word_spacing'] = 9
        self.assertEqual(normalize_layers([old])[0]['word_spacing'],9)
        self.assertEqual(caption(word_spacing=20)['word_spacing'],20)
        self.assertEqual(new_caption()['word_spacing'],20)
        self.assertEqual(apply_style(new_caption(),PREFIX+'默认白字')['word_spacing'],20)

    def test_resting_word_gap_is_font_space_not_word_width_times_pop_peak(self):
        layer = caption()
        events,_,_ = groups_and_events(WORDS,layer,1080,1920,4.)
        self.assertEqual({event['line'] for event in events},{0})
        gaps = [b['x']-a['x']-a['width'] for a,b in zip(events,events[1:])]
        from app_plugins.builtin.batch_text_video.fonts import weight_font
        font = weight_font(layer['font'],layer['font_weight'],layer['italic']); font.setPixelSize(48)
        natural = QtGui.QFontMetricsF(font).horizontalAdvance(' ')
        for gap in gaps:
            self.assertAlmostEqual(gap,natural,places=5)
        peak=1.28
        self.assertGreaterEqual(events[0]['x']-events[0]['width']*(peak-1)/2,10.8-1e-5)
        self.assertLessEqual(events[-1]['x']+events[-1]['width']*(peak+1)/2,1069.2+1e-5)

    def test_explicit_extra_spacing_and_resolution_scale_are_respected(self):
        for width,height in ((1080,1920),(360,640)):
            events,_,_ = groups_and_events(WORDS,caption(word_spacing=6),width,height,4.)
            gap = events[1]['x']-events[0]['x']-events[0]['width']
            from app_plugins.builtin.batch_text_video.fonts import weight_font
            font=weight_font('Poppins',700,False); font.setPixelSize(round(48*width/1080))
            self.assertAlmostEqual(gap,QtGui.QFontMetricsF(font).horizontalAdvance(' ')+6*width/1080,places=5)

    def test_source_install_does_not_require_hot_reloading_earlier_glyph_objects(self):
        from app_plugins.builtin.batch_text_video.captions import _glyph
        def earlier(*args,**kwargs):
            glyph=_glyph(*args,**kwargs); glyph.pop('_space_advance',None)
            return glyph
        expected=groups_and_events(WORDS,caption(),360,640,4.)[0]
        with patch('app_plugins.builtin.batch_text_video.captions._glyph',side_effect=earlier):
            actual=groups_and_events(WORDS,caption(),360,640,4.)[0]
        self.assertEqual([(e['x'],e['y']) for e in actual],[(e['x'],e['y']) for e in expected])

    def test_watermark_corrected_spacing_survives_save_and_render(self):
        watermark = new_watermark(); watermark['enabled'] = True
        part = watermark['corners']['top_right']
        part.update(enabled=True,text='Generované AI LZX',font_min=32,font_max=32,
            word_spacing=20,background_enabled=False,outline_width=0)
        part.pop('word_spacing_version',None)
        with tempfile.TemporaryDirectory() as root:
            store=Store(root); state=store.load(); state['settings']['layers']=normalize_layers([watermark])
            store.save(state,0)
            layers=store.load()['settings']['layers']
            part=next(p for p in expand_watermarks(layers) if p['id'].endswith(':top_right'))
            self.assertEqual(part['word_spacing'],0)
            image,_=render_text_box(part,1080,1920)
            self.assertFalse(image.isNull())

    def test_preview_and_ass_use_identical_compact_geometry_without_changing_word_times(self):
        layer=caption()
        expected,_,_=groups_and_events(WORDS,layer,360,640,4.)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'caption.ass'
            events=write_caption_ass(path,WORDS,layer,360,640,4.)
            self.assertEqual([(e['x'],e['y']) for e in events],[(e['x'],e['y']) for e in expected])
            self.assertEqual([(e['start'],e['spoken_end']) for e in events],[(w['start'],w['end']) for w in WORDS])
            image,_=render_design({**layer,'text':' '.join(w['text'] for w in WORDS)},360,640)
            self.assertFalse(image.isNull())
