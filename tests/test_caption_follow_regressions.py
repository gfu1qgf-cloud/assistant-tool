import copy
import os
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_preview_overlay
from app_plugins.builtin.batch_text_video.store import DEFAULTS
from app_plugins.builtin.batch_text_video.captions import task_word_timings, design_layers
from app_plugins.builtin.batch_text_video.caption_follow_preview import prepare
from app_plugins.builtin.batch_text_video.layout import _render_component, CaptionPreviewError


class FollowPreviewRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_default_follow_in_static_and_seek_preview(self):
        for length in (12, 180, 1500):
            text = ' '.join(['Pane prosím ochraňuj moju rodinu a naplň naše srdcia pokojom každý deň'] * (length // 12))
            for duration in (None, 60., .1):
                with self.subTest(length=length, duration=duration):
                    settings = copy.deepcopy(DEFAULTS)
                    layer = new_follow_caption()
                    settings['layers'] = normalize_layers([layer])
                    for item in settings['layers']:
                        if item['id'] != layer['id']: item['enabled'] = False
                    if duration is not None: settings['_editor_timing'] = {'duration':duration, 'time':duration*.5}
                    image, info = render_preview_overlay('', text, 540, 960, settings)
                    self.assertFalse(info.get('export_blocked'), info.get('warnings'))

    def fixture(self, root):
        voice = Path(root)/'task_audio.wav'
        voice.write_bytes(b'not used for inference')
        srt = voice.with_suffix('.srt')
        srt.write_text('1\n00:00:00,100 --> 00:00:02,500\nPane prosím ochraňuj\n', encoding='utf8')
        layer = normalize_layers([new_follow_caption()])[0]
        job = {'body':'Pane prosím ochraňuj', 'title':'', 'voice_path':str(voice)}
        words = [{'text':text, 'start':.1+i*.5, 'end':.4+i*.5} for i,text in enumerate(job['body'].split())]
        return voice, layer, job, words

    def test_auto_export_uses_precise_alignment_when_existing_srt_is_sentence_timed(self):
        with tempfile.TemporaryDirectory() as root:
            voice, layer, job, words = self.fixture(root)
            before = voice.with_suffix('.srt').read_bytes()
            reports = []
            with patch('model.ElevenLabsTimings.load_timings', return_value=None), \
                 patch('app_plugins.builtin.batch_text_video.captions.load_word_timings', return_value=words) as align:
                result = task_word_timings(job, voice, layer, '', job['body'], 3., root, report=reports.append)
            self.assertEqual(result, words)
            self.assertEqual(align.call_count, 1)
            self.assertFalse(align.call_args.kwargs.get('cache_only', False))
            self.assertTrue(any('精确逐词对齐' in value for value in reports))
            self.assertEqual(voice.with_suffix('.srt').read_bytes(), before)

    def test_auto_preview_reuses_precise_cache_and_never_starts_alignment(self):
        with tempfile.TemporaryDirectory() as root:
            voice, layer, job, words = self.fixture(root)
            settings = {'layers':[layer], '_preview_voice':str(voice), '_editor_timing':{'duration':3.,'time':1.}}
            with patch('model.ElevenLabsTimings.load_timings', return_value=None), \
                 patch('app_plugins.builtin.batch_text_video.captions.load_word_timings', return_value=words) as align:
                preview, notices = prepare(settings, job, root)
            self.assertEqual(preview['_caption_preview_words'][layer['id']], words)
            self.assertTrue(align.call_args.kwargs['cache_only'])
            self.assertIn('复用现有时间戳', notices[0])

    def test_missing_precise_cache_keeps_layout_visible_with_explicit_illustration_notice(self):
        with tempfile.TemporaryDirectory() as root:
            voice, layer, job, words = self.fixture(root)
            settings = copy.deepcopy(DEFAULTS)
            settings.update(layers=normalize_layers([layer]), _preview_voice=str(voice),
                            _editor_timing={'duration':3., 'time':1.})
            for item in settings['layers']:
                if item['id'] != layer['id']: item['enabled'] = False
            with patch('model.ElevenLabsTimings.load_timings', return_value=None), \
                 patch('app_plugins.builtin.batch_text_video.captions.load_word_timings', return_value=None) as align:
                preview, notices = prepare(settings, job, root)
            image, info = render_preview_overlay('', job['body'], 540, 960, preview)
            self.assertFalse(info.get('export_blocked'), info.get('warnings'))
            self.assertTrue(align.call_args.kwargs['cache_only'])
            self.assertIn('仅为排版／运动示意', notices[0])
            if os.environ.get('LZX_FOLLOW_PREVIEW_QA'):
                self.assertTrue(image.save(os.environ['LZX_FOLLOW_PREVIEW_QA']))

    def test_explicit_sentence_source_is_not_guessed_or_called_a_fit_error(self):
        with tempfile.TemporaryDirectory() as root:
            voice, layer, job, words = self.fixture(root)
            layer.update(caption_timing='file', caption_file=str(voice.with_suffix('.srt')))
            with patch('app_plugins.builtin.batch_text_video.captions.load_word_timings', side_effect=AssertionError('no inference')):
                with self.assertRaisesRegex(ValueError, '逐词时间戳'):
                    task_word_timings(job, voice, layer, '', job['body'], 3., root)
            design = design_layers([layer], '', job['body'])[0]
            sentence = [dict(word,start=.1,end=2.5) for word in words]
            settings = {'_editor_timing':{'duration':3.,'time':1.}, '_caption_preview_words':{layer['id']:sentence}}
            with self.assertRaisesRegex(CaptionPreviewError, '逐词时间戳'):
                _render_component(design, 540, 960, settings)

    def test_line_mode_cache_does_not_leak_sentence_words_into_word_follow(self):
        with tempfile.TemporaryDirectory() as root:
            voice, layer, job, words = self.fixture(root)
            settings = {'layers':[dict(layer,caption_follow_mode='line')], '_preview_voice':str(voice),
                        '_editor_timing':{'duration':3.,'time':1.}}
            with patch('model.ElevenLabsTimings.load_timings', return_value=None), \
                 patch('app_plugins.builtin.batch_text_video.captions.load_word_timings', return_value=words) as align:
                line, _ = prepare(settings,job,root)
                self.assertEqual(len({w['start'] for w in line['_caption_preview_words'][layer['id']]}),1)
                settings['layers'] = [layer]
                preview, _ = prepare(settings,job,root)
            self.assertEqual(preview['_caption_preview_words'][layer['id']],words)
            self.assertTrue(align.call_args.kwargs['cache_only'])

    def test_audio_bound_dynamic_subtitles_keep_priority_without_new_alignment(self):
        from model.ElevenLabsTimings import save_timings
        with tempfile.TemporaryDirectory() as root:
            voice, layer, job, words = self.fixture(root)
            original = voice.with_suffix('.srt').read_bytes()
            dynamic = save_timings(voice, words, 3., source='test')
            with patch('app_plugins.builtin.batch_text_video.captions.load_word_timings', side_effect=AssertionError('no extra alignment')):
                result = task_word_timings(job, voice, layer, '', job['body'], 3., root)
                settings = {'layers':[layer], '_preview_voice':str(voice), '_editor_timing':{'duration':3.,'time':1.}}
                preview, _ = prepare(settings,job,root)
            self.assertEqual(result, words)
            self.assertEqual(preview['_caption_preview_words'][layer['id']], words)
            self.assertEqual(voice.with_suffix('.srt').read_bytes(), original)
            self.assertTrue(dynamic.is_file())


if __name__ == '__main__': unittest.main()
