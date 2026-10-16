import copy
from itertools import groupby
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets,QtGui
from app_plugins.builtin.batch_text_video.captions import new_caption,caption_events,_glyph,write_caption_ass
from app_plugins.builtin.batch_text_video.caption_styles import apply_style,PREFIX
from app_plugins.builtin.batch_text_video.caption_style_render import groups_and_events,render_design
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts

TEXT='nohách. Ty si dôvodom, prečo som tu. Uvidíme,'
WORDS=[dict(text=t,start=.1+i*.4,end=.4+i*.4) for i,t in enumerate(TEXT.split())]


def component(preset=None,**changes):
    layer=new_caption()
    if preset:layer=apply_style(layer,PREFIX+preset)
    layer.update(font='Open Sans',font_min=48,font_max=48,width=98,height=80,caption_words=8,caption_per_line=4,
        **changes)
    return normalize_layers([layer])[0]


class CaptionBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def assert_baseline(self,events):
        values=[event['y']+event['_baseline'] for event in events]
        self.assertLess(max(values)-min(values),.001)

    def test_font_baseline_is_not_trimmed_per_word(self):
        layer=component()
        glyphs=[_glyph(word,layer,48,native=True) for word in ('som','prečo','dôvodom','Ty','gyp')]
        for glyph in glyphs:
            self.assertGreater(glyph['_baseline'],0)
            self.assertLessEqual(glyph['_baseline'],glyph['height'])
        self.assertEqual(len({g['_baseline'] for g in glyphs}),1)
        self.assertNotEqual(glyphs[0]['_native_path'].boundingRect().top(),
                            glyphs[2]['_native_path'].boundingRect().top())

    def test_builtin_same_size_and_mixed_random_sizes_share_row_baseline(self):
        for random_size in (False,True):
            with self.subTest(random_size=random_size):
                layer=component();layer.update(caption_random_size=random_size,font_min=30,font_max=74)
                events=caption_events(WORDS,layer,1080,1920,4)
                for start in (0,4):self.assert_baseline(events[start:start+4])
                self.assertEqual([e['start'] for e in events],[w['start'] for w in WORDS])

    def test_catalog_normal_rows_align_accents_and_descenders(self):
        layer=component('暗夜青_青高亮',caption_random_size=False)
        events,_groups,_style=groups_and_events(WORDS,layer,1080,1920,4)
        for _,items in groupby(events,key=lambda e:(e['group'],e['line'])):
            self.assert_baseline(list(items))

    def test_catalog_mixed_sizes_align_without_changing_authoritative_times(self):
        layer=component('逐词弹出(随机大小)');layer.update(font_min=24,font_max=60,caption_random_size=True)
        events,_groups,_style=groups_and_events(WORDS,layer,1080,1920,4)
        for _,items in groupby(events,key=lambda e:(e['group'],e['line'])):
            self.assert_baseline(list(items))
        self.assertEqual([(e['text'],e['start'],e['spoken_end']) for e in events],
                         [(w['text'],w['start'],w['end']) for w in WORDS])

    def test_intentional_random_position_remains_random_and_deterministic(self):
        layer=component('随机单词定位',caption_random_size=False)
        first=groups_and_events(WORDS,layer,1080,1920,4)[0]
        self.assertGreater(len({round(e['y'],2) for e in first}),1)
        self.assertEqual(first,groups_and_events(WORDS,layer,1080,1920,4)[0])

    def test_preview_and_ass_use_same_baseline_geometry_without_mutating_input(self):
        layer=component('暗夜青_青高亮',caption_random_size=False)
        before=copy.deepcopy(layer)
        sample={**layer,'text':TEXT}
        image,_size=render_design(sample,540,960)
        self.assertFalse(image.isNull())
        with tempfile.TemporaryDirectory() as directory:
            events=write_caption_ass(Path(directory)/'caption.ass',WORDS,layer,540,960,4)
            for _,items in groupby(events,key=lambda e:(e['group'],e['line'])):
                self.assert_baseline(list(items))
        self.assertEqual(layer,before)

    def test_playback_does_not_reuse_old_misaligned_cache(self):
        import hashlib,json
        from app_plugins.builtin.batch_text_video.playback import preview_key
        from app_plugins.builtin.batch_text_video.store import fresh_state
        original=hashlib.sha256
        captured=[]
        def sha(value):
            captured.append(value)
            return original(value)
        with patch('app_plugins.builtin.batch_text_video.playback.hashlib.sha256',side_effect=sha):
            preview_key(dict(path='',title='',body=''),fresh_state())
        self.assertEqual(json.loads(captured[-1])[0],'playback-v19-common-caption-appearance')


if __name__=='__main__':unittest.main()
