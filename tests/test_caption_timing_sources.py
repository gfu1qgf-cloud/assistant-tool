"""Word-record discovery, manual picker and precise-mode grouping regressions."""
import copy
import gc
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from model.ElevenLabsTimings import matching_words, load_timings, save_timings
from model.node_workflow.contracts import ResourceRef, SubtitleTrack
from app_plugins.builtin.batch_text_video.captions import (
    new_caption, caption_events, task_word_timings, read_task_subtitle)
from app_plugins.builtin.batch_text_video.components import resolve_sources, new_text_component
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.node_effects import input_subtitles
from app_plugins.builtin.batch_text_video.workflow_caption import subtitle_words
from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
from app_plugins.builtin.batch_text_video.export_results_ui import show_export_warning

PREFIX = 'app_plugins.builtin.batch_text_video.'
TOKENS = ['v','jej','srdci','zanechať','dlhotrvajúcu','bolesť.','Po','druhé,','neprehliadaj']
WORDS = [dict(text=t,start=.2+i*.5,end=.5+i*.5) for i,t in enumerate(TOKENS)]
TEXT = 'v jej srdci zanechať dlhotrvajúcu bolesť.Po druhé, neprehliadaj'


def layer(**values):
    result = new_caption(); result.update(values)
    return normalize_layers([result])[0]


class WordRecordMatchTests(unittest.TestCase):
    def test_missing_space_after_punctuation_preserves_all_supplied_word_times(self):
        matched = matching_words(WORDS,TEXT)
        self.assertEqual(matched,WORDS)
        self.assertEqual(matching_words(WORDS,TEXT.upper()),[dict(w,text=w['text'].upper()) for w in WORDS])

    def test_body_subset_must_start_and_end_on_real_word_boundaries(self):
        self.assertEqual(matching_words(WORDS,'dlhotrvajúcu bolesť.Po druhé,'),WORDS[4:8])
        for text in ('trvajúcu bolesť.Po','bolesť.Po dru','dlhotrvajúcu bolesť.Iné'):
            with self.subTest(text=text),self.assertRaises(ValueError): matching_words(WORDS,text)

    def test_standalone_dash_before_body_does_not_invalidate_other_word_spans(self):
        prefix=[dict(text='Nádej',start=0,end=.05),dict(text='–',start=.06,end=.1)]
        self.assertEqual(matching_words(prefix+WORDS,'Nádej – '+TEXT),prefix+WORDS)
        self.assertEqual(matching_words(prefix+WORDS,TEXT),WORDS)

    def test_no_ambiguous_span_or_arbitrary_joined_word(self):
        with self.assertRaises(ValueError): matching_words(WORDS+WORDS,TEXT)
        with self.assertRaises(ValueError): matching_words([dict(text='the',start=0,end=.3),
            dict(text='rapist',start=.4,end=.8)],'therapist')

    def test_no_splitting_one_service_word_into_invented_word_times(self):
        with self.assertRaises(ValueError): matching_words([dict(text='bolesť.Po',start=0,end=1)],'bolesť. Po')

    def test_fingerprinted_pair_reuses_exact_times_despite_punctuation_spacing(self):
        with tempfile.TemporaryDirectory() as directory:
            voice = Path(directory)/'task_audio.mp3'; voice.write_bytes(b'fixture-audio')
            original = voice.with_suffix('.srt'); original.write_bytes(b'original-davinci-srt')
            dynamic = save_timings(voice,WORDS,5.,source='test')
            before = {p:p.read_bytes() for p in (voice,original,dynamic,dynamic.with_suffix('.json'))}
            self.assertEqual(load_timings(voice,TEXT,5.),WORDS)
            self.assertEqual({p:p.read_bytes() for p in before},before)
            messages=[]
            self.assertIsNone(load_timings(voice,'An entirely different text',5.,report=messages.append))
            self.assertTrue(any('未使用' in m for m in messages))
            voice.write_bytes(b'changed-audio')
            self.assertIsNone(load_timings(voice,TEXT,5.))


class CaptionSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.voice = self.directory/'task_audio.mp3'; self.voice.write_bytes(b'fixture-audio')
        self.srt = self.voice.with_suffix('.srt')
        self.srt.write_text('1\n00:00:00,200 --> 00:00:04,800\n'+' '.join(TOKENS)+'\n',encoding='utf8')
        self.job = dict(task_dir=str(self.directory),voice_path=str(self.voice),task_voice_path=str(self.voice))

    def tearDown(self):
        gc.collect()
        self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def native_words(self,value):
        return task_word_timings(self.job,str(self.voice),value,'',TEXT,5.,self.directory)

    def test_auto_and_task_modes_prefer_existing_ele_words_over_ordinary_srt(self):
        save_timings(self.voice,WORDS,5.,source='test')
        before = self.srt.read_bytes()
        for mode in ('auto','task'):
            with (self.subTest(mode=mode),patch(PREFIX+'captions.load_word_timings',side_effect=AssertionError('model')),
                    patch(PREFIX+'captions.read_task_subtitle',side_effect=AssertionError('coarse srt'))):
                result = self.native_words(layer(caption_timing=mode))
            self.assertEqual(result,WORDS)
        self.assertEqual(self.srt.read_bytes(),before)

    def test_explicit_local_model_never_falls_back_to_ele_or_a_coarse_srt(self):
        save_timings(self.voice,WORDS,5.,source='test')
        with (patch('model.ElevenLabsTimings.load_timings',side_effect=AssertionError('ele')),
                patch(PREFIX+'captions.read_task_subtitle',side_effect=AssertionError('srt')),
                patch(PREFIX+'captions.load_word_timings',return_value=WORDS) as local):
            self.assertEqual(self.native_words(layer(caption_timing='model')),WORDS)
            self.assertEqual(local.call_count,1)

    def test_explicit_alignment_not_overwritten_by_old_external_source(self):
        value = layer(caption_timing='model',source='file',source_file=str(self.directory/'missing.srt'))
        result,steps = resolve_sources([value],'',TEXT,job=self.job)
        self.assertEqual(result[0]['caption_timing'],'model')
        self.assertEqual(result[0]['source'],'voice')
        self.assertEqual(steps,[])
        self.assertEqual(value['source'],'file')  # Derived data only, no config rewrite.

    def test_manual_dynamic_srt_is_word_aligned_and_uses_the_group_setting(self):
        dynamic = save_timings(self.voice,WORDS,5.,source='test')
        value = layer(caption_timing='file',caption_file=str(dynamic),caption_words=6,caption_per_line=4,
                      font_min=32,font_max=48,width=95,height=50)
        with (patch(PREFIX+'captions.load_word_timings',side_effect=AssertionError('model')),
                patch('model.ElevenLabsTimings.load_timings',side_effect=AssertionError('cloud metadata'))):
            words = self.native_words(value)
        self.assertTrue(all('cue' not in w for w in words))
        self.assertEqual([(w['start'],w['end']) for w in words],[(w['start'],w['end']) for w in WORDS])
        events = caption_events(words,value,1080,1920,5.)
        self.assertEqual([sum(e['group']==g for e in events) for g in sorted({e['group'] for e in events})],[6,3])
        self.assertEqual([e['start'] for e in events],[w['start'] for w in WORDS])

    def test_node_input_port_passes_dynamic_track_without_calling_any_api(self):
        save_timings(self.voice,WORDS,5.,source='test')
        before = self.srt.read_bytes()
        with patch(PREFIX+'captions.load_word_timings',side_effect=AssertionError('model')):
            track = input_subtitles(self.job,str(self.voice),5.,task_key='fixture-task')
        self.assertIsInstance(track,SubtitleTrack)
        self.assertEqual(track.timing,'word_aligned')
        self.assertEqual(subtitle_words(track,5.),WORDS)
        self.assertEqual(self.srt.read_bytes(),before)

    def test_node_manual_srt_keeps_exact_word_times_not_one_page_per_word(self):
        dynamic = save_timings(self.voice,WORDS,5.,source='test')
        result = subtitle_words(ResourceRef.capture(dynamic,'subtitles'),5.)
        self.assertEqual(result,WORDS)
        self.assertTrue(all('cue' not in w for w in result))

    def test_coarse_srt_is_not_silently_interpolated(self):
        words = self.native_words(layer(caption_timing='task'))
        self.assertEqual({w['start'] for w in words},{.2})
        self.assertEqual({w['end'] for w in words},{4.8})
        self.assertTrue(all('cue' in w for w in words))

    def test_six_word_limit_long_words_wrap_before_four_words_without_splitting(self):
        tokens = ['dlhotrvajúcu','neprehliadaj','každodenný','zanechať','rodine','nádej']*2
        words = [dict(text=t,start=i*.5,end=i*.5+.3) for i,t in enumerate(tokens)]
        value = layer(caption_timing='model',caption_words=6,caption_per_line=4,font_min=60,font_max=60,
                      caption_random_size=False,width=66,height=52,caption_preset='appear')
        events = caption_events(words,value,1080,1920,6.)
        self.assertEqual([e['text'] for e in events],tokens)
        self.assertEqual([e['start'] for e in events],[w['start'] for w in words])
        self.assertEqual([sum(e['group']==g for e in events) for g in (0,1)],[6,6])
        self.assertTrue(any(sum(e['group']==0 and e['line']==line for e in events)<4 for line in {e['line'] for e in events}))
        self.assertTrue(all(e['x']+e['width'] <= 1080*.83 for e in events))

    def panel(self,value=None):
        value = value or layer(caption_timing='model',caption_words=6,caption_per_line=4)
        panel = LayerPanel([value,new_text_component()],self.directory)
        panel.select_layer_id(value['id'])
        self.addCleanup(panel.deleteLater)
        return panel

    def test_manual_mode_shows_inline_picker_and_saves_path_canonically(self):
        panel = self.panel(); props = panel.caption_properties
        combo = props.controls['caption_timing']
        self.assertTrue(props.file_row.isHidden())
        combo.setCurrentIndex(combo.findData('file'))
        self.assertFalse(props.file_row.isHidden())
        with patch.object(QtWidgets.QFileDialog,'getOpenFileName',return_value=(str(self.srt),'')):
            props.file_button.click()
        self.assertEqual(panel.current()['caption_file'],str(self.srt))
        self.assertEqual(panel.current()['source_file'],str(self.srt))
        identifier = panel.current()['id']
        panel.select_layer_id(panel.values()[1]['id']); panel.select_layer_id(identifier)
        self.assertEqual(props.controls['caption_file'].text(),str(self.srt))
        combo.setCurrentIndex(combo.findData('model'))
        self.assertEqual(panel.current()['source'],'voice')
        self.assertEqual(panel.current()['caption_timing'],'model')
        self.assertEqual(panel.current()['caption_words'],6)

    def test_local_mode_and_groups_survive_source_refresh_and_selection(self):
        panel = self.panel(); identifier=panel.current()['id']
        source = panel.editor.content_editor
        source.source_changed()  # Re-selecting task source is not permission to reset model.
        panel.select_layer_id(panel.values()[1]['id']); panel.select_layer_id(identifier)
        panel.rebuild(identifier)
        self.assertEqual(panel.current()['caption_timing'],'model')
        self.assertEqual(panel.current()['caption_words'],6)
        self.assertEqual(panel.current()['caption_per_line'],4)

    def test_node_editor_never_exposes_a_competing_file_picker(self):
        editor = CaptionNodeEditor({},lambda _v:None)
        self.addCleanup(editor.deleteLater)
        editor.mode.setCurrentIndex(1)
        props = editor.panel.caption_properties
        combo=props.controls['caption_timing']; combo.setCurrentIndex(combo.findData('uniform'))
        self.assertFalse(props.allow_manual_file)
        self.assertTrue(props.file_row.isHidden())

    def test_source_node_manual_strategy_exposes_picker_and_model_switch_is_explicit(self):
        from app_plugins.builtin.batch_text_video.workflow_component_source_ui import ComponentSourceEditor
        updates=[]; editor=ComponentSourceEditor({'source':new_caption()},updates.append)
        self.addCleanup(editor.deleteLater)
        timing=editor.timing; timing.setCurrentIndex(timing.findData('file'))
        self.assertFalse(editor.content.file_row.isHidden())
        with patch.object(QtWidgets.QFileDialog,'getOpenFileName',return_value=(str(self.srt),'')):
            editor.content.choose_file()
        self.assertEqual(updates[-1]['source']['caption_file'],str(self.srt))
        timing.setCurrentIndex(timing.findData('model'))
        self.assertEqual(updates[-1]['source']['caption_timing'],'model')
        self.assertEqual(updates[-1]['source']['source'],'voice')

    def test_failure_summary_is_visible_nonmodal_and_shown_only_once_per_call(self):
        owner=QtWidgets.QWidget(); self.addCleanup(owner.deleteLater)
        result=dict(completed=2,failed=1,failures=['字幕时间戳无效：请检查动态SRT'])
        message=show_export_warning(owner,result)
        self.assertTrue(message.isVisible())
        self.assertFalse(message.isModal())
        self.assertIn('时间戳无效',message.informativeText())
        again=show_export_warning(owner,result)
        self.assertIs(owner._batch_warning,again)
        self.assertFalse(message.isVisible())

    def test_text_defaults_keep_twenty_but_davinci_uses_natural_spaces(self):
        from app_plugins.builtin.batch_text_video.layers import componentize_layers
        from app_plugins.builtin.batch_text_video.store import fresh_state
        from app_plugins.builtin.batch_text_video.caption_styles import apply_style
        self.assertEqual(new_text_component()['word_spacing'],20)
        self.assertEqual(new_caption()['word_spacing'],20)
        self.assertEqual(apply_style(new_caption(),'videokit:默认白字')['word_spacing'],20)
        self.assertEqual(apply_style(new_caption(),'davinci:lzx_word_pop')['word_spacing'],0)
        self.assertEqual(layer(word_spacing=0)['word_spacing'],0)
        self.assertTrue(all(l['word_spacing']==20 for l in
            componentize_layers(None,fresh_state()['settings']) if l['kind'] in {'title','body'}))
