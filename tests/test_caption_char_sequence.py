"""Whole-cue SRT must type one continuous prefix, never all word prefixes."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from qt_compat import QtCore, QtGui, QtWidgets
import test_caption_display_controls as fixtures
from app_plugins.builtin.batch_text_video.caption_display import reveal_events
from app_plugins.builtin.batch_text_video.captions import read_task_subtitle, write_caption_ass
from app_plugins.builtin.batch_text_video.caption_style_render import render_design
from app_plugins.builtin.batch_text_video.caption_styles import PREFIX

TEXT = 'Viete, aký hriech Boh nikdy neodpustí? Všetci sme'
WORDS = [dict(text=text, start=0., end=4.38, cue=0) for text in TEXT.split()]


class CharSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        ensure_fonts()

    def layer(self, preset='appear', **values):
        value = fixtures.caption(preset, caption_display_mode='char', caption_accumulate=True,
            caption_words=6, caption_per_line=3, caption_cursor=True, font_min=54, font_max=54,
            width=94, height=80, caption_first_line_emphasis=False, caption_random_size=False)
        value['caption_effects'].update(anim_in_type='none', anim_out_type='none', karaoke_highlight=False,
            dynamic_box=False, only_show_active_word=False, global_mask_enabled=False,
            ambient_glow_enabled=False, ambient_lighting_enabled=False, use_underline=False,
            speed_trail_enabled=False, stroke_expand_enabled=False)
        value.update(values)
        return value

    def test_sentence_cue_never_starts_multiple_words_at_once_in_either_renderer(self):
        for preset in ('appear', PREFIX+'全屏打字机_金黄光标'):
            layer = self.layer(preset); words = copy.deepcopy(WORDS)
            _primitives, events = fixtures.primitives(layer, words, width=540, height=960)
            before = [dict(event) for event in events]
            pieces = reveal_events(events, layer)
            first = [p for p in pieces if p['_character_index'] == 0]
            self.assertEqual(len(first), len(words))
            self.assertEqual(len({round(p['start'], 6) for p in first}), len(words),
                'sentence SRT previously started every word at t=0')
            for a, b in zip(first, first[1:]):
                last = max(p['start'] for p in pieces if p['text'] == a['text'])
                self.assertLess(last, b['start'])
            visible = [p for p in pieces if p['start'] <= .15]
            self.assertEqual({p['text'] for p in visible}, {'Viete,'})
            self.assertEqual(events, before)
            self.assertEqual(words, WORDS)

    def test_precise_word_timestamps_stay_in_their_own_windows(self):
        words = [dict(text=t, start=.5+i*.7, end=1.+i*.7) for i,t in enumerate(('nádej', 'áno', 'Božia'))]
        layer = self.layer(); _p, events = fixtures.primitives(layer, words)
        pieces = reveal_events(events, layer)
        for word in words:
            own = [p for p in pieces if p['text'] == word['text']]
            self.assertEqual(min(p['start'] for p in own), word['start'])
            self.assertLess(max(p['start'] for p in own), word['end'])
        accented = [p for p in pieces if p['text'] == 'áno']
        self.assertEqual(len(accented), 3, 'combining accent remains attached to its base letter')

    def test_separate_cues_do_not_share_a_typing_window(self):
        words = WORDS[:3] + [dict(text='ďalej', start=5., end=5.8, cue=1)]
        layer = self.layer(); _p, events = fixtures.primitives(layer, words)
        own = [p for p in reveal_events(events, layer) if p['text'] == 'ďalej']
        self.assertEqual(min(p['start'] for p in own), 5.)

    def test_non_character_modes_keep_original_cue_behavior(self):
        for mode in ('group', 'word', 'line'):
            layer = self.layer(caption_display_mode=mode)
            _p, events = fixtures.primitives(layer, WORDS)
            self.assertEqual([e['start'] for e in reveal_events(events, layer)], [0.]*len(WORDS))

    def test_plain_srt_is_not_rewritten_or_converted_to_fake_word_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'task_audio.srt'
            source = ('1\n00:00:00,000 --> 00:00:04,380\n'+TEXT+'\n').encode('utf-8')
            path.write_bytes(source)
            words = read_task_subtitle(path, 5.)
            _p, events = fixtures.primitives(self.layer(), words)
            reveal_events(events, self.layer())
            self.assertTrue(all(w['start'] == 0. and w['end'] == 4.38 for w in words))
            self.assertEqual(path.read_bytes(), source)

    def test_native_ass_and_qt_preview_leave_future_words_empty(self):
        ffmpeg = os.environ.get('BATCH_TEST_FFMPEG')
        if not ffmpeg: self.skipTest('set BATCH_TEST_FFMPEG for native verification')
        layer = self.layer(PREFIX+'全屏打字机_金黄光标', caption_cursor=False, color='#ffffff')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'caption.ass'
            events = write_caption_ass(path, WORDS, layer, 540, 960, 5.)
            result = subprocess.run([ffmpeg, '-v','error','-f','lavfi','-i','color=c=black:s=540x960:r=25:d=5',
                '-vf',"ass=filename='caption.ass'",'-ss','0.15','-frames:v','1','-f','image2pipe','-c:v','png','-'],
                cwd=directory, capture_output=True, timeout=30,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8','replace'))
            rendered = QtGui.QImage.fromData(result.stdout)
            preview = render_design(layer, 540, 960, words=WORDS, duration=5., time=.15)[0]
            def visible(image, event):
                box = QtCore.QRectF(event['x'],event['y'],event['width'],event['height']).toAlignedRect().intersected(image.rect())
                return sum(image.pixelColor(x,y).lightness()>70 and image.pixelColor(x,y).alpha()>100
                    for y in range(box.top(),box.bottom()+1) for x in range(box.left(),box.right()+1))
            for image in (rendered, preview):
                self.assertGreater(visible(image, events[0]), 5)
                for event in events[1:]: self.assertEqual(visible(image, event), 0, event['text'])
            output = os.environ.get('CAPTION_CHAR_QA_DIR')
            if output:
                folder = Path(output); folder.mkdir(parents=True, exist_ok=True)
                rendered.save(str(folder/'export-early.png')); preview.save(str(folder/'preview-early.png'))
                render_design(layer, 540, 960, words=WORDS, duration=5., time=3.)[0].save(str(folder/'preview-later.png'))


if __name__ == '__main__': unittest.main()
