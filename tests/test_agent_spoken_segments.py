import copy
import csv
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from app_plugins.builtin.ai_agent_assistant.segmentation import split_spoken, parameters, require_available
from app_plugins.builtin.ai_agent_assistant.sheets import row_values, clipboard_tsv, run_copy, FIELDS
from app_plugins.builtin.ai_agent_assistant.store import Store
from model.BackgroundTasks import BackgroundTaskCanceled
from test_ai_agent_assistant import FakeSheets, URL, target
from app_plugins.builtin.ai_agent_assistant.tasks import snapshot


class SpokenSegmentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'queue')
        self.payload = snapshot(target(self.temp.name), 'sk')
        self.payload['spoken'] = ('Pane, ochraňuj nás dnes a veď naše kroky správnou cestou. ' * 12).strip()
        self.store.stage([self.payload]); self.job = self.store.jobs()[0]
        self.mapping = {key: index for index, (key, _title, _aliases) in enumerate(FIELDS)}
        self.run = SimpleNamespace(check_canceled=Mock(), report=Mock(), is_canceled=lambda: False)

    def test_uses_existing_node_10_second_mode_not_audio_or_alignment(self):
        from app_plugins.builtin.script_segmenter import nodes
        original = nodes.build
        with patch.object(nodes, 'build', wraps=original) as build:
            result = split_spoken(self.payload['spoken'], self.run)
        self.assertEqual(build.call_args.args[1]['duration_mode'], '10')
        self.assertEqual(build.call_args.args[0], {'text': self.payload['spoken']})
        self.assertGreater(result['count'], 1)
        self.assertIn('【第1段｜约10秒】', result['text'])

    def test_list_parts_exactly_match_node_and_no_warning_in_spoken_text(self):
        from app_plugins.builtin.script_segmenter.nodes import build
        expected = build({'text': self.payload['spoken']}, parameters(), self.run)
        actual = split_spoken(self.payload['spoken'], self.run)
        self.assertEqual(actual['text'], '\n\n'.join(f'【第{i}段｜约10秒】\n{text}' for i, text in enumerate(expected['segments'], 1)))
        self.assertEqual(actual['warnings'], expected['warnings'])

    def test_api_and_clipboard_same_segmented_cell_one_job_not_split_rows(self):
        row = row_values(self.job, self.mapping, len(FIELDS), self.run)
        rows = list(csv.reader(io.StringIO(clipboard_tsv([self.job], self.run)), delimiter='\t'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(row[self.mapping['spoken']], rows[1][self.mapping['spoken']])
        self.assertIn('【第2段｜约10秒】', row[self.mapping['spoken']])
        self.assertEqual(row[self.mapping['job_id']], self.job['id'])

    def test_original_and_chinese_unchanged_and_no_double_segmentation(self):
        before = copy.deepcopy(self.job)
        first = row_values(self.job, self.mapping, len(FIELDS), self.run)
        second = row_values(self.job, self.mapping, len(FIELDS), self.run)
        self.assertEqual(first, second); self.assertEqual(self.job, before)
        self.assertEqual(first[self.mapping['chinese']], self.payload['chinese'])
        self.assertEqual(self.store.jobs()[0]['payload']['spoken'], self.payload['spoken'])

    def test_warnings_put_in_notes_not_original_body(self):
        with patch('app_plugins.builtin.script_segmenter.nodes.build', return_value={
                'segments': [self.payload['spoken']], 'warnings': '估时提醒-测试'}):
            row = row_values(self.job, self.mapping, len(FIELDS), self.run)
        self.assertIn('估时提醒-测试', row[self.mapping['notes']])
        self.assertNotIn('估时提醒-测试', row[self.mapping['spoken']])

    def test_invalid_node_output_or_missing_words_not_sent_as_whole_script(self):
        for segments in ([], [''], 'not a list', ['丢失了原文']):
            with self.subTest(segments=segments), patch('app_plugins.builtin.script_segmenter.nodes.build', return_value={'segments': segments}):
                with self.assertRaises(ValueError): split_spoken(self.payload['spoken'], self.run)

    def test_cancel_propagates_without_fallback(self):
        self.run.check_canceled.side_effect = BackgroundTaskCanceled()
        with self.assertRaises(BackgroundTaskCanceled): split_spoken(self.payload['spoken'], self.run)

    def test_disabled_provider_blocks_submit_not_silent_raw_fallback(self):
        context = SimpleNamespace(available_nodes=lambda: SimpleNamespace(definitions={}))
        with self.assertRaisesRegex(ValueError, '启用'): require_available(context)

    def test_copy_runner_returns_text_not_touching_clipboard_or_remote_state(self):
        result = run_copy({'root': str(self.store.root.path), 'ids': [self.job['id']]}, self.run)
        self.assertIn('约10秒', result['clipboard'])
        self.assertEqual(self.store.jobs()[0]['state'], 'draft')

    def test_real_export_contains_segmented_foreign_script_and_full_chinese(self):
        from app_plugins.builtin.ai_agent_assistant.sheets import run_export
        api = FakeSheets()
        run_export({'url': URL, 'sheet': '口播表格', 'root': str(self.store.root.path),
                    'ids': [self.job['id']], 'operation': 'submit'}, self.run, service=api)
        values = api.writes[0][0]['body']['values']
        self.assertEqual(len(values), 1)
        self.assertIn('【第2段｜约10秒】', values[0][self.mapping['spoken']])
        self.assertEqual(values[0][self.mapping['chinese']], self.payload['chinese'])

    def test_unreadable_script_does_not_block_other_valid_job(self):
        from app_plugins.builtin.ai_agent_assistant.sheets import run_export
        bad = snapshot(target(self.temp.name, identifier='bad'), 'sk'); bad['spoken'] = '.'
        self.store.stage([bad]); jobs = self.store.jobs()
        api = FakeSheets()
        result = run_export({'url': URL, 'sheet': '口播表格', 'root': str(self.store.root.path),
                    'ids': [job['id'] for job in jobs], 'operation': 'submit'}, self.run, service=api)
        self.assertEqual(result['added'], 1); self.assertEqual(result['invalid'], ['bad'])
