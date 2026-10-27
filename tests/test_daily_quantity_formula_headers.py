"""Calculated date headers must not be mistaken for removed historical columns."""
import copy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from model import DailyQuantityStats as stats
from tests.test_daily_quantity_prayer_repair import fake_sheet


DAY = '2026-10-24'
IDENTITY = ('统计', DAY, '03', '甲', '本人')


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 27)


def formula_grids():
    raw = [['', '', '', ''], ['组别', '名字', '类别', '定额'],
           ['视频组', '=Other!B3', '甲', 50]]
    calculated = copy.deepcopy(raw)
    calculated[2][1] = '本人'
    for index in range(31):
        day = date(2026, 11, 22) - timedelta(days=index)
        serial = (day - date(1899, 12, 30)).days
        previous = stats.column_to_letter(1 + index * 4)
        raw[0].extend([serial if index == 0 else f'={previous}1-1', '', '', ''])
        calculated[0].extend([serial, '', '', ''])
        raw[1].extend(['一天总数', '12点', '18点', '24点'])
        calculated[1].extend(['一天总数', '12点', '18点', '24点'])
        raw[2].extend(['=SUM()', 0, 0, 0])
        calculated[2].extend([0, 0, 0, 0])
    displayed = copy.deepcopy(calculated)
    displayed[0] = [(date(1899, 12, 30) + timedelta(days=value)).strftime('%d.%m.%Y')
                    if isinstance(value, int) else value for value in displayed[0]]
    return raw, calculated, displayed


class FormulaHeaderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'state.json'
        self.config = {'daily_quantity_sheet_url': 'synthetic-sheet', 'task_submission_creator': '本人'}
        self.scope = stats._scope_key(self.config, self.root)
        self.raw, self.calculated, self.displayed = formula_grids()
        self.column = stats._period_columns(self.calculated)[(DAY, '03')][0]
        self.video = {'drive_file_id': 'synthetic', 'file_name': 'video.mp4',
                      'identity': 'synthetic', 'content_md5': 'a' * 32, 'content_size': '100'}
        self.service = fake_sheet({'统计': self.raw})
        self.reads = []

        def read(**kwargs):
            self.reads.append(kwargs)
            mode = kwargs['valueRenderOption']
            grids = {'FORMULA': self.raw, 'FORMATTED_VALUE': self.displayed,
                     'UNFORMATTED_VALUE': self.calculated}
            rows = copy.deepcopy(grids[mode])
            if mode == 'FORMATTED_VALUE':
                rows = [row[:3] for row in rows]
            elif mode == 'UNFORMATTED_VALUE':
                rows = rows[:2]
            response = Mock()
            response.execute.return_value = {'valueRanges': [{'values': rows}]}
            return response

        self.service.spreadsheets.return_value.values.return_value.batchGet.side_effect = read
        for context in (patch.object(stats, 'date', FrozenDate),
                        patch.object(stats, 'read_review_history', return_value={'items': {}})):
            context.start()
            self.addCleanup(context.stop)

    def reconcile(self, groups=None, **kwargs):
        with patch.object(stats, 'collect_assignments',
                          return_value=({IDENTITY: [self.video]} if groups is None else groups, [])) as collect:
            result = stats.reconcile_daily_quantity(self.config, self.root, records=[],
                state_path=self.path, service=self.service, **kwargs)
        self.collect = collect
        return result

    def save(self, cells):
        self.path.write_text(json.dumps({self.scope: {'cells': cells}}, ensure_ascii=False), encoding='utf-8')

    def test_formula_date_writes_exact_current_cell_and_preserves_formulas(self):
        formulas = [list(self.raw[0]), self.raw[2][4]]
        result = self.reconcile()
        self.assertEqual(result['counted'], 1)
        self.assertEqual(result['updated'], [{'range': "'统计'!DT3", 'count': 1}])
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['retired_periods'], [])
        self.assertEqual([self.raw[0], self.raw[2][4]], formulas)
        self.assertEqual(self.raw[2][self.column], 1)
        self.assertIn(DAY, self.collect.call_args.kwargs['allowed_dates'])

    def test_calculated_read_is_narrow_and_locale_independent(self):
        self.reconcile()
        read = next(item for item in self.reads if item['valueRenderOption'] == 'UNFORMATTED_VALUE')
        self.assertEqual(read['ranges'], ["'统计'!A1:DX2"])
        self.assertEqual(read['dateTimeRenderOption'], 'SERIAL_NUMBER')
        self.assertEqual(len(self.collect.call_args.kwargs['allowed_dates']), 31)

    def test_old_removed_dates_still_skip_without_hiding_current_formula_dates(self):
        old_key = json.dumps(('统计', '2026-09-24', '03', '甲', '本人'), ensure_ascii=False)
        self.save({old_key: {'written': 2, 'videos': []}})
        result = self.reconcile()
        self.assertEqual(result['counted'], 1)
        self.assertEqual([item['date'] for item in result['retired_periods']], ['2026-09-24'])
        self.assertFalse(result['warnings'])

    def test_calculated_period_label_formulas_are_supported(self):
        self.raw[1][self.column] = '="24点"'
        result = self.reconcile()
        self.assertEqual(len(result['updated']), 1)
        self.assertFalse(result['warnings'])

    def test_numeric_target_formula_is_never_overwritten(self):
        self.raw[2][self.column] = '=1-1'
        self.calculated[2][self.column] = 0
        result = self.reconcile()
        self.assertEqual(result['updated'], [])
        self.assertTrue(any('目标格有公式' in warning for warning in result['warnings']))
        self.assertEqual(self.raw[2][self.column], '=1-1')
        self.service.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()

    def test_untracked_hand_entered_number_is_not_overwritten(self):
        self.raw[2][self.column] = 9
        result = self.reconcile()
        self.assertFalse(result['updated'])
        self.assertTrue(any('手填数字 9' in warning for warning in result['warnings']))

    def test_external_changes_remain_protected(self):
        self.save({json.dumps(IDENTITY, ensure_ascii=False): {'base': 0, 'written': 2, 'videos': []}})
        self.raw[2][self.column] = 3
        result = self.reconcile()
        self.assertFalse(result['updated'])
        self.assertTrue(any('外部修改' in warning for warning in result['warnings']))

    def test_existing_matching_number_is_adopted_without_double_counting(self):
        self.raw[2][self.column] = 1
        result = self.reconcile()
        self.assertFalse(result['updated'])
        self.assertFalse(result['warnings'])
        self.assertEqual(result['counted'], 1)

    def test_incomplete_header_response_fails_before_writes_or_history_changes(self):
        self.save({})
        before = self.path.read_bytes()
        original = self.service.spreadsheets.return_value.values.return_value.batchGet.side_effect
        def incomplete(**kwargs):
            if kwargs['valueRenderOption'] == 'UNFORMATTED_VALUE':
                response = Mock()
                response.execute.return_value = {'valueRanges': []}
                return response
            return original(**kwargs)
        self.service.spreadsheets.return_value.values.return_value.batchGet.side_effect = incomplete
        with self.assertRaisesRegex(RuntimeError, '表头读取不完整'):
            self.reconcile()
        self.assertEqual(self.path.read_bytes(), before)
        self.service.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()

    def test_dry_run_returns_current_write_plan_without_modifying_any_state(self):
        self.save({})
        before = self.path.read_bytes()
        result = self.reconcile(dry_run=True)
        self.assertEqual(len(result['updated']), 1)
        self.assertEqual(result['counted'], 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.raw[2][self.column], 0)
        self.service.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()


class InspectionLogTests(unittest.TestCase):
    def test_read_only_inventory_log_points_to_separate_write_result(self):
        from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
        dialog = Mock()
        dialog._pending_view = None
        dialog.folder_day.date.return_value.toString.return_value = DAY
        owner = SimpleNamespace(daily_quantity_dialog=dialog,
                                daily_quantity_inspection_thread=None,
                                context=SimpleNamespace(log=Mock()))
        result = {'checked': 3, 'pending': [], 'ignored': [], 'records': []}
        TaskDeliveryPlugin._daily_quantity_inspection_completed(owner, result)
        message = owner.context.log.call_args.args[0]
        self.assertIn('清单检索（只读）', message)
        self.assertIn('每日数量统计', message)
        self.assertNotIn('未修改云端数字', message)
        dialog.show_inspection.assert_called_once_with(result)


if __name__ == '__main__':
    unittest.main()
