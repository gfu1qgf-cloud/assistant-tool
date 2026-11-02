"""Regression for non-inserting values-only submission into styled blank rows."""
import copy
import csv
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app_plugins.builtin.ai_agent_assistant.sheets import FIELDS, SUBMISSION_FIELDS, row_values, clipboard_tsv, run_export
from app_plugins.builtin.ai_agent_assistant.store import Store
from app_plugins.builtin.ai_agent_assistant.tasks import snapshot
from test_ai_agent_assistant import FakeSheets, target, URL


class TemplateSheets(FakeSheets):
    """Model the documented INSERT_ROWS vs OVERWRITE values API contract."""
    def __init__(self):
        super().__init__()
        self.template = {'format': {'numberFormat': {'type': 'TEXT'}},
                         'validation': {5: ['普通', '优先'], 6: ['待领取', '执行中'], 17: ['未下载', '已下载']}}
        self.row_styles = [copy.deepcopy(self.template) for _ in range(10)]
        self.cell_values = [[None] * len(FIELDS) for _ in range(10)]

    def append(self, **kwargs):
        request = super().append(**kwargs)
        def execute(**options):
            rows = kwargs['body']['values']
            if kwargs.get('insertDataOption') == 'INSERT_ROWS':
                self.row_styles[:0] = [{'format': {'background': 'header-gray'}, 'validation': {}} for _ in rows]
                self.cell_values[:0] = [[None] * len(FIELDS) for _ in rows]
            for index, row in enumerate(rows):
                for col, value in enumerate(row):
                    if value is not None: self.cell_values[index][col] = value
            return request.execute(**options)
        return SimpleNamespace(execute=execute)


class AgentSheetFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'private')
        self.value = snapshot(target(self.temp.name), 'sk')
        self.ids = self.store.stage([self.value])['imported']
        self.run = SimpleNamespace(check_canceled=Mock(), report=Mock(), is_canceled=Mock(return_value=False))

    def export(self, api):
        return run_export({'url': URL, 'sheet': '口播表格', 'root': str(self.store.root.path),
            'ids': self.ids, 'operation': 'submit'}, self.run, service=api)

    def test_append_fills_template_instead_of_inserting_header_styled_row(self):
        api = TemplateSheets(); before = copy.deepcopy(api.row_styles)
        result = self.export(api)
        self.assertEqual(result['added'], 1)
        self.assertEqual(api.row_styles, before)
        self.assertEqual(len(api.cell_values), 10)
        self.assertEqual(api.cell_values[0][0], self.ids[0])
        self.assertEqual(api.row_styles[0]['validation'], api.template['validation'])
        request = api.writes[0][0]
        self.assertEqual(request['insertDataOption'], 'OVERWRITE')
        self.assertEqual(request['valueInputOption'], 'RAW')
        self.assertEqual(set(request['body']), {'values'})

    def test_unknown_and_agent_owned_columns_are_null_not_empty_string(self):
        mapping = {key: index for index, (key, _title, _aliases) in enumerate(FIELDS)}
        job = self.store.jobs(self.ids)[0]
        row = row_values(job, mapping, len(FIELDS) + 1, self.run)
        for key, index in mapping.items():
            if key in SUBMISSION_FIELDS: self.assertIsInstance(row[index], str)
            else: self.assertIsNone(row[index], key)
        self.assertIsNone(row[-1])

    def test_null_cells_do_not_clear_reserved_template_values(self):
        api = TemplateSheets(); agent = next(i for i, field in enumerate(FIELDS) if field[0] == 'agent')
        api.cell_values[0][agent] = 'template-owned'
        self.export(api)
        self.assertEqual(api.cell_values[0][agent], 'template-owned')

    def test_clipboard_nulls_are_empty_cells_not_literal_none(self):
        text = clipboard_tsv(self.store.jobs(self.ids), self.run)
        rows = list(csv.reader(io.StringIO(text), delimiter='\t'))
        agent = next(i for i, field in enumerate(FIELDS) if field[0] == 'agent')
        self.assertEqual(rows[1][agent], '')
        self.assertNotIn('None', rows[1])

    def test_repeated_submit_still_only_writes_once(self):
        api = TemplateSheets(); self.export(api); styles = copy.deepcopy(api.row_styles)
        result = self.export(api)
        self.assertEqual(result['confirmed'], 1); self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.row_styles, styles)


if __name__ == '__main__': unittest.main()
