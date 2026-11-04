"""Date-scope and review-gate regression tests, using private temp state and fake Sheets."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from model import DailyQuantityStats as stats
from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog, DailyQuantityThread
from tests.test_daily_quantity_progress import fake_sheet, key


class QuantityReviewOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.path = self.root/'state.json'
        self.config = {'daily_quantity_sheet_url': 'synthetic-id', 'task_submission_creator': '本人'}
        self.scope = stats._scope_key(self.config, self.root)
        self.row = {'id': 'local-row', 'drive_file_id': 'video_a', 'file_name': 'a.mp4',
            'batch_date': '2026-09-26', 'batch_slot': '02', 'sheet': '统计', 'category': '甲',
            'included': True, 'review_path': True}
        self.history = {'items': {'google:video_a': {'status': 'pending'}}}
        self.patch = patch('model.DailyQuantityStats.read_review_history', return_value=self.history)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.save([self.row])
    def save(self, rows, **extra):
        self.path.write_text(json.dumps({self.scope: {'external_videos': rows, **extra}}), encoding='utf8')
    def state(self): return json.loads(self.path.read_text(encoding='utf8'))[self.scope]
    def set_review(self, required):
        return stats.set_daily_quantity_review_required(self.config, self.root, ['video_a'], required, self.path)
    def count(self):
        return sum(len(values) for values in stats._external_assignments(self.state(), '本人', {})[0].values())
    def test_no_review_override_is_durable_and_not_a_forged_pass(self):
        before = copy.deepcopy(self.history)
        self.assertEqual(self.count(), 0)
        self.set_review(False)
        self.assertEqual(self.count(), 1)
        record = stats.external_video_records(self.config, self.root, self.path)[0]
        self.assertFalse(record['review_required']); self.assertTrue(record['review_path'])
        self.assertIs(record['review_required_override'], False)
        self.assertEqual(stats.preview_external_day([record], '2026-09-26')['counted'], 1)
        self.assertEqual(self.history, before)
        self.assertEqual(stats.pending_review_quantity_records(self.config, self.root, self.path, records=[]), [])
    def test_restore_automatic_gate_and_explicit_required(self):
        self.set_review(False); self.set_review(None)
        self.assertEqual(self.count(), 0)
        self.assertNotIn('video_a', self.state()['review_overrides'])
        self.save([dict(self.row, review_path=False)])
        with patch('model.DailyQuantityStats.read_review_history', return_value={'items': {}}):
            self.assertEqual(self.count(), 1); self.set_review(True); self.assertEqual(self.count(), 0)
    def test_restore_auto_ignores_old_materialized_override(self):
        self.save([dict(self.row, review_required_override=False)], review_overrides={'video_a': False})
        self.set_review(None)
        row = stats.external_video_records(self.config, self.root, self.path)[0]
        self.assertTrue(row['review_required']); self.assertNotIn('review_required_override', row)
    def test_unrelated_duplicate_name_does_not_override_explicit_no_review(self):
        other = dict(self.row, id='other', drive_file_id='video_b')
        self.save([self.row, other], review_overrides={'video_a': False})
        self.assertEqual(self.count(), 1)
        pending = stats.pending_review_quantity_records(self.config, self.root, self.path, records=[])
        self.assertEqual([row['drive_file_id'] for row in pending], ['video_b'])
    def test_unchecked_ignored_and_missing_classification_still_do_not_count(self):
        for changes in ({'included': False}, {'quantity_ignored': True}, {'category': ''}):
            self.save([dict(self.row, **changes)]); self.set_review(False)
            self.assertEqual(self.count(), 0)
    def test_override_does_not_follow_replacement_drive_id(self):
        self.set_review(False)
        state = self.state(); state['external_videos'][0]['drive_file_id'] = 'replacement'
        self.path.write_text(json.dumps({self.scope: state}), encoding='utf8')
        self.assertEqual(self.count(), 0)
    def test_unknown_selection_is_atomic_and_does_not_rewrite_state(self):
        before = self.path.read_bytes()
        with patch('model.DailyQuantityStats.inspect_daily_quantity', return_value={'records': []}):
            with self.assertRaises(ValueError):
                stats.set_daily_quantity_review_required(self.config, self.root, ['video_a', 'missing'], False, self.path)
        self.assertEqual(self.path.read_bytes(), before)
    def test_same_file_on_multiple_dates_refreshes_every_affected_date(self):
        self.save([self.row, dict(self.row, id='another-copy', batch_date='2026-09-27')])
        result = self.set_review(False)
        self.assertEqual(result['dates'], ['2026-09-26', '2026-09-27'])
    def test_waived_review_video_is_written_to_quantity_sheet(self):
        self.set_review(False); service, grid = fake_sheet()
        result = stats.reconcile_daily_quantity(self.config, self.root, service=service, records=[], state_path=self.path,
            manual_refresh=True, only_dates=['2026-09-26'])
        self.assertEqual(result['counted'], 1); self.assertEqual(grid[4][6], 1)
    def test_upload_history_review_gate_uses_override_too(self):
        project = self.root/'1001'; project.mkdir(); (project/'tasks.ods').touch()
        record = {'source': 'upload', 'logical_key': 'v', 'drive_file_id': 'video_a', 'file_name': 'a.mp4',
            'local_file': str(project/'result'/'a.mp4'), 'batch_date': '2026-09-26', 'batch_slot': '02', 'task': {'id': '7'}}
        task = SimpleNamespace(task_id='7', source_row=2, daily_stat_sheet='统计', daily_stat_category='甲')
        report = {'matched_headers': {'daily_stat_sheet': [1], 'daily_stat_category': [2]}}
        with patch('model.DailyQuantityStats.ReadTaskOds2', return_value=([task], report)), patch('model.DailyQuantityStats.load_task_table_schema', return_value={}):
            groups, _ = stats.collect_assignments({**self.config, 'task_table_file_name': 'tasks.ods'}, self.root, [record], review_overrides={'video_a': False})
        self.assertEqual(sum(map(len, groups.values())), 1)
    def test_rescan_preserves_override_for_same_file(self):
        self.row['folder_id'] = 'syntheticFolder12345'; self.save([self.row]); self.set_review(False)
        api = Mock(); api.files.return_value.get.return_value.execute.return_value = {
            'id': 'syntheticFolder12345', 'name': 'folder', 'mimeType': 'application/vnd.google-apps.folder'}
        with patch('model.MaterialDriveSync._collect_remote_files', return_value=[
                {'id': 'video_a', 'name': 'a.mp4', 'mimeType': 'video/mp4', 'relative_parts': ('a.mp4',)}]):
            result = stats.scan_external_video_folder(self.config, self.root, 'syntheticFolder12345', '2026-09-26', '02',
                service=api, state_path=self.path)
        self.assertFalse(result['records'][0]['review_required'])
        self.assertEqual(self.count(), 1)


class QuantityDateScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def test_selected_oct28_does_not_write_or_mark_nov4_as_manually_checked(self):
        service, grid = fake_sheet()
        grid[0] = ['', '', '', '', '2026-10-28', '', '', '', '2026-11-04', '', '', '']
        grid[1] += ['一天总数', '中午12点', '中午18点', '晚上24点']
        grid[4] += ['=SUM(J5:L5)', '', '', '']
        service.spreadsheets.return_value.get.return_value.execute.return_value['sheets'][0]['properties']['gridProperties']['columnCount'] = 12
        config = {'daily_quantity_sheet_url': 'synthetic-id', 'task_submission_creator': '本人'}
        groups = {('统计', day, '02', '甲', '本人'): [{'identity': day}] for day in ('2026-10-28', '2026-11-04')}
        with tempfile.TemporaryDirectory() as root, patch('model.DailyQuantityStats.collect_assignments', return_value=(groups, [])):
            path = Path(root)/'state.json'; scope = stats._scope_key(config, root)
            path.write_text(json.dumps({scope: {'cells': {key('2026-11-04'): {'written': 0, 'videos': []}}}}), encoding='utf8')
            result = stats.reconcile_daily_quantity(config, root, service=service, records=[], state_path=path,
                manual_refresh=True, only_dates=['2026-10-28'])
            self.assertEqual(result['sync_progress']['manual_refresh']['latest_date'], '2026-10-28')
            self.assertEqual(result['sync_progress']['manual_refresh']['requested_dates'], ['2026-10-28'])
            self.assertEqual(json.loads(path.read_text(encoding='utf8'))[scope]['cells'][key('2026-11-04')]['written'], 0)
            self.assertEqual(grid[4][6], 1); self.assertEqual(grid[4][10], '')
    def test_thread_freezes_requested_date_before_running(self):
        dates = ['2026-10-28']; worker = DailyQuantityThread({}, 'root', manual=True, only_dates=dates)
        dates[0] = '2026-11-04'
        with patch('app_plugins.builtin.task_delivery_daily_quantity.reconcile_daily_quantity', return_value={}) as reconcile:
            worker.run()
        self.assertEqual(reconcile.call_args.kwargs['only_dates'], ('2026-10-28',))
    def test_plugin_manual_refresh_passes_selected_date_to_worker(self):
        from app_plugins.builtin.task_delivery import TaskDeliveryPlugin
        with tempfile.TemporaryDirectory() as root:
            parent = QtWidgets.QWidget(); parent.task_path_edit = QtWidgets.QLineEdit(root, parent)
            plugin = TaskDeliveryPlugin(); plugin.context = SimpleNamespace(parent_widget=parent,
                load_config=lambda: {'daily_quantity_sheet_url': 'synthetic-id'}, log=Mock())
            dialog = DailyQuantityDialog(parent); plugin.daily_quantity_dialog = dialog
            dialog.folder_day.setDate(QtCore.QDate(2026, 10, 28))
            with patch.object(DailyQuantityThread, 'start'):
                self.assertTrue(plugin.refresh_daily_quantity(manual=True))
            self.assertEqual(plugin.daily_quantity_thread.only_dates, ('2026-10-28',))
            parent.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    def test_scoped_heading_separates_delivery_date_from_operation_time(self):
        dialog = DailyQuantityDialog()
        dialog.show_sync_progress({'manual_refresh': {'latest_date': '2026-10-28', 'requested_dates': ['2026-10-28'],
            'updated_at': '2026-11-04T10:00:00+01:00'}})
        text = dialog.sync_progress_heading.text()
        self.assertIn('手动刷新日期：2026-10-28', text); self.assertIn('手动刷新时间：2026-11-04', text)
        dialog.deleteLater()
    def test_review_right_click_requires_confirmation_and_exact_file_identity(self):
        dialog = DailyQuantityDialog(); dialog.show_external_records([
            {'id': 'a', 'drive_file_id': 'video_a', 'file_name': 'a.mp4', 'batch_date': '2026-10-28', 'review_path': True}])
        requests = []; dialog.review_requirement_requested.connect(lambda ids, required: requests.append((ids, required)))
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No):
            dialog._request_review_requirement([0], False)
        self.assertEqual(requests, [])
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            dialog._request_review_requirement([0], False)
        self.assertEqual(requests, [(['video_a'], False)])
        dialog.deleteLater()


if __name__ == '__main__': unittest.main()
