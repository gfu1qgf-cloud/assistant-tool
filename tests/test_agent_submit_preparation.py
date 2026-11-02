"""Synthetic legacy drafts only: lossless reference preparation and approval CAS."""
import copy
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app_plugins.builtin.ai_agent_assistant.preparation import prepare_payload
from app_plugins.builtin.ai_agent_assistant.store import Store
from app_plugins.builtin.ai_agent_assistant.tasks import fingerprint, snapshot, validate_payload
from test_ai_agent_assistant import target, FakeSheets, URL

LINK = 'https://drive.google.com/file/d/synthetic/view'


class DraftPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'queue')
        self.payload = snapshot(target(self.temp.name, chinese=None), 'sk')

    def test_reference_description_and_links_preserved_without_mutating_input(self):
        self.payload['reference'] = '温暖自然光；素材：' + LINK + '\n第二个 https://docs.google.com/document/d/test/edit'
        before = copy.deepcopy(self.payload)
        result = prepare_payload(self.payload)
        self.assertEqual(self.payload, before)
        self.assertEqual(result['reference'], LINK + '\nhttps://docs.google.com/document/d/test/edit')
        self.assertIn(before['reference'], result['requirements'])
        self.assertTrue(result['requirements'].startswith(before['requirements']))
        self.assertFalse(result['chinese_confirmed'])
        self.assertEqual(prepare_payload(result), result)

    def test_pure_drive_links_untouched_including_query_strings(self):
        for value in (LINK, LINK + '?usp=sharing', LINK + '\nhttps://docs.google.com/document/d/test/edit'):
            with self.subTest(value=value):
                payload = dict(self.payload, reference=value)
                self.assertEqual(prepare_payload(payload), payload)

    def test_plain_text_goes_to_requirements_without_fabricating_link(self):
        value = prepare_payload(dict(self.payload, reference='自然光、人物正面，保持构图。'))
        self.assertEqual(value['reference'], '')
        self.assertIn('自然光、人物正面，保持构图。', value['requirements'])

    def test_unsupported_urls_and_local_paths_remain_blocked(self):
        for reference in ('C:/private/image.jpg', r'素材 C:\private\image.jpg', r'\\server\image.jpg',
                          'file:///C:/private/image.jpg', '../image.jpg', '/home/private/image.jpg',
                          r'素材：\\server\image.jpg', '素材：/home/private/image.jpg', '素材：../image.jpg',
                          'image.jpg', 'https://example.test/image.jpg',
                          '说明 ' + LINK + ' https://example.test/image.jpg',
                          'https://drive.google.com.evil.test/file/d/private'):
            with self.subTest(reference=reference):
                value = prepare_payload(dict(self.payload, reference=reference), confirm_chinese=True)
                self.assertEqual(value['reference'], reference)
                self.assertEqual(value['requirements'], self.payload['requirements'])
                with self.assertRaises(ValueError): validate_payload(value)

    def test_https_is_not_mistaken_for_windows_drive_path(self):
        value = prepare_payload(dict(self.payload, reference='素材：' + LINK))
        self.assertEqual(value['reference'], LINK)

    def test_chinese_punctuation_does_not_become_part_of_extracted_link(self):
        value = prepare_payload(dict(self.payload, reference='素材（' + LINK + '）。保持明亮。'))
        self.assertEqual(value['reference'], LINK)
        self.assertIn('保持明亮。', value['requirements'])

    def test_duplicate_links_are_deduplicated_but_original_notes_kept(self):
        reference = '第一个 ' + LINK + '\n备用 ' + LINK
        value = prepare_payload(dict(self.payload, reference=reference))
        self.assertEqual(value['reference'], LINK)
        self.assertIn(reference, value['requirements'])

    def test_malformed_url_does_not_crash_preparation(self):
        value = dict(self.payload, reference='https://[broken')
        self.assertEqual(prepare_payload(value), value)

    def test_only_explicit_confirmation_changes_chinese_approval(self):
        self.assertFalse(prepare_payload(self.payload)['chinese_confirmed'])
        value = prepare_payload(self.payload, confirm_chinese=True)
        self.assertTrue(value['chinese_confirmed']); self.assertEqual(value['chinese'], self.payload['chinese'])
        self.assertIn('用户批量确认', value['chinese_source'])
        self.assertFalse(prepare_payload(dict(self.payload, chinese=''), confirm_chinese=True)['chinese_confirmed'])

    def test_already_confirmed_source_not_rewritten(self):
        value = dict(self.payload, chinese_confirmed=True, chinese_source='原表中文列')
        self.assertEqual(prepare_payload(value, confirm_chinese=True), value)

    def test_transaction_keeps_ids_states_and_destination(self):
        payload = dict(self.payload, reference='说明 ' + LINK)
        key = self.store.stage([payload])['imported'][0]
        self.store.prepare_drafts([key], confirm_chinese=True, expected={key: fingerprint(payload)})
        job = self.store.jobs([key])[0]
        self.assertEqual(job['id'], key); self.assertEqual(job['state'], 'draft'); self.assertEqual(job['destination'], '')
        validate_payload(job['payload'])
        self.assertEqual(job['payload']['target_dir'], self.payload['target_dir'])
        self.store.begin([job], 'synthetic:sheet')
        self.assertEqual(self.store.jobs([key])[0]['state'], 'sending')

    def test_concurrent_changes_roll_back_every_draft(self):
        second = snapshot(target(self.temp.name, '002', chinese=None), 'sk')
        ids = self.store.stage([self.payload, second])['imported']
        expected = dict(zip(ids, map(fingerprint, (self.payload, second))))
        self.store.edit(ids[1], 'spoken', 'new content')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            self.store.prepare_drafts(ids, confirm_chinese=True, expected=expected)
        jobs = self.store.jobs(ids)
        self.assertFalse(jobs[0]['payload']['chinese_confirmed'])
        self.assertFalse(jobs[1]['payload']['chinese_confirmed'])
        self.assertEqual(jobs[1]['payload']['spoken'], 'new content')

    def test_history_state_change_prevents_entire_batch(self):
        second = snapshot(target(self.temp.name, '002', chinese=None), 'sk')
        ids = self.store.stage([self.payload, second])['imported']
        self.store.mark([ids[1]], 'uncertain', 'synthetic:sheet')
        with self.assertRaisesRegex(ValueError, '发生变化'):
            self.store.prepare_drafts(ids, confirm_chinese=True,
                expected=dict(zip(ids, map(fingerprint, (self.payload, second)))))
        self.assertFalse(self.store.jobs([ids[0]])[0]['payload']['chinese_confirmed'])
        self.assertEqual(self.store.jobs([ids[1]])[0]['state'], 'uncertain')

    def test_missing_job_prevents_partial_commit(self):
        key = self.store.stage([self.payload])['imported'][0]
        with self.assertRaises(ValueError):
            self.store.prepare_drafts([key, 'nonexistent'], confirm_chinese=True, expected={key: fingerprint(self.payload)})
        self.assertFalse(self.store.jobs([key])[0]['payload']['chinese_confirmed'])

    def test_58_prepared_jobs_reach_single_fake_append_without_losing_notes(self):
        from app_plugins.builtin.ai_agent_assistant.sheets import FIELDS, run_export
        values = [dict(snapshot(target(self.temp.name, str(index), chinese=None), 'sk'),
                       reference='自然光说明 ' + LINK) for index in range(58)]
        ids = self.store.stage(values)['imported']
        self.store.prepare_drafts(ids, confirm_chinese=True, expected=dict(zip(ids, map(fingerprint, values))))
        api = FakeSheets()
        result = run_export({'url': URL, 'sheet': '口播表格', 'root': str(self.store.root.path),
            'ids': ids, 'operation': 'submit'},
            SimpleNamespace(check_canceled=Mock(), report=Mock(), is_canceled=Mock(return_value=False)), service=api)
        self.assertEqual(result['added'], 58); self.assertEqual(len(api.writes), 1)
        rows = api.writes[0][0]['body']['values']
        columns = {key: index for index, (key, _title, _aliases) in enumerate(FIELDS)}
        for index, row in enumerate(rows):
            self.assertEqual(row[columns['job_id']], ids[index])
            self.assertEqual(row[columns['task_id']], str(index))
            self.assertEqual(row[columns['reference']], LINK)
            self.assertIn('自然光说明 ' + LINK, row[columns['requirements']])
            self.assertIn('第1段', row[columns['spoken']])
        self.assertEqual({job['state'] for job in self.store.jobs()}, {'submitted'})


if __name__ == '__main__': unittest.main()
