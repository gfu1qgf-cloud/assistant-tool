import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from app_plugins.builtin.batch_text_video.caption_windows import subtitle_windows, recognition_windows, recognize_anchors
from app_plugins.builtin.batch_text_video.caption_alignment import align_verbatim
from app_plugins.builtin.batch_text_video.caption_alignment import repair_model_timings
from app_plugins.builtin.batch_text_video.captions import validate_words


def result(words):
    return SimpleNamespace(all_words=lambda: [SimpleNamespace(word=w['text'], start=w['start'], end=w['end']) for w in words])


class CaptionWindowTests(unittest.TestCase):
    def setUp(self):
        self.words = [{'text':f'slovo{i:03}', 'start':i*.5+.1, 'end':i*.5+.4} for i in range(107)]
        self.text = ' '.join(w['text'] for w in self.words)
        self.failed = [copy.deepcopy(w) if i < 61 else {**w, 'start':54., 'end':54.} for i,w in enumerate(self.words)]
        self.coarse = [{**w,'start':(i//10)*5.,'end':min(53.9,(i//10+1)*5.-.1),'cue':i//10} for i,w in enumerate(self.words)]
        self.model = Mock(); self.reports = []

    def run_alignment(self, loader):
        return align_verbatim(self.model, 'voice.mp3', self.text, 'sk', 55, lambda:None,
                              self.reports.append, validate_words, loader)

    def test_many_zero_words_use_real_window_realign_instead_of_relaxed_validation(self):
        with patch('stable_whisper.alignment.align', return_value=result(self.failed)) as whole, \
             patch('stable_whisper.alignment.align_words', return_value=result(self.words)) as window:
            output = self.run_alignment(lambda:self.coarse)
        self.assertEqual(output,self.words); self.assertEqual(whole.call_count,2); window.assert_called_once()
        self.model.transcribe.assert_not_called()
        arguments = window.call_args.kwargs
        self.assertFalse(arguments['normalize_text']); self.assertFalse(arguments['suppress_silence'])
        ranges = arguments['result']; self.assertGreater(len(ranges),1)
        self.assertTrue(all(w['end']-w['start'] <= 12.25 for w in ranges))
        self.assertEqual(''.join(w['text'].replace(' ','') for w in ranges),self.text.replace(' ',''))

    def test_mismatched_srt_uses_asr_ranges_but_not_asr_transcript_as_output(self):
        asr = [SimpleNamespace(word=w['text'].upper()+',',start=w['start'],end=w['end']) for w in self.words]
        self.model.transcribe.return_value = (iter([SimpleNamespace(words=asr)]), None)
        wrong = [{**w,'text':'WRONG'} for w in self.coarse]
        with patch('stable_whisper.alignment.align', return_value=result(self.failed)), \
             patch('stable_whisper.alignment.align_words', return_value=result(self.words)) as window:
            output = self.run_alignment(lambda:wrong)
        self.assertEqual(output,self.words); self.model.transcribe.assert_called_once()
        self.assertEqual(''.join(w['text'].replace(' ','') for w in window.call_args.kwargs['result']),self.text.replace(' ',''))
        self.assertTrue(any('不可用' in text for text in self.reports))

    def test_weak_asr_does_not_fabricate_windows(self):
        self.model.transcribe.return_value = (iter([SimpleNamespace(words=[SimpleNamespace(word='different',start=0.,end=1.)])]), None)
        with patch('stable_whisper.alignment.align',return_value=result(self.failed)), \
             patch('stable_whisper.alignment.align_words') as window, self.assertRaisesRegex(ValueError,'可靠匹配不足'):
            self.run_alignment(lambda:[])
        window.assert_not_called()

    def test_window_result_must_still_cover_every_original_word(self):
        with patch('stable_whisper.alignment.align', return_value=result(self.failed)), \
             patch('stable_whisper.alignment.align_words', return_value=result(self.words[:-1])), self.assertRaisesRegex(ValueError,'缺少或改变'):
            self.run_alignment(lambda:self.coarse)

    def test_window_results_with_many_zeros_are_not_accepted(self):
        with patch('stable_whisper.alignment.align', return_value=result(self.failed)), \
             patch('stable_whisper.alignment.align_words', return_value=result(self.failed)), self.assertRaisesRegex(ValueError,'46/107'):
            self.run_alignment(lambda:self.coarse)

    def test_cancel_in_subtitle_loader_is_not_swallowed(self):
        def cancel(): raise InterruptedError('cancel')
        with patch('stable_whisper.alignment.align',return_value=result(self.failed)), self.assertRaises(InterruptedError):
            self.run_alignment(cancel)
        self.model.transcribe.assert_not_called()

    def test_single_long_cue_is_not_split_by_equal_word_time(self):
        words = [{**w,'start':0.,'end':54.,'cue':0} for w in self.words]
        with self.assertRaisesRegex(ValueError,'太长'):
            subtitle_windows(words,self.text,55)

    def test_repeated_short_matches_are_not_weak_anchors(self):
        words = [{'text':'a','start':0.,'end':1.}]
        with self.assertRaises(ValueError): recognition_windows(words,'a b c d e f g h i',10)

    def test_asr_iterator_is_closed_on_cancellation(self):
        iterator = Mock(); iterator.__iter__ = Mock(return_value=iter([SimpleNamespace(words=[])]))
        self.model.transcribe.return_value = (iterator,None)
        def cancel(): raise InterruptedError('cancel')
        with self.assertRaises(InterruptedError): recognize_anchors(self.model,'voice.mp3','sk',cancel)
        iterator.close.assert_called_once()

    def test_grouping_does_not_mutate_srt_words(self):
        before = copy.deepcopy(self.coarse); ranges = subtitle_windows(self.coarse,self.text,55)
        self.assertEqual(before,self.coarse)
        self.assertTrue(all(a['end'] <= b['start']+.02 for a,b in zip(ranges,ranges[1:])))

    def test_verified_window_can_repair_one_leading_short_word_without_a_missing_block_guess(self):
        words = [{'text':'V','start':0.,'end':0.}, {'text':'čase','start':0.,'end':.26}, {'text':'nádeje.','start':.3,'end':.8}]
        with self.assertRaises(ValueError): repair_model_timings(words,1.,lambda _:None)
        repaired = repair_model_timings(words,1.,lambda _:None,edge_bounds=(0.,.92))
        self.assertEqual(repaired[0],{'text':'V','start':0.,'end':.04})
        self.assertEqual(repaired[1]['start'],.04); self.assertEqual(repaired[1]['end'],.26)
        self.assertEqual(words[0]['end'],0.)
        for changes in ({'text':'Neviem'}, {'start':.5,'end':.5}):
            with self.assertRaises(ValueError):
                repair_model_timings([{**words[0],**changes}]+words[1:],1.,lambda _:None,edge_bounds=(0.,.92))

    def test_edge_repairs_do_not_accept_an_unlocated_tail_or_two_leading_words(self):
        tail = [{'text':'Boh','start':.1,'end':.4},{'text':'že','start':3.,'end':3.}]
        with self.assertRaises(ValueError): repair_model_timings(tail,3.,lambda _:None,edge_bounds=(0.,1.))
        head = [{'text':'V','start':0.,'end':0.},{'text':'a','start':0.,'end':0.},{'text':'Boh','start':0.,'end':1.}]
        with self.assertRaises(ValueError): repair_model_timings(head,1.,lambda _:None,edge_bounds=(0.,1.))

    def test_body_only_matching_does_not_include_extra_spoken_title_or_tail(self):
        words = [{'text':'extra','start':0.,'end':.2}]+[{**w,'start':w['start']+2,'end':w['end']+2} for w in self.words]+[{'text':'outside','start':60.,'end':60.3}]
        windows = recognition_windows(words,self.text,65)
        self.assertGreater(windows[0]['start'],1.9)
        self.assertLess(windows[-1]['end'],56)
        self.assertEqual(''.join(w['text'].replace(' ','') for w in windows),self.text.replace(' ',''))

