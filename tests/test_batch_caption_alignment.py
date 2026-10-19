import copy
import gc
import os
from pathlib import Path
import random
import tempfile
import types
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.caption_alignment import original_words, align_verbatim, repair_model_timings
from app_plugins.builtin.batch_text_video.caption_random import contrast_unit, size_between
from app_plugins.builtin.batch_text_video.captions import new_caption, validate_words, caption_events
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, PREFIX
from app_plugins.builtin.batch_text_video.caption_style_render import groups_and_events
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.store import Store, fresh_state


def result(words):
    return types.SimpleNamespace(all_words=lambda: [types.SimpleNamespace(word=w['text'], start=w['start'], end=w['end']) for w in words])


class CaptionAlignmentTests(unittest.TestCase):
    def align(self, text, words, duration=3):
        reports = []
        with patch('stable_whisper.alignment.align', side_effect=[result(w) for w in words]) as mock:
            value = align_verbatim(object(), 'synthetic.wav', text, 'sk', duration, lambda: None, reports.append, validate_words)
        return value, reports, mock

    def test_original_input_preserves_abbreviation_case_and_punctuation(self):
        text = 'dr. Boh, že 2026... Amen!'
        words = [{'text': t, 'start': i * .4, 'end': i * .4 + .3} for i, t in enumerate(text.split())]
        output, _, model = self.align(text, [words])
        self.assertEqual(model.call_args.kwargs['text'], text)
        self.assertEqual([w['text'] for w in output], text.split())
        self.assertFalse(model.call_args.kwargs['remove_instant_words'])

    def test_token_and_punctuation_pieces_restore_whole_original_words(self):
        pieces = [{'text': 'Ľu', 'start': .1, 'end': .2}, {'text': 'dia', 'start': .2, 'end': .3},
                  {'text': ',', 'start': .3, 'end': .3}, {'text': 'že', 'start': .4, 'end': .5},
                  {'text': '.', 'start': .5, 'end': .5}]
        words = original_words(pieces, 'Ľudia, že.')
        self.assertEqual(words, [{'text': 'Ľudia,', 'start': .1, 'end': .3}, {'text': 'že.', 'start': .4, 'end': .5}])

    def test_unicode_canonical_equivalence_does_not_drop_accents(self):
        text = 'láska'
        word = {'text': 'la\u0301ska', 'start': 0., 'end': 1.}
        self.assertEqual(original_words([word], text)[0]['text'], text)
        with self.assertRaisesRegex(ValueError, '缺少或改变'):
            original_words([{**word, 'text': 'laska'}], text)

    def test_invalid_time_triggers_real_retry_before_any_local_repair(self):
        first = [{'text': 'Boh', 'start': .1, 'end': .4}, {'text': 'že', 'start': .5, 'end': .5},
                 {'text': 'príde.', 'start': .9, 'end': 1.4}]
        second = [first[0], {**first[1], 'end': .7}, first[2]]
        output, reports, model = self.align('Boh že príde.', [first, second])
        self.assertEqual(model.call_count, 2)
        self.assertEqual(model.call_args.kwargs['token_step'], 0)
        self.assertFalse(model.call_args.kwargs['suppress_silence'])
        self.assertEqual(output, second)
        self.assertFalse(any('局部修整' in report for report in reports))

    def test_isolated_internal_zero_uses_bounded_anchors_after_retry(self):
        words = [{'text': 'Boh', 'start': .1, 'end': .4}, {'text': 'že', 'start': .6, 'end': .6},
                 {'text': 'príde.', 'start': .9, 'end': 1.4}]
        output, reports, model = self.align('Boh že príde.', [words, words])
        self.assertEqual([w['text'] for w in output], ['Boh', 'že', 'príde.'])
        self.assertGreater(output[1]['end'], output[1]['start'])
        self.assertLessEqual(output[1]['end'] - output[1]['start'], .081)
        self.assertEqual(output[0], words[0]); self.assertEqual(output[2], words[2])
        self.assertEqual(model.call_count, 2)
        self.assertTrue(any('近似' in report for report in reports))

    def test_unaligned_tail_and_large_failures_are_not_guessed_or_deleted(self):
        words = [{'text': 'Boh', 'start': .1, 'end': .4}, {'text': 'že', 'start': 3., 'end': 3.}]
        with patch('stable_whisper.alignment.align', return_value=result(words)) as model:
            with self.assertRaisesRegex(ValueError, '末尾'):
                align_verbatim(object(), 'synthetic.wav', 'Boh že', 'sk', 3, lambda: None, lambda _: None, validate_words)
            self.assertEqual(model.call_count, 2)
        with self.assertRaisesRegex(ValueError, '零时长'):
            repair_model_timings([{'text': str(i), 'start': 1., 'end': 1.} for i in range(10)], 3, lambda _: None)

    def test_wrong_words_are_never_replaced_by_supplied_words(self):
        with self.assertRaisesRegex(ValueError, '缺少或改变'):
            original_words([{'text': 'Boh nie', 'start': .1, 'end': 1.}], 'Boh áno')

    def test_original_srt_validation_remains_strict(self):
        with self.assertRaises(ValueError):
            validate_words([{'text': 'že', 'start': 1., 'end': 1.}], 3)

    def test_cancellation_is_checked_from_library_progress_callback(self):
        checks = []
        def check():
            checks.append(1)
            if len(checks) > 1: raise InterruptedError('cancel')
        def inference(*args, **kwargs): kwargs['progress_callback'](1, 2)
        with patch('stable_whisper.alignment.align', side_effect=inference), self.assertRaises(InterruptedError):
            align_verbatim(object(), 'synthetic.wav', 'Boh', 'sk', 3, check, lambda _: None, validate_words)

    def test_approximate_local_repair_notice_is_retained_when_cache_is_reused(self):
        from globalValue import globalValue
        from app_plugins.builtin.batch_text_video.captions import load_word_timings
        words = [{'text': 'Boh', 'start': .1, 'end': .4}, {'text': 'že', 'start': .6, 'end': .6},
                 {'text': 'príde.', 'start': .9, 'end': 1.4}]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(globalValue, 'whisper_model_name', return_value='large-v3'), \
             patch.object(globalValue, 'get_whisper_model', return_value=object()), \
             patch('stable_whisper.alignment.align', return_value=result(words)) as model:
            path = Path(folder) / 'synthetic.wav'; path.write_bytes(b'synthetic data')
            first = load_word_timings(path, 'Boh že príde.', 3, folder)
            notices = []
            second = load_word_timings(path, 'Boh že príde.', 3, folder, report=notices.append)
            self.assertEqual(first, second); self.assertEqual(model.call_count, 2)
            self.assertTrue(any('局部修整' in notice and '近似' in notice for notice in notices))


class CaptionContrastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def tearDown(self):
        gc.collect(); QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_default_preserves_old_uniform_rng_and_stronger_expands_extremes(self):
        rng, old = random.Random(12), random.Random(12)
        for _ in range(20): self.assertEqual(size_between(rng, 48, 72, 1), old.uniform(48, 72))
        samples = [.1, .35, .45, .55, .65, .9]
        stronger = [contrast_unit(u, 3) for u in samples]
        self.assertTrue(all(abs(v - .5) >= abs(u - .5) for u, v in zip(samples, stronger)))
        self.assertEqual((stronger[0], stronger[-1]), (0., 1.))
        self.assertTrue(all(contrast_unit(u, 0) == .5 for u in samples))

    def test_invalid_contrast_is_rejected(self):
        for value in (-1, 4.1, True, float('nan'), '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_layers([{**new_caption(), 'caption_size_contrast': value}])

    def test_ui_and_profile_save_preserve_contrast(self):
        layer = new_caption(); editor = CaptionProperties(); editor.set_layer(layer)
        changed = []; editor.changed.connect(changed.append)
        editor.controls['caption_size_contrast'].setValue(2.75)
        self.assertEqual(changed[-1]['caption_size_contrast'], 2.75)
        editor.deleteLater()
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder); state = fresh_state(); state['settings']['layers'].append(changed[-1])
            store.save(state, 0)
            self.assertEqual(next(l for l in store.load()['settings']['layers'] if l['kind'] == 'caption')['caption_size_contrast'], 2.75)

    def test_contrast_changes_both_builtin_and_catalog_geometry_without_timing_changes(self):
        words = [{'text': 'Amen', 'start': .1 + i*.3, 'end': .3 + i*.3} for i in range(6)]
        for layer in (new_caption(), apply_style(new_caption(), PREFIX+'逐词弹出(随机大小)')):
            layer = normalize_layers([layer])[0]
            layer.update(width=96, height=80, caption_per_line=2, caption_words=2,caption_random_size=True)
            first = caption_events(words, layer, 1080, 1920, 3)
            stronger = caption_events(words, {**layer, 'caption_size_contrast': 3}, 1080, 1920, 3)
            self.assertEqual([(w['text'], w['start'], w['end']) for w in first], [(w['text'], w['start'], w['end']) for w in stronger])
            self.assertNotEqual([w['path'] for w in first], [w['path'] for w in stronger])
            self.assertEqual(stronger, caption_events(words, {**layer, 'caption_size_contrast': 3}, 1080, 1920, 3))

