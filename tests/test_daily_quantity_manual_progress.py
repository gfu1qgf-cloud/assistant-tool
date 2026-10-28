import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_daily_quantity_progress import fake_sheet, key
from model.DailyQuantityStats import reconcile_daily_quantity, _scope_key, daily_quantity_sync_progress
from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog, DailyQuantityThread
from qt_compat import QtWidgets


class ManualQuantityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_auto_never_creates_or_advances_manual_marker_even_zero_write(self):
        service, _ = fake_sheet()
        config = {'daily_quantity_sheet_url': 'synthetic-id', 'task_submission_creator': '本人'}
        group = {('统计', '2026-09-26', '02', '甲', '本人'): [{'identity': 'v1'}]}
        with tempfile.TemporaryDirectory() as root, patch('model.DailyQuantityStats.collect_assignments', return_value=(group, [])):
            path = Path(root) / 'state.json'
            auto = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[])
            self.assertEqual(auto['sync_progress']['manual_refresh'], {})
            manual = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[], manual_refresh=True)
            self.assertFalse(manual['updated'])
            marker = copy.deepcopy(manual['sync_progress']['manual_refresh'])
            self.assertEqual(marker['latest_date'], '2026-09-26')
            self.assertTrue(marker['updated_at'])
            scope = _scope_key(config, root)
            state = json.loads(path.read_text(encoding='utf8'))
            state[scope]['cells'][key('2026-09-27')] = {'written': 0, 'videos': []}
            path.write_text(json.dumps(state), encoding='utf8')
            newer = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[])
            self.assertEqual(newer['sync_progress']['manual_refresh'], marker)
            self.assertEqual(daily_quantity_sync_progress(config, root, path)['manual_refresh'], marker)

    def test_dry_run_or_failed_manual_does_not_advance(self):
        service, _ = fake_sheet()
        config = {'daily_quantity_sheet_url': 'synthetic-id', 'task_submission_creator': '本人'}
        group = {('统计', '2026-09-26', '02', '甲', '本人'): [{'identity': 'v1'}]}
        with tempfile.TemporaryDirectory() as root, patch('model.DailyQuantityStats.collect_assignments', return_value=(group, [])):
            path = Path(root) / 'state.json'; path.write_text('{}', encoding='utf8')
            result = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[], manual_refresh=True, dry_run=True)
            self.assertEqual(result['sync_progress']['manual_refresh'], {})
            service.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.side_effect = RuntimeError('synthetic network failure')
            with self.assertRaises(RuntimeError):
                reconcile_daily_quantity(config, root, service=service, state_path=path, records=[], manual_refresh=True)
            self.assertEqual(path.read_text(encoding='utf8'), '{}')

    def test_previously_automatic_date_that_now_fails_verification_is_not_marked_manual(self):
        service, _ = fake_sheet()
        config = {'daily_quantity_sheet_url': 'synthetic-id', 'task_submission_creator': '本人'}
        groups = {('统计', '2026-09-26', '02', '甲', '本人'): [{'identity': 'v1'}]}
        with tempfile.TemporaryDirectory() as root, patch('model.DailyQuantityStats.collect_assignments', return_value=(groups, [])):
            path = Path(root) / 'state.json'; scope = _scope_key(config, root)
            state = {scope: {'cells': {key('2026-09-27'): {'written': 5, 'videos': []}}}}
            path.write_text(json.dumps(state), encoding='utf8')
            result = reconcile_daily_quantity(config, root, service=service, state_path=path, records=[], manual_refresh=True)
            self.assertEqual(result['sync_progress']['latest_date'], '2026-09-27')
            self.assertEqual(result['sync_progress']['manual_refresh']['latest_date'], '2026-09-26')
            self.assertTrue(result['warnings'])

    def test_threads_explicit_origin_and_legacy_ui_does_not_guess_manual(self):
        with patch('app_plugins.builtin.task_delivery_daily_quantity.reconcile_daily_quantity', return_value={}) as reconcile:
            for manual in (False, True):
                worker = DailyQuantityThread({}, 'synthetic-root', manual=manual)
                worker.run()
                self.assertEqual(reconcile.call_args.kwargs['manual_refresh'], manual)
        dialog = DailyQuantityDialog()
        try:
            dialog.show_sync_progress({'latest_date': '2026-09-27', 'updated_at': '2026-09-27T22:00:00+02:00'})
            self.assertIn('尚无手动刷新记录', dialog.sync_progress_heading.text())
            self.assertFalse(dialog.latest_date_button.isEnabled())
            dialog.show_sync_progress({'latest_date': '2026-09-27', 'updated_at': '2026-09-27T22:00:00+02:00',
                'manual_refresh': {'latest_date': '2026-09-26', 'updated_at': '2026-09-26T20:00:00+02:00'}})
            self.assertIn('手动刷新至：2026-09-26', dialog.sync_progress_heading.text())
            from datetime import datetime
            expected = datetime.fromisoformat('2026-09-26T20:00:00+02:00').astimezone().strftime('%Y-%m-%d %H:%M')
            self.assertIn(expected, dialog.sync_progress_heading.text())
            self.assertIn('仍需手动刷新', dialog.sync_progress_note.text())
        finally:
            dialog.close()
