"""Retired date columns are not write failures; current errors stay visible."""
import copy
from datetime import date
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from model import DailyQuantityStats as stats
from tests.test_daily_quantity_prayer_repair import fake_sheet

OLD = '2026-09-24'
CURRENT = '2026-10-24'


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 27)


def identity(day, slot='03', sheet='统计', category='甲'):
    return sheet, day, slot, category, '本人'


def cell_key(*args, **kwargs):
    return json.dumps(identity(*args, **kwargs), ensure_ascii=False)


def video(name='current', checksum='a' * 32):
    return {'identity': name, 'drive_file_id': name, 'file_name': name + '.mp4',
            'content_md5': checksum, 'content_size': '100'}


def grid(*days):
    rows = [['', '', '', ''], ['组别', '名字', '类别', '定额'], ['视频组', '本人', '甲', 50]]
    for index, day in enumerate(days):
        start = 4 + index * 4
        rows[0].extend([day, '', '', ''])
        rows[1].extend(['一天总数', '12点', '18点', '24点'])
        rows[2].extend(['=SUM()', 0, 0, 0])
    return rows


class RemovedColumnModelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'state.json'
        self.config = {'daily_quantity_sheet_url': 'synthetic-sheet', 'task_submission_creator': '本人'}
        self.scope = stats._scope_key(self.config, self.root)
        self.date_patch = patch.object(stats, 'date', FrozenDate)
        self.date_patch.start()
        self.addCleanup(self.date_patch.stop)
        history = patch.object(stats, 'read_review_history', return_value={'items': {}})
        history.start()
        self.addCleanup(history.stop)

    def save(self, cells, **extra):
        state = {self.scope: {'cells': cells, **extra}, 'other-scope': {'untouched': True}}
        self.path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        return copy.deepcopy(state[self.scope])

    def reconcile(self, sheets, groups=None, **kwargs):
        service = fake_sheet(sheets)
        with patch.object(stats, 'collect_assignments', return_value=(groups or {}, [])):
            result = stats.reconcile_daily_quantity(self.config, self.root, records=[], state_path=self.path,
                service=service, **kwargs)
        return result, service

    def test_deleted_old_columns_do_not_warn_or_prevent_current_write(self):
        old = {'base': 0, 'written': 3, 'videos': [video('old', 'b' * 32)]}
        self.save({cell_key(OLD): old})
        rows = grid(CURRENT)
        result, service = self.reconcile({'统计': rows}, {identity(CURRENT): [video()]})
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['updated'], [{'range': "'统计'!H3", 'count': 1}])
        self.assertEqual(rows[2][3:5], [50, '=SUM()'])
        self.assertEqual([(item['sheet'], item['date'], item['slot']) for item in result['retired_periods']],
                         [('统计', OLD, '03')])
        saved = json.loads(self.path.read_text('utf-8'))
        self.assertEqual(saved[self.scope]['cells'][cell_key(OLD)], old)
        self.assertEqual(saved['other-scope'], {'untouched': True})
        service.spreadsheets.return_value.batchUpdate.assert_not_called()  # no structural/column edits

    def test_removed_periods_are_deduplicated_across_categories(self):
        self.save({cell_key(OLD, category=name): {'written': 2, 'videos': []}
                   for name in ('FL 短口播', 'FL 长口播')})
        result, service = self.reconcile({'统计': grid(CURRENT)})
        self.assertEqual(len(result['retired_periods']), 1)
        self.assertFalse(result['warnings'])
        self.assertFalse(result['updated'])
        service.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()

    def test_retention_is_per_sheet_not_union_of_all_sheet_dates(self):
        self.save({cell_key(OLD, sheet='新表'): {'written': 4, 'videos': []}})
        sheets = {'新表': grid(CURRENT), '保留历史表': grid(OLD, CURRENT)}
        groups = {identity(OLD, sheet='保留历史表'): [video('kept')],
                  identity(CURRENT, sheet='新表'): [video('new', 'b' * 32)]}
        result, _ = self.reconcile(sheets, groups)
        self.assertFalse(result['warnings'])
        self.assertEqual(len(result['updated']), 2)
        self.assertEqual([(item['sheet'], item['date']) for item in result['retired_periods']], [('新表', OLD)])

    def test_deleted_leading_period_with_date_header_remaining_is_retired(self):
        rows = grid(OLD, CURRENT)
        rows[1][5] = ''  # old 12:00 column removed/cleared, later 12:00 still exists
        self.save({cell_key(OLD, '01'): {'written': 2, 'videos': []},
                   cell_key(OLD, '03'): {'written': 0, 'videos': []}})
        result, _ = self.reconcile({'统计': rows})
        self.assertFalse(result['warnings'])
        self.assertEqual([(item['date'], item['slot']) for item in result['retired_periods']], [(OLD, '01')])

    def test_gap_inside_retained_date_range_is_not_silently_ignored(self):
        missing = '2026-10-22'
        self.save({cell_key(missing): {'written': 1, 'videos': []}})
        result, _ = self.reconcile({'统计': grid('2026-10-21', CURRENT)})
        self.assertTrue(any('时段列匹配到 0' in warning for warning in result['warnings']))
        self.assertFalse(result['retired_periods'])

    def test_newer_missing_date_is_still_a_warning(self):
        self.save({cell_key('2026-10-25'): {'written': 1, 'videos': []}})
        result, _ = self.reconcile({'统计': grid(CURRENT)})
        self.assertTrue(result['warnings'])
        self.assertFalse(result['retired_periods'])

    def test_today_missing_period_is_not_retired_even_if_later_dates_exist(self):
        today = '2026-10-27'
        rows = grid(today, '2026-10-28')
        rows[1][7] = ''
        self.save({cell_key(today): {'written': 1, 'videos': []}})
        result, _ = self.reconcile({'统计': rows})
        self.assertTrue(result['warnings'])
        self.assertFalse(result['retired_periods'])

    def test_empty_headers_and_missing_sheet_remain_errors(self):
        self.save({cell_key(OLD): {'written': 1, 'videos': []}})
        for sheets in ({'统计': [['', '', '', ''], ['组别', '名字', '类别', '定额']]},
                       {'另一表': grid(CURRENT)}):
            with self.subTest(sheets=list(sheets)):
                result, _ = self.reconcile(sheets)
                self.assertTrue(result['warnings'])
                self.assertFalse(result['retired_periods'])

    def test_duplicate_old_period_is_never_treated_as_deleted(self):
        rows = grid(OLD, CURRENT)
        rows[1][5:7] = ['12点', '12点']
        self.save({cell_key(OLD, '01'): {'written': 1, 'videos': []}})
        result, _ = self.reconcile({'统计': rows})
        self.assertTrue(any('匹配到 2 列' in warning for warning in result['warnings']))
        self.assertFalse(result['retired_periods'])

    def test_repeated_date_headers_cannot_be_mistaken_for_a_retired_period(self):
        rows = grid(OLD, OLD, CURRENT)
        rows[1][5] = ''  # first duplicate block lacks 12:00, second has it
        self.save({cell_key(OLD, '01'): {'written': 1, 'videos': []}})
        result, _ = self.reconcile({'统计': rows})
        self.assertTrue(result['warnings'])
        self.assertFalse(result['retired_periods'])

    def test_retired_unknown_media_does_not_request_drive_or_block_current_work(self):
        external = {'id': 'old-record', 'drive_file_id': 'unavailable-old', 'file_name': 'old.mp4',
                    'batch_date': OLD, 'batch_slot': '03', 'included': True, 'sheet': '统计', 'category': '甲'}
        self.save({}, external_videos=[external])
        with patch('model.MaterialDriveSync._execute_with_retry', side_effect=AssertionError('old Drive access')):
            result, _ = self.reconcile({'统计': grid(CURRENT)}, {identity(CURRENT): [video()]},
                                       verify_content=True, drive_service=Mock())
        self.assertFalse(result['warnings'])
        self.assertEqual(len(result['updated']), 1)
        self.assertEqual(result['counted'], 1)
        self.assertEqual(len(result['retired_periods']), 1)

    def test_retired_known_fingerprint_still_prevents_duplicate_current_delivery(self):
        self.save({cell_key(OLD): {'written': 1, 'videos': [video('first')]}})
        groups = {identity(OLD): [video('first')], identity(CURRENT): [video('copy')]}
        result, _ = self.reconcile({'统计': grid(CURRENT)}, groups, verify_content=True)
        self.assertEqual(result['counted'], 0)
        self.assertEqual(len(result['duplicate_exclusions']), 1)
        self.assertFalse(result['warnings'])

    def test_current_metadata_failure_still_blocks_writes(self):
        self.save({})
        with patch('model.MaterialDriveSync._execute_with_retry', side_effect=OSError('synthetic network failure')):
            with self.assertRaisesRegex(RuntimeError, '重复内容核验失败'):
                self.reconcile({'统计': grid(CURRENT)},
                    {identity(CURRENT): [{'drive_file_id': 'unknown-current', 'file_name': 'current.mp4'}]},
                    verify_content=True, drive_service=Mock())

    def test_retired_only_refresh_preserves_previous_manual_progress(self):
        marker = {'latest_date': OLD, 'updated_at': '2026-09-24T20:00:00+02:00'}
        self.save({cell_key(OLD): {'written': 1, 'videos': []}}, last_manual_refresh=marker)
        result, _ = self.reconcile({'统计': grid(CURRENT)}, manual_refresh=True)
        self.assertFalse(result['warnings'])
        self.assertEqual(result['sync_progress']['manual_refresh'], marker)

    def test_dry_run_preserves_every_byte_of_local_history(self):
        self.save({cell_key(OLD): {'written': 1, 'videos': []}})
        before = self.path.read_bytes()
        result, service = self.reconcile({'统计': grid(CURRENT)},
            {identity(CURRENT): [video()]}, dry_run=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(result['warnings'])
        self.assertEqual(len(result['retired_periods']), 1)
        service.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()

    def test_restored_date_automatically_rejoins_verification_without_resetting_history(self):
        old = {'base': 0, 'written': 1, 'videos': [video('original')]}
        self.save({cell_key(OLD): old})
        first, _ = self.reconcile({'统计': grid(CURRENT)})
        self.assertEqual(len(first['retired_periods']), 1)
        restored = grid(OLD, CURRENT)
        restored[2][7] = 1
        second, _ = self.reconcile({'统计': restored}, {identity(OLD): [video('original')]})
        self.assertFalse(second['warnings'])
        self.assertFalse(second['retired_periods'])
        self.assertFalse(second['updated'])
        self.assertEqual(json.loads(self.path.read_text('utf-8'))[self.scope]['cells'][cell_key(OLD)], old)


class RemovedColumnUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def result(self):
        return {'warnings': [], 'updated': [], 'counted': 0,
                'retired_periods': [{'sheet': '统计', 'date': OLD, 'slot': '03'}]}

    def test_removed_history_is_informational_not_an_error_or_false_latest_claim(self):
        from qt_compat import QtCore
        from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog
        dialog = DailyQuantityDialog()
        try:
            dialog.show_result(self.result())
            self.assertIn('旧时段', dialog.status.text())
            self.assertNotIn('有待处理问题', dialog.status.text())
            self.assertIn('历史', dialog.details.toPlainText())
            self.assertNotIn('现有表格已是最新', dialog.details.toPlainText())
            self.assertNotIn('待处理', dialog.tabs.tabText(dialog.settings_tab_index))
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_plugin_does_not_notify_or_repeat_write_blocker_logs_for_retired_dates(self):
        from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
        owner = SimpleNamespace(daily_quantity_dialog=Mock(), refresh_daily_quantity_review_queue=Mock(),
                                context=SimpleNamespace(log=Mock(), notify=Mock()))
        TaskDeliveryPlugin._daily_quantity_completed(owner, self.result())
        owner.context.notify.assert_not_called()
        logs = [call.args[0] for call in owner.context.log.call_args_list]
        self.assertTrue(any('旧' in message and '略过' in message for message in logs))
        self.assertFalse(any('未写入原因' in message for message in logs))


if __name__ == '__main__':
    unittest.main()
