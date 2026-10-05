import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from model.DailyQuantityStats import (
    _category_options, _creator_category_rows, _find_cell, _scope_key,
    repair_future_scan_dates, reconcile_daily_quantity, scan_daily_drive_date,
)


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 3)


def fake_sheet(grids):
    service = MagicMock()
    api = service.spreadsheets.return_value
    api.get.return_value.execute.return_value = {'sheets': [{'properties': {
        'title': name, 'gridProperties': {'rowCount': len(rows), 'columnCount': max(map(len, rows))}}}
        for name, rows in grids.items()]}
    def read(**kwargs):
        result = []
        for address in kwargs['ranges']:
            name = address.split("'", 2)[1]
            result.append({'values': copy.deepcopy(grids[name])})
        request = Mock()
        request.execute.return_value = {'valueRanges': result}
        return request
    def write(**kwargs):
        updates = kwargs['body']['data']
        for update in updates:
            name, address = update['range'].split('!')
            name = name.strip("'")
            letters = ''.join(c for c in address if c.isalpha())
            column = 0
            for c in letters:
                column = column * 26 + ord(c) - ord('A') + 1
            row = int(''.join(c for c in address if c.isdigit())) - 1
            while len(grids[name][row]) < column:
                grids[name][row].append('')
            grids[name][row][column-1] = update['values'][0][0]
        request = Mock()
        request.execute.return_value = {'totalUpdatedCells': len(updates)}
        return request
    api.values.return_value.batchGet.side_effect = read
    api.values.return_value.batchUpdate.side_effect = write
    return service


class PrayerRepairTests(unittest.TestCase):
    def test_every_category_repeats_group_and_owner_and_skips_only_summary(self):
        rows = [[], [], ['', '本人', '全时间'],
                ['视频组', '本人', '第一类'], ['视频组', '本人', '第二类'],
                ['视频组', '本人', '图转动画【AI生图自己跑动画】'],
                ['视频组', '本人', '第四类'], ['视频组', '他人', '不属于本人']]
        self.assertEqual(_creator_category_rows(rows, '本人'), [3, 4, 5, 6])
        self.assertEqual(len(_category_options({'祷告词': rows}, '本人')['祷告词']), 4)

    def test_first_explicit_category_is_not_mistaken_for_header(self):
        self.assertEqual(_creator_category_rows([[], [], ['视频组', '本人', '类别一']], '本人'), [2])

    def test_legacy_blank_owner_continuation_stops_at_unknown_section(self):
        rows = [[], [], ['组', '本人', '15'], ['', '', '本人类别'],
                ['另一区块', '', '不明归属'], ['', '', '不能继承'],
                ['', '他人', '他人类别'], ['', '', '他人续行']]
        self.assertEqual(_creator_category_rows(rows, '本人'), [3])

    def test_exact_target_guard_keeps_duplicate_and_formula_protection(self):
        grid = [['', '', '', '', '2026-10-01', '', '', ''],
                ['组别', '名字', '类别', '定额', '总数', '12点', '18点', '24点'],
                ['', '本人', '全时间'], ['视频组', '本人', '图转动画', 99, '=SUM(F4:H4)', '', '', '']]
        self.assertEqual(_find_cell(grid, '2026-10-01', '02', '本人', '图转动画'), (3, 6))
        grid.append(list(grid[3]))
        with self.assertRaisesRegex(ValueError, '匹配到 2 行'):
            _find_cell(grid, '2026-10-01', '02', '本人', '图转动画')
        grid.pop()
        grid[3][6] = '=1+1'
        with self.assertRaisesRegex(ValueError, '公式'):
            _find_cell(grid, '2026-10-01', '02', '本人', '图转动画')

    def test_future_scan_rejected_before_any_network_or_state_write(self):
        api = MagicMock()
        with patch('model.DailyQuantityStats.date', FrozenDate), tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'state.json'
            with self.assertRaisesRegex(ValueError, '年份'):
                scan_daily_drive_date({}, folder, '2027-10-01', service=api, state_path=path)
            api.files.assert_not_called()
            self.assertFalse(path.exists())

    def test_year_repair_preserves_classification_ids_slots_and_other_scopes(self):
        with tempfile.TemporaryDirectory() as folder, patch('model.DailyQuantityStats.date', FrozenDate):
            config = {'daily_quantity_sheet_url': 'fake-id'}
            scope = _scope_key(config, folder)
            path = Path(folder)/'state.json'
            entry = {'id': 'stable', 'drive_file_id': 'drive', 'batch_date': '2027-10-01',
                     'daily_scan_date': '2027-10-01', 'category': '人工类别', 'batch_slot': '02',
                     'manual_included': True, 'included': True}
            state = {scope: {'daily_scans': {'2027-10-01': {'folder_id': 'same', 'scanned_at': '2026-10-03'}},
                             'external_videos': [entry], 'cells': {'["x","2026-09-25","02","x","本人"]': {'written': 2}},
                             'duration_cache': {'id': {'duration_millis': 12000}}}, 'other': {'do_not_touch': [1, 2]}}
            path.write_text(json.dumps(state), encoding='utf-8')
            correction = {'2027-10-01': '2026-10-01'}
            repair_future_scan_dates(config, folder, correction, path, dry_run=True)
            self.assertEqual(json.loads(path.read_text('utf-8')), state)
            result = repair_future_scan_dates(config, folder, correction, path)
            self.assertEqual(result['changed_records'], 1)
            actual = json.loads(path.read_text('utf-8'))
            self.assertEqual(actual['other'], state['other'])
            expected = {**entry, 'batch_date': '2026-10-01', 'daily_scan_date': '2026-10-01'}
            self.assertEqual(actual[scope]['external_videos'], [expected])
            self.assertEqual(actual[scope]['cells'], state[scope]['cells'])
            self.assertEqual(actual[scope]['duration_cache'], state[scope]['duration_cache'])
            self.assertEqual(repair_future_scan_dates(config, folder, correction, path)['changed_records'], 0)

    def test_year_collision_different_folder_stops_without_writing(self):
        with tempfile.TemporaryDirectory() as folder, patch('model.DailyQuantityStats.date', FrozenDate):
            config = {'daily_quantity_sheet_url': 'fake-id'}
            path = Path(folder)/'state.json'
            state = {_scope_key(config, folder): {'daily_scans': {
                '2027-10-01': {'folder_id': 'A'}, '2026-10-01': {'folder_id': 'B'}}}}
            path.write_text(json.dumps(state), encoding='utf-8')
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, '文件夹不一致'):
                repair_future_scan_dates(config, folder, {'2027-10-01': '2026-10-01'}, path)
            self.assertEqual(path.read_bytes(), before)

    def test_scoped_fill_only_selected_prayer_day_and_repeat_has_no_updates(self):
        grid = [['', '', '', '', '2026-10-01', '', '', '', '2026-10-02', '', '', ''],
                ['组别', '名字', '类别', '定额', '合计', '12点', '18点', '24点', '合计', '12点', '18点', '24点'],
                ['', '本人', '全时间'], ['视频组', '本人', '图转动画', 99, '=SUM(F4:H4)', '', '', '', '=SUM(J4:L4)', '', 8, '']]
        grids = {'祷告词': grid, '其他页': copy.deepcopy(grid)}
        service = fake_sheet(grids)
        config = {'daily_quantity_sheet_url': 'fake-id', 'task_submission_creator': '本人'}
        groups = {('祷告词', '2026-10-01', '02', '图转动画', '本人'): [{'identity': 'v1'}, {'identity': 'v2'}],
                  ('祷告词', '2026-10-02', '02', '图转动画', '本人'): [{'identity': 'other-day'}],
                  ('其他页', '2026-10-01', '02', '图转动画', '本人'): [{'identity': 'other-sheet'}]}
        with tempfile.TemporaryDirectory() as folder, patch('model.DailyQuantityStats.collect_assignments', return_value=(groups, [])), patch('model.DailyQuantityStats.read_review_history', return_value={'items': {}}):
            path = Path(folder)/'state.json'
            result = reconcile_daily_quantity(config, folder, service=service, state_path=path,
                                               only_sheet='祷告词', only_dates=['2026-10-01'])
            self.assertEqual(result['updated'], [{'range': "'祷告词'!G4", 'count': 2}])
            self.assertEqual(grid[3][10], 8)
            self.assertEqual(grids['其他页'][3][6], '')
            second = reconcile_daily_quantity(config, folder, service=service, state_path=path,
                                               only_sheet='祷告词', only_dates=['2026-10-01'])
            self.assertFalse(second['updated'])
            self.assertEqual(grid[3][3:5], [99, '=SUM(F4:H4)'])


if __name__ == '__main__':
    unittest.main()
