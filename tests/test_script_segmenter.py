import copy
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import tempfile
import random
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app_plugins.api import MAIN_MENU, TASK_CONTEXT_MENU
from app_plugins.builtin.script_segmenter.engine import (
    CONFIG_KEY, format_result, normalize_settings, run_segmentation, split_copy, word_spans,
)
from app_plugins.builtin.script_segmenter.plugin import ScriptSegmenterPlugin
from app_plugins.builtin.script_segmenter.ui import ScriptSegmenterDialog
from model.BackgroundTasks import BackgroundTaskCanceled, BackgroundTaskManager, TaskRunContext
from model.ClipboardHelper import is_internal_clipboard_content
from qt_compat import QtCore, QtGui, QtTest, QtWidgets


class SegmentationEngineTests(unittest.TestCase):
    def assert_integrity(self, source, result):
        self.assertEqual("".join(source[s["start"]:s["end"]] for s in result["segments"]), source)
        self.assertEqual(result["segments"][0]["start"], 0)
        self.assertEqual(result["segments"][-1]["end"], len(source))
        cuts = {s["end"] for s in result["segments"][:-1]}
        for start, end in word_spans(source):
            self.assertFalse(any(start < cut < end for cut in cuts), source[start:end])
        for item in result["segments"]:
            self.assertEqual(item["text"], source[item["start"]:item["end"]].strip())
            if not item["warning"]:
                self.assertLessEqual(item["estimated_seconds"] + result["settings"]["padding_seconds"],
                                     item["seconds"] + .001)
            elif '已并入上一段' in item['warning']:
                self.assertIs(item, result['segments'][-1])
                self.assertLessEqual(item['estimated_seconds'] + result['settings']['padding_seconds'],
                                     item['seconds'] + 3 + .001)

    def test_prefers_ten_seconds_and_minimal_segments_then_short_tail(self):
        source = " ".join(["amen"] * 60)
        result = split_copy(source)
        self.assertEqual([s["seconds"] for s in result["segments"]], [10, 10, 6])
        self.assertEqual([s["word_count"] for s in result["segments"]], [24, 24, 12])
        self.assert_integrity(source, result)

    def test_fixed_duration_modes(self):
        for duration in (6, 8, 10):
            source = " ".join(["amen"] * 100)
            result = split_copy(source, {"durations": [duration]})
            self.assertTrue(all(s["seconds"] == duration for s in result["segments"]))
            self.assert_integrity(source, result)

    def test_optional_punctuation_strategy_retreats_instead_of_starting_next_phrase(self):
        phrase = ' '.join(['amen'] * 14)
        source = phrase + ', ' + phrase + ', ' + phrase + '.'
        old = split_copy(source)
        result = split_copy(source, {'boundary_strategy': 'punctuation_first'})
        self.assertEqual(len(old['segments']), 2)
        self.assertEqual(len(result['segments']), 3)
        self.assertEqual([s['text'] for s in result['segments']], [phrase+',', phrase+',', phrase+'.'])
        self.assertEqual([s['seconds'] for s in result['segments']], [8, 8, 8])
        self.assert_integrity(source, result)
        self.assertEqual(normalize_settings({})['boundary_strategy'], 'fewest')
        self.assertEqual(normalize_settings({'boundary_strategy': 'unknown'})['boundary_strategy'], 'fewest')

    def test_all_four_duration_rules_are_available_with_new_strategy(self):
        for durations in ([6, 8, 10], [10], [8], [6]):
            source = (' '.join(['amen'] * 14) + ', ') * 7
            result = split_copy(source, {'durations': durations, 'boundary_strategy': 'punctuation_first'})
            self.assertTrue(all(s['seconds'] in durations for s in result['segments']))
            self.assert_integrity(source, result)

    def test_punctuation_strategy_absorbs_short_tail_and_preserves_result_fields(self):
        source = ' '.join(['amen'] * 14) + '. ' + ' '.join(['amen'] * 22) + '. Amen.'
        result = split_copy(source, {'boundary_strategy': 'punctuation_first'})
        self.assertEqual([s['word_count'] for s in result['segments']], [14, 23])
        last = result['segments'][-1]
        self.assertEqual(last['index'], 2)
        self.assertEqual(last['seconds'], 10)
        self.assertEqual(last['estimated_seconds'], 9.8)
        self.assertEqual(last['spoken_word_count'], 23)
        self.assertEqual(last['boundary'], '文案结束')
        self.assertIn('已并入上一段', last['warning'])
        self.assertIn('0.20秒', last['warning'])
        self.assertEqual(result['warning_count'], 1)
        self.assertEqual(result['estimated_seconds'], 15.7)
        self.assertEqual(result['video_seconds'], 18)
        self.assertIn('超出10秒，需人工处理', format_result(result))
        self.assertTrue(format_result(result).endswith(last['text']))
        self.assert_integrity(source, result)

    def test_short_tail_merge_applies_to_all_four_duration_rules(self):
        for durations in ([6, 8, 10], [10], [8], [6]):
            with self.subTest(durations=durations):
                maximum = max(durations)
                source = ' '.join(['amen'] * (maximum - 1)) + '. Amen.'
                result = split_copy(source, {'durations': durations, 'words_per_minute': 60,
                                            'pause_seconds': 0, 'boundary_strategy': 'punctuation_first'})
                self.assertEqual(len(result['segments']), 1)
                self.assertEqual(result['segments'][0]['seconds'], maximum)
                self.assertAlmostEqual(result['segments'][0]['estimated_seconds'], maximum)
                self.assertIn('0.40秒', result['segments'][0]['warning'])
                self.assert_integrity(source, result)

    def test_short_tail_merge_allows_exactly_three_seconds_overrun_but_no_more(self):
        first = ' '.join(['amen'] * 6) + '. '
        settings = {'durations': [6], 'words_per_minute': 60, 'pause_seconds': 0,
                    'padding_seconds': 0, 'boundary_strategy': 'punctuation_first'}
        at_limit = first + ' '.join(['amen'] * 3) + '.'
        result = split_copy(at_limit, settings)
        self.assertEqual(len(result['segments']), 1)
        self.assertEqual(result['segments'][0]['estimated_seconds'], 9)
        self.assertIn('3.00秒', result['segments'][0]['warning'])
        self.assert_integrity(at_limit, result)
        # A long word adds a fractional reading unit, just beyond the boundary.
        above_limit = first + 'amen amen abcdefghijklm.'
        result = split_copy(above_limit, settings)
        self.assertEqual(len(result['segments']), 2)
        self.assertEqual(result['warning_count'], 0)
        self.assert_integrity(above_limit, result)

    def test_short_tail_merge_counts_padding_in_overrun(self):
        source = ' '.join(['amen'] * 5) + '. amen amen.'
        result = split_copy(source, {'durations': [6], 'words_per_minute': 60,
                                    'pause_seconds': 0, 'padding_seconds': 1,
                                    'boundary_strategy': 'punctuation_first'})
        self.assertEqual(len(result['segments']), 1)
        self.assertEqual(result['segments'][0]['estimated_seconds'], 7)
        self.assertIn('2.00秒', result['segments'][0]['warning'])
        self.assert_integrity(source, result)

    def test_short_tail_merge_does_not_absorb_oversized_units(self):
        settings = {'boundary_strategy': 'punctuation_first'}
        for source in ('1' * 50 + '. Amen.', 'Amen. ' + '1' * 50 + '.'):
            with self.subTest(source=source):
                result = split_copy(source, settings)
                self.assertEqual(len(result['segments']), 2)
                self.assertEqual(result['warning_count'], 1)
                self.assertNotIn('已并入上一段', format_result(result))
                self.assertTrue(any('单个完整词' in s['warning'] for s in result['segments']))
                self.assert_integrity(source, result)

    def test_short_tail_merge_only_changes_last_pair_and_keeps_list_marker_and_quote(self):
        source = (' '.join(['amen'] * 6) + '.\n') * 3 + '1. “Amen!”'
        result = split_copy(source, {'durations': [6], 'words_per_minute': 60,
                                    'pause_seconds': 0, 'padding_seconds': 0,
                                    'boundary_strategy': 'punctuation_first'})
        self.assertEqual([s['word_count'] for s in result['segments']], [6, 6, 8])
        self.assertTrue(result['segments'][-1]['text'].endswith('1. “Amen!”'))
        self.assertEqual(result['warning_count'], 1)
        self.assert_integrity(source, result)

    def test_fewest_strategy_and_single_short_source_keep_existing_behavior(self):
        source = ' '.join(['amen'] * 24) + '. Amen.'
        old = split_copy(source, {'boundary_strategy': 'fewest'})
        self.assertEqual(len(old['segments']), 2)
        self.assertEqual(old['warning_count'], 0)
        self.assert_integrity(source, old)
        short = split_copy('Amen.', {'boundary_strategy': 'punctuation_first'})
        self.assertEqual(len(short['segments']), 1)
        self.assertEqual(short['warning_count'], 0)
        self.assert_integrity('Amen.', short)

    def test_new_strategy_combines_short_complete_sentences_and_falls_back_for_long_ones(self):
        short = 'Amen. Boh nás miluje. Mária plače.'
        self.assertEqual(len(split_copy(short, {'boundary_strategy': 'punctuation_first'})['segments']), 1)
        source = 'Amen. ' + ' '.join(['amen'] * 70) + '.'
        result = split_copy(source, {'boundary_strategy': 'punctuation_first'})
        self.assertEqual(result['segments'][0]['text'], 'Amen.')
        self.assertTrue(any(s['boundary'] == '词间' for s in result['segments']))
        self.assertEqual(result['warning_count'], 0)
        self.assert_integrity(source, result)

    def test_new_strategy_keeps_numbers_quotes_abbreviations_and_oversized_words_whole(self):
        source = 'Dr. Mária 24:11, “' + '2026 ' * 22 + 'Amen!”'
        result = split_copy(source, {'boundary_strategy': 'punctuation_first'})
        self.assert_integrity(source, result)
        self.assertEqual(result['segments'][0]['text'], 'Dr. Mária 24:11,')
        self.assertTrue(result['segments'][1]['text'].startswith('“'))
        too_long = '1234567890' * 5
        result = split_copy(too_long, {'boundary_strategy': 'punctuation_first'})
        self.assertEqual(result['segments'][0]['text'], too_long)
        self.assertEqual(result['warning_count'], 1)
        with self.assertRaises(BackgroundTaskCanceled):
            split_copy(source, {'boundary_strategy': 'punctuation_first'}, canceled=lambda: True)

    def test_random_new_strategy_preserves_every_character_and_time_limit(self):
        rng = random.Random(20261008)
        for _ in range(40):
            source = ''.join(rng.choice(['amen', '2026', '24:11', 'Mária', "don't"]) +
                             rng.choice([' ', ', ', '. ', '; ', '\n']) for _ in range(rng.randint(10, 130)))
            self.assert_integrity(source, split_copy(source, {'boundary_strategy': 'punctuation_first'}))

    def test_numbers_are_counted_even_without_any_letters(self):
        source = " ".join(["7"] * 60)
        result = split_copy(source)
        self.assertEqual([s["word_count"] for s in result["segments"]], [24, 24, 12])
        self.assertEqual(result["estimated_seconds"], 24)
        self.assert_integrity(source, result)

    def test_list_marker_does_not_end_previous_segment_like_task_316(self):
        # Same failure shape as task 316, without retaining customer copy.
        source = ' '.join(['amen'] * 20) + '.\n\n1. Pane Ježišu Kriste, ' + 'amen ' * 80
        for strategy in ('fewest','punctuation_first'):
            for durations in ([6,8,10],[10],[8],[6]):
                with self.subTest(strategy=strategy,durations=durations):
                    result = split_copy(source,{'boundary_strategy':strategy,'durations':durations})
                    self.assertTrue(any('1. Pane' in s['text'] for s in result['segments']))
                    self.assertFalse(any(re.search(r'\b1\.$',s['text']) for s in result['segments']))
                    expected_warnings = int(strategy == 'punctuation_first' and durations == [8])
                    self.assertEqual(result['warning_count'], expected_warnings)
                    if expected_warnings:
                        self.assertIn('已并入上一段', result['segments'][-1]['warning'])
                    self.assert_integrity(source,result)

    def test_list_marker_formats_stay_attached_with_spaces_indents_and_line_endings(self):
        for marker in ('1.', '12)', '(3)', '4、', '5：', '６）', '7.Pane'):
            for newline in ('\n','\r','\r\n'):
                content = marker if marker == '7.Pane' else marker+' Pane'
                source = ' '.join(['amen']*23) + '.' + newline + '\t ' + content + ' amen ' * 30
                for strategy in ('fewest','punctuation_first'):
                    with self.subTest(marker=marker,newline=newline,strategy=strategy):
                        result = split_copy(source,{'boundary_strategy':strategy})
                        self.assertTrue(any(content in s['text'] for s in result['segments']))
                        self.assert_integrity(source,result)

    def test_list_number_counts_for_timing_without_a_false_sentence_pause(self):
        for source in ('1. Pane', '1) Pane', '(1) Pane', '1.Pane'):
            result = split_copy(source)
            self.assertEqual(result['estimated_seconds'],.8)
            self.assertEqual(result['segments'][0]['word_count'],2)
            self.assertEqual(result['segments'][0]['spoken_word_count'],2)
            self.assert_integrity(source,result)
        for source,expected in (('2026. Pane',2.3),('24:11 Pane',2.4),('3.14 Pane',2.0),('22.12.2026 Pane',4.4)):
            self.assertEqual(split_copy(source)['estimated_seconds'],expected,source)

    def test_oversized_numbered_word_is_warned_as_one_unit_not_stranded(self):
        source = 'Amen.\n1. ' + 'x'*400
        for strategy in ('fewest','punctuation_first'):
            result = split_copy(source,{'boundary_strategy':strategy,'durations':[6]})
            self.assertEqual(result['warning_count'],1)
            warned = next(s for s in result['segments'] if s['warning'])
            self.assertTrue(warned['text'].startswith('1. '))
            self.assertIn('段落序号',warned['warning'])
            self.assert_integrity(source,result)

    def test_random_numbered_paragraphs_keep_all_numbers_with_the_following_word(self):
        rng = random.Random(20261009)
        for _ in range(30):
            source = '\n'.join(f'{index}. Pane '+''.join('amen'+rng.choice([' ', ', ', '. ', '; '])
                for _word in range(rng.randint(10,50))) for index in range(1,5))
            for strategy in ('fewest','punctuation_first'):
                result = split_copy(source,{'boundary_strategy':strategy})
                for index in range(1,5):
                    self.assertTrue(any(f'{index}. Pane' in s['text'] for s in result['segments']))
                self.assert_integrity(source,result)

    def test_multidigit_numbers_have_real_reading_budget(self):
        self.assertEqual(split_copy("2026")["estimated_seconds"], 1.6)
        self.assertEqual(split_copy("24:11")["estimated_seconds"], 2.0)
        for value in ("2026", "24:11", "8:30", "3.14", "1/2", "22.12.2026", "1-10", "Mária2026", "２０２６"):
            result = split_copy(" ".join([value] * 24))
            self.assertGreater(len(result["segments"]), 1, value)
            self.assert_integrity(result["source"], result)
            self.assertTrue(all(s["text"].split() == [value] * s["word_count"] for s in result["segments"]))

    def test_long_numeric_token_is_warned_not_cut_or_silently_squeezed_in(self):
        source = "1234567890" * 5
        result = split_copy(source)
        self.assertEqual(result["warning_count"], 1)
        self.assertEqual(result["segments"][0]["text"], source)
        self.assertEqual(result["segments"][0]["spoken_word_count"], 50)
        self.assert_integrity(source, result)

    def test_number_timing_respects_speed_and_padding(self):
        source = "2026 " * 30
        self.assertGreater(len(split_copy(source, {"words_per_minute": 90})["segments"]),
                           len(split_copy(source, {"words_per_minute": 240})["segments"]))
        self.assertGreater(len(split_copy(source, {"padding_seconds": 2})["segments"]),
                           len(split_copy(source)["segments"]))

    def test_numeric_punctuation_stays_inside_tokens_and_does_not_change_cut_policy(self):
        source = "Matúš 24:11, Mária 2026. " * 10
        result = split_copy(source)
        self.assert_integrity(source, result)
        self.assertEqual([source[a:b] for a, b in word_spans(source)][:4], ["Matúš", "24:11", "Mária", "2026"])
        self.assertTrue(all(s["seconds"] == 10 for s in result["segments"][:-1]))

    def test_random_punctuation_keeps_minimum_feasible_video_count(self):
        rng = random.Random(20261007)
        for _index in range(45):
            units = ["amen" + rng.choice([" ", " ", ", ", ". ", "; ", "\n"])
                     for _word in range(rng.randint(20, 140))]
            source = "".join(units)
            # All durations are additive; greedily fill each maximum-size
            # interval to calculate an independent minimum count bound.
            minimum, used = 1, 0.0
            for unit in units:
                duration = split_copy(unit)["estimated_seconds"]
                if used + duration > 9.6 + 1e-9:
                    minimum += 1
                    used = 0
                used += duration
            result = split_copy(source)
            self.assertEqual(len(result["segments"]), minimum)
            self.assert_integrity(source, result)

    def test_natural_sentence_boundary_is_preferred_when_it_fits(self):
        sentence = " ".join(["amen"] * 20) + ". "
        source = sentence * 3
        result = split_copy(source)
        self.assertEqual(len(result["segments"]), 3)
        self.assertTrue(all(s["text"].endswith(".") for s in result["segments"]))
        self.assertEqual([s["boundary"] for s in result["segments"][:-1]], ["句末", "句末"])
        self.assert_integrity(source, result)

    def test_short_sentences_are_combined_not_one_video_per_sentence(self):
        source = "Amen. Boh nás miluje. Nevynechaj to! Mária plače."
        result = split_copy(source)
        self.assertEqual(len(result["segments"]), 1)
        self.assert_integrity(source, result)

    def test_accents_apostrophes_hyphens_and_numeric_units_are_never_cut(self):
        units = ["Mária", "POŠLI", "don't", "d’Artagnan", "cafe\u0301", "čierno-biely", "8:30", "3.14", "24,11", "1/2"]
        source = "  „" + " ".join(units * 12) + "!“\n"
        result = split_copy(source, {"durations": [6]})
        self.assert_integrity(source, result)
        tokens = [source[a:b] for a, b in word_spans(source)]
        self.assertEqual(tokens, units * 12)
        self.assertTrue(result["segments"][0]["text"].startswith("„Mária"))
        self.assertTrue(result["segments"][-1]["text"].endswith("!“"))

    def test_url_email_and_decimal_have_no_internal_split(self):
        source = ("Prosím "+" ".join(["amen"] * 22)+" https://example.com/a/b?q=one-two. "
                  "mail.name@example.com. 12:34 2.718 amen.")
        result = split_copy(source)
        self.assert_integrity(source, result)
        tokens = [source[a:b] for a, b in word_spans(source)]
        for unit in ("https://example.com/a/b?q=one-two", "mail.name@example.com", "12:34", "2.718"):
            self.assertIn(unit, tokens)

    def test_opening_quote_attaches_to_next_word(self):
        source = " ".join(["amen"] * 24) + ' “Mária plače.”'
        result = split_copy(source)
        self.assert_integrity(source, result)
        self.assertTrue(result["segments"][1]["text"].startswith("“Mária"))
        self.assertFalse(result["segments"][0]["text"].endswith("“"))

    def test_abbreviation_is_not_a_sentence_pause(self):
        plain = split_copy("Dr Mária")
        abbreviation = split_copy("Dr. Mária")
        sentence = split_copy("Amen. Mária")
        self.assertEqual(plain["estimated_seconds"], abbreviation["estimated_seconds"])
        self.assertGreater(sentence["estimated_seconds"], abbreviation["estimated_seconds"])

    def test_cjk_punctuation_and_long_units_are_preserved_and_warned(self):
        source = "今天要好好吃饭，明天也要照顾自己。" * 8
        result = split_copy(source)
        self.assert_integrity(source, result)
        self.assertEqual(result["warning_count"], 0)
        long_source = "今" * 100
        long_result = split_copy(long_source)
        self.assertEqual(len(long_result["segments"]), 1)
        self.assertEqual(long_result["warning_count"], 1)
        self.assertIn("需人工处理", format_result(long_result))
        self.assert_integrity(long_source, long_result)

    def test_unbreakable_oversized_latin_word_is_not_silently_accepted(self):
        source = "x" * 400
        result = split_copy(source)
        self.assertEqual(result["warning_count"], 1)
        self.assertEqual(result["segments"][0]["text"], source)
        self.assertGreater(result["estimated_seconds"], 10)

    def test_speed_padding_and_pause_are_effective(self):
        source = " ".join(["amen"] * 60)
        slow = split_copy(source, {"words_per_minute": 90})
        fast = split_copy(source, {"words_per_minute": 240})
        padded_source = " ".join(["amen"] * 65)
        padded = split_copy(padded_source, {"padding_seconds": 2})
        self.assertGreater(len(slow["segments"]), len(fast["segments"]))
        self.assertGreater(len(padded["segments"]), len(split_copy(padded_source)["segments"]))
        self.assertGreater(split_copy("Amen. " * 20, {"pause_seconds": 1})["estimated_seconds"],
                           split_copy("Amen. " * 20, {"pause_seconds": 0})["estimated_seconds"])

    def test_invalid_or_nonfinite_settings_are_normalized(self):
        settings = normalize_settings({"durations": ["6", None, {}], "preferred_seconds": "bad",
            "words_per_minute": float("nan"), "characters_per_minute": -10,
            "pause_seconds": "not a number", "padding_seconds": float("inf")})
        self.assertEqual(settings["durations"], [6, 8, 10])
        self.assertEqual(settings["preferred_seconds"], 10)
        self.assertEqual(settings["words_per_minute"], 150)
        self.assertEqual(settings["characters_per_minute"], 80)
        self.assertEqual(settings["pause_seconds"], .3)
        self.assertEqual(settings["padding_seconds"], .4)
        self.assertEqual(normalize_settings(None)["preferred_seconds"], 10)

    def test_empty_or_huge_text_has_actionable_error(self):
        for source in (None, "", " \n ", "!!!"):
            with self.assertRaises(ValueError):
                split_copy(source)
        with self.assertRaisesRegex(ValueError, "12万"):
            split_copy("a" * 120001)

    def test_cancellation_does_not_return_partial_success(self):
        with self.assertRaises(BackgroundTaskCanceled):
            split_copy("amen " * 1000, canceled=lambda: True)

    def test_format_selected_segments_preserves_text_and_labels(self):
        result = split_copy("amen " * 60)
        selected = format_result(result, [2])
        self.assertTrue(selected.startswith("【第2段｜10秒】"))
        self.assertNotIn("第1段", selected)
        self.assertEqual(format_result(result, [2], False), result["segments"][1]["text"])

    def test_batch_reports_empty_task_but_continues_other_tasks(self):
        reports = []
        context = TaskRunContext(threading.Event(), lambda *values: reports.append(values), lambda _message: None)
        output = run_segmentation({"entries": [{"key": "1", "text": ""}, {"key": "2", "text": "Nevynechaj to!"}],
                                   "settings": {}}, context)
        self.assertIn("error", output["entries"][0])
        self.assertIn("result", output["entries"][1])
        self.assertEqual(reports[-1][1], 100)


class FakeContext:
    def __init__(self, manager):
        self.manager = manager
        self.parent_widget = None
        self.config = {"sentinel": {"keep": True}, "api_configuration": "untouched"}
        self.logs, self.commands, self.submissions, self.targets = [], [], [], []

    def load_config(self):
        return copy.deepcopy(self.config)

    def patch_config(self, updates, expected=None):
        assert expected == self.config
        assert set(updates) == {CONFIG_KEY}
        self.config.update(copy.deepcopy(updates))
        return True

    def submit_background_task(self, **kwargs):
        self.submissions.append(copy.copy(kwargs))
        return self.manager.submit(owner="script_segmenter", **kwargs)

    def log(self, message):
        self.logs.append(message)

    def register_command(self, command):
        self.commands.append(command)

    def selected_task_rows(self):
        return list(range(len(self.targets)))

    def task_targets(self, rows):
        return [self.targets[index] for index in rows]


class SegmentationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.manager = BackgroundTaskManager()
        self.context = FakeContext(self.manager)
        self.dialog = ScriptSegmenterDialog(self.context)

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QtTest.QTest.qWait(5)
        QtTest.QTest.qWait(5)
        self.assertTrue(predicate(), "segmentation background task timed out")

    def tearDown(self):
        self.manager.cancel_all()
        self.wait_until(lambda: not self.manager.is_busy())
        self.dialog.close()
        self.dialog.deleteLater()
        self.manager.shutdown()
        self.manager.deleteLater()
        QtTest.QTest.qWait(10)

    def generate_manual(self, source="amen " * 60):
        self.dialog.input.setPlainText(source)
        self.dialog.generate()
        self.wait_until(lambda: self.dialog.handle is None)
        self.assertIn("result", self.dialog.current_entry())

    def task(self, index, text, directory=""):
        return {"task": SimpleNamespace(task_audio_text=text, task_name=f"任务{index}"),
                "label": f"{index} | 任务{index}", "target_dir": directory}

    def test_menu_and_task_context_registration_lazy_and_reusable(self):
        plugin = ScriptSegmenterPlugin()
        plugin.register(self.context)
        self.assertIsNone(plugin.dialog)
        self.assertEqual({c.command_id: c.locations for c in self.context.commands},
                         {"open": frozenset({MAIN_MENU}), "split_tasks": frozenset({TASK_CONTEXT_MENU}),
                          "copy_segments": frozenset({TASK_CONTEXT_MENU})})
        quick = next(c for c in self.context.commands if c.command_id == 'copy_segments')
        self.assertEqual(quick.title,'复制分段文案')
        self.assertFalse(quick.enabled([]))
        self.assertTrue(quick.enabled([0]))
        self.context.targets = [self.task(1, "Nevynechaj to!")]
        first = plugin.split_tasks([0])
        self.wait_until(lambda: first.handle is None)
        self.assertEqual(first.entries[0]["text"], "Nevynechaj to!")
        first.close()
        self.assertIs(plugin.open_dialog(), first)
        self.assertIn("result", first.entries[0])
        plugin.stop()
        first.deleteLater()

    def test_multiple_tasks_read_audio_text_without_falling_back_to_name(self):
        targets = [self.task(1, "Mária plače, prosím POŠLI JEJ RUŽU. Začínam ja."), self.task(2, "")]
        original = copy.deepcopy(targets)
        self.dialog.load_tasks(targets, generate=True)
        self.wait_until(lambda: self.dialog.handle is None)
        self.assertIn("result", self.dialog.entries[0])
        self.assertIn("error", self.dialog.entries[1])
        self.assertEqual(targets, original)
        self.assertEqual(self.context.submissions[0]["payload"]["entries"][1]["text"], "")
        self.dialog.source_field.setCurrentIndex(1)
        self.assertEqual(self.dialog.entries[1]["text"], "任务2")
        self.assertNotIn("error", self.dialog.entries[1])

    def quick_plugin(self):
        plugin = ScriptSegmenterPlugin()
        plugin.register(self.context)
        self.addCleanup(plugin.stop)
        return plugin

    def wait_quick_copy(self, plugin):
        self.wait_until(lambda: plugin.quick_copy.handle is None)

    def test_quick_copy_uses_saved_settings_without_opening_dialog_or_changing_source(self):
        plugin = self.quick_plugin()
        self.context.config[CONFIG_KEY] = {'durations':[6], 'boundary_strategy':'punctuation_first'}
        self.context.targets = [self.task(316,'amen '*60)]
        original = copy.deepcopy(self.context.targets)
        config = copy.deepcopy(self.context.config)
        command = next(c for c in self.context.commands if c.command_id == 'copy_segments')
        with patch.object(plugin,'open_dialog',side_effect=AssertionError('must remain headless')):
            command.callback([0])
            self.wait_quick_copy(plugin)
        expected = format_result(split_copy('amen '*60,self.context.config[CONFIG_KEY]))
        self.assertEqual(self.app.clipboard().text(),expected)
        self.assertTrue(is_internal_clipboard_content())
        self.assertIsNone(plugin.dialog)
        self.assertEqual(self.context.targets,original)
        self.assertEqual(self.context.config,config)
        self.assertIn('已复制 1个任务',self.context.logs[-1])

    def test_quick_copy_multiple_tasks_keeps_selection_order_and_task_labels(self):
        plugin = self.quick_plugin()
        self.context.targets = [self.task(1,'Nevynechaj to!'),self.task(2,'Boh nás miluje.')]
        plugin.copy_segments([1,0])
        self.wait_quick_copy(plugin)
        text = self.app.clipboard().text()
        self.assertTrue(text.startswith('【任务：2 | 任务2】\n【第1段｜6秒】'))
        self.assertLess(text.index('Boh nás miluje.'),text.index('Nevynechaj to!'))
        self.assertIn('【任务：1 | 任务1】',text)
        self.assertTrue(is_internal_clipboard_content())

    def test_quick_copy_missing_audio_text_never_falls_back_or_publishes_partial_batch(self):
        plugin = self.quick_plugin()
        self.context.targets = [self.task(1,'Amen.'),self.task(2,'')]
        self.app.clipboard().setText('preserve previous clipboard')
        plugin.copy_segments([0,1])
        self.wait_quick_copy(plugin)
        self.assertEqual(self.app.clipboard().text(),'preserve previous clipboard')
        self.assertIn('2 | 任务2',self.context.logs[-1])
        self.assertIn('未复制',self.context.logs[-1])
        self.assertIsNone(plugin.dialog)

    def test_quick_copy_clipboard_write_happens_on_gui_thread(self):
        from app_plugins.builtin.script_segmenter import quick_copy
        plugin = self.quick_plugin()
        self.context.targets = [self.task(1,'Amen.')]
        calls = []
        real_copy = quick_copy.set_internal_clipboard_text
        def record(text):
            calls.append(QtCore.QThread.currentThread())
            real_copy(text)
        with patch.object(quick_copy,'set_internal_clipboard_text',side_effect=record):
            plugin.copy_segments([0])
            self.wait_quick_copy(plugin)
        self.assertEqual(calls,[self.app.thread()])

    def test_quick_copy_empty_selection_and_submit_failure_preserve_clipboard(self):
        plugin = self.quick_plugin()
        self.app.clipboard().setText('unchanged')
        plugin.copy_segments([])
        self.assertEqual(self.context.submissions,[])
        self.context.targets = [self.task(1,'Amen.')]
        with patch.object(self.context,'submit_background_task',side_effect=RuntimeError('queue closed')):
            plugin.copy_segments([0])
        self.assertEqual(self.app.clipboard().text(),'unchanged')
        self.assertIsNone(plugin.quick_copy.handle)
        self.assertIn('queue closed',self.context.logs[-1])
        self.assertIn('Traceback',self.context.logs[-1])

    def test_quick_copy_repeated_click_does_not_duplicate_and_stop_cancels_late_copy(self):
        plugin = self.quick_plugin()
        self.context.targets = [self.task(1,'Amen.')]
        self.app.clipboard().setText('unchanged')
        release = threading.Event()
        submit = self.context.submit_background_task
        def delayed(**kwargs):
            original = kwargs['runner']
            def runner(payload,run):
                release.wait(2)
                return original(payload,run)
            kwargs['runner'] = runner
            return submit(**kwargs)
        with patch.object(self.context,'submit_background_task',side_effect=delayed):
            plugin.copy_segments([0])
            plugin.copy_segments([0])
            self.assertEqual(len(self.context.submissions),1)
            plugin.stop()
            release.set()
            self.wait_quick_copy(plugin)
        plugin.quick_copy.completed({'text':'late outcome','count':1,'warnings':0})
        self.assertEqual(self.app.clipboard().text(),'unchanged')

    def test_quick_copy_oversized_segment_keeps_warning_in_clipboard_and_log(self):
        plugin = self.quick_plugin()
        self.context.targets = [self.task(1,'x'*400)]
        plugin.copy_segments([0])
        self.wait_quick_copy(plugin)
        self.assertIn('需人工处理',self.app.clipboard().text())
        self.assertIn('1段超时需人工核对',self.context.logs[-1])

    def test_quick_copy_short_tail_uses_merged_result_and_keeps_overrun_notice(self):
        plugin = self.quick_plugin()
        source = ' '.join(['amen'] * 14) + '. ' + ' '.join(['amen'] * 22) + '. Amen.'
        self.context.config[CONFIG_KEY] = {'boundary_strategy': 'punctuation_first'}
        self.context.targets = [self.task(1, source)]
        plugin.copy_segments([0])
        self.wait_quick_copy(plugin)
        copied = self.app.clipboard().text()
        self.assertIn('第2段｜超出10秒，需人工处理', copied)
        self.assertNotIn('第3段', copied)
        self.assertTrue(copied.endswith('Amen.'))
        self.assertIn('1段超时需人工核对', self.context.logs[-1])
        self.assertEqual(self.context.targets[0]['task'].task_audio_text, source)

    def test_reads_current_host_selection_and_no_table_write(self):
        self.context.targets = [self.task(1, "Amen."), self.task(2, "Boh nás miluje.")]
        self.dialog.read_selected_tasks()
        self.wait_until(lambda: self.dialog.handle is None)
        self.assertEqual(len(self.dialog.entries), 2)
        self.assertNotIn(CONFIG_KEY, self.context.config)

    def test_editing_text_and_parameters_invalidates_old_results(self):
        self.generate_manual()
        self.dialog.input.insertPlainText("Amen.")
        self.assertNotIn("result", self.dialog.current_entry())
        self.assertEqual(self.dialog.table.rowCount(), 0)
        self.dialog.generate()
        self.wait_until(lambda: self.dialog.handle is None)
        self.dialog.speed.setValue(200)
        self.assertNotIn("result", self.dialog.current_entry())
        self.assertFalse(self.dialog.copy_button.isEnabled())
        self.assertEqual(self.dialog.sources.item(0).text(), "自由文案")

    def test_defaults_saved_scoped_and_errors_logged_without_unhandled_qt_exception(self):
        self.dialog.speed.setValue(175)
        self.dialog.save_settings()
        self.assertEqual(self.context.config[CONFIG_KEY]["words_per_minute"], 175)
        self.assertEqual(self.context.config["sentinel"], {"keep": True})
        with patch.object(self.context, "patch_config", side_effect=OSError("cannot save")):
            self.dialog.save_settings()
        self.assertIn("参数未保存", self.dialog.status.text())
        self.assertIn("Traceback", self.context.logs[-1])

    def test_saved_duration_and_speed_are_loaded(self):
        self.context.config[CONFIG_KEY] = {"durations": [8], "words_per_minute": 170, "padding_seconds": .8}
        other = ScriptSegmenterDialog(self.context)
        self.assertEqual(other.mode.currentData(), [8])
        self.assertEqual(other.speed.value(), 170)
        self.assertEqual(other.padding.value(), .8)
        other.deleteLater()

    def test_strategy_is_separate_from_four_modes_saved_loaded_and_invalidates_result(self):
        self.assertEqual(self.dialog.mode.count(), 4)
        self.assertEqual(self.dialog.strategy.currentData(), 'fewest')
        self.generate_manual()
        self.dialog.strategy.setCurrentIndex(1)
        self.assertNotIn('result', self.dialog.current_entry())
        self.assertEqual(self.dialog.mode.count(), 4)
        self.dialog.save_settings()
        self.assertEqual(self.context.config[CONFIG_KEY]['boundary_strategy'], 'punctuation_first')
        other = ScriptSegmenterDialog(self.context)
        self.assertEqual(other.strategy.currentData(), 'punctuation_first')
        other.deleteLater()
        self.generate_manual()
        self.assertEqual(self.dialog.current_entry()['result']['settings']['boundary_strategy'], 'punctuation_first')

    def test_closing_window_keeps_running_host_job_and_does_not_allow_new_input(self):
        release = threading.Event()
        actual_submit = self.context.submit_background_task

        def submit(**kwargs):
            original = kwargs["runner"]
            def runner(payload, run):
                release.wait(2)
                return original(payload, run)
            kwargs["runner"] = runner
            return actual_submit(**kwargs)

        with patch.object(self.context, "submit_background_task", side_effect=submit):
            self.dialog.show()
            self.dialog.input.setPlainText("Nevynechaj to!")
            self.dialog.generate()
            self.assertFalse(self.dialog.input.isEnabled())
            self.assertFalse(self.context.submissions[0]["retryable"])
            self.dialog.close()
            self.assertTrue(self.manager.is_busy())
            self.dialog.new_manual()
            self.assertEqual(self.dialog.entries[0]["text"], "Nevynechaj to!")
            release.set()
            self.wait_until(lambda: self.dialog.handle is None)
            self.assertIn("result", self.dialog.entries[0])
            self.dialog.show()
            self.assertTrue(self.dialog.input.isEnabled())

    def test_copy_selected_and_all_use_internal_clipboard_and_keep_labels(self):
        self.generate_manual()
        self.dialog.table.selectRow(1)
        self.dialog.copy_selected()
        clipboard = self.app.clipboard()
        self.assertTrue(clipboard.text().startswith("【第2段｜10秒】"))
        self.assertTrue(is_internal_clipboard_content())
        self.dialog.copy_selected(False)
        self.assertEqual(clipboard.text(), self.dialog.current_entry()["result"]["segments"][1]["text"])
        self.dialog.copy_all()
        self.assertIn("第1段", clipboard.text())
        self.assertIn("第3段", clipboard.text())

    def test_ctrl_c_copies_segment_labels(self):
        self.generate_manual()
        self.dialog.show()
        self.dialog.activateWindow()
        self.dialog.table.setFocus()
        self.dialog.table.selectRow(0)
        QtTest.QTest.qWait(30)
        QtTest.QTest.keyClick(self.dialog.table, QtCore.Qt.Key.Key_C, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.app.clipboard().text().startswith("【第1段｜10秒】"))

    def test_save_result_atomic_utf8_in_task_directory_and_save_error_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            self.dialog.load_tasks([self.task(1, "Nevynechaj to!", directory)], generate=True)
            self.wait_until(lambda: self.dialog.handle is None)
            filename = str(Path(directory) / "Flow文案分段.txt")
            with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(filename, "")) as picker:
                self.dialog.save_result()
            self.assertEqual(picker.call_args.args[2], filename)
            data = Path(filename).read_bytes()
            self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
            self.assertIn("Nevynechaj to!", data.decode("utf-8-sig"))
            with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(str(Path(directory) / "absent" / "out.txt"), "")), \
                    patch.object(QtWidgets.QMessageBox, "warning") as warning:
                self.dialog.save_result()
            warning.assert_called_once()
            self.assertIn("Traceback", self.context.logs[-1])

    def test_submit_failure_logs_trace_and_keeps_ui_responsive(self):
        self.dialog.input.setPlainText("Amen.")
        with patch.object(self.context, "submit_background_task", side_effect=RuntimeError("queue closed")):
            self.dialog.generate()
        self.assertIsNone(self.dialog.handle)
        self.assertTrue(self.dialog.input.isEnabled())
        self.assertIn("queue closed", self.dialog.status.text())
        self.assertIn("Traceback", self.context.logs[-1])

    def test_oversize_segment_visible_with_readable_warning(self):
        self.generate_manual("x" * 400)
        self.assertEqual(self.dialog.table.item(0, 1).text(), "超时需处理")
        self.assertIn("超时", self.dialog.table.item(0, 3).toolTip())
        self.assertEqual(self.dialog.table.item(0, 3).foreground().color().name(), "#522800")

    def test_number_timing_visible_and_original_is_not_modified(self):
        source = "2026 " * 24
        self.generate_manual(source)
        result = self.dialog.current_entry()["result"]
        self.assertEqual(result["source"], source)
        self.assertGreater(self.dialog.table.rowCount(), 1)
        self.assertIn("数字按位", self.dialog.table.item(0, 3).toolTip())
        self.assertIn("朗读折算", self.dialog.table.item(0, 3).toolTip())
        self.assertNotIn(CONFIG_KEY, self.context.config)


if __name__ == "__main__":
    unittest.main()
