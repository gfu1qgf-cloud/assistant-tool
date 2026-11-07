"""No real Google requests: explicit write opt-in, scoped cache, source-order copy."""
import copy,json,tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch,Mock
from qt_compat import QtCore,QtWidgets
from model import DailyQuantityStats as stats
from model.DailyQuantityCopy import auto_write_enabled,make_copy_tables
from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityThread,DailyQuantityDialog
from app_plugins.builtin.task_delivery_quantity_copy import DailyQuantityCopyPanel
from tests.test_daily_quantity_progress import fake_sheet


class CopyModelTests(TestCase):
    def test_default_and_legacy_values_do_not_enable_writes(self):
        for config in ({},{'daily_quantity_auto_write':'true'},{'daily_quantity_auto_write':1},
                       {'daily_quantity_auto_write':False}):self.assertFalse(auto_write_enabled(config))
        self.assertTrue(auto_write_enabled({'daily_quantity_auto_write':True}))

    def test_thread_entry_always_passes_explicit_write_policy(self):
        for enabled in (None,False,True):
            config={} if enabled is None else {'daily_quantity_auto_write':enabled}
            with patch('app_plugins.builtin.task_delivery_daily_quantity.reconcile_daily_quantity',return_value={}) as run:
                DailyQuantityThread(config,'synthetic',only_dates=['2026-09-26']).run()
                self.assertIs(run.call_args.kwargs['write_cloud'],enabled is True)

    def test_read_only_refresh_persists_cache_without_claiming_cloud_ownership(self):
        api,grid=fake_sheet();grid[4][6]=999
        config={'daily_quantity_sheet_url':'synthetic','task_submission_creator':'本人'}
        groups={('统计','2026-09-26','02','甲','本人'):[{'identity':'one'},{'identity':'two'}]}
        with tempfile.TemporaryDirectory() as root,patch.object(stats,'collect_assignments',return_value=(groups,[])):
            path=Path(root)/'state.json'
            result=stats.reconcile_daily_quantity(config,root,service=api,records=[],state_path=path,
                only_dates=['2026-09-26'],manual_refresh=True,write_cloud=False)
            api.spreadsheets().values().batchUpdate.assert_not_called();self.assertEqual(grid[4][6],999)
            self.assertEqual(result['write_mode'],'local');self.assertEqual(result['updated'],[])
            self.assertEqual(result['preview_days']['2026-09-26']['tables'][0]['rows'][0]['values'],[0,2,0])
            stored=json.loads(path.read_text('utf-8'))[stats._scope_key(config,root)]
            self.assertNotIn('cells',stored);self.assertNotIn('last_manual_refresh',stored)
            with patch.object(stats,'collect_assignments',side_effect=AssertionError('cache re-counted')):
                cached=stats.cached_daily_quantity_preview(config,root,'2026-09-26',path)
            self.assertFalse(cached['cache_miss']);self.assertEqual(cached['preview_days'],result['preview_days'])
            self.assertTrue(stats.cached_daily_quantity_preview(dict(config,task_submission_creator='别人'),root,'2026-09-26',path)['cache_miss'])
            self.assertTrue(stats.cached_daily_quantity_preview(dict(config,daily_quantity_sheet_url='other'),root,'2026-09-26',path)['cache_miss'])
            self.assertTrue(stats.cached_daily_quantity_preview(config,Path(root)/'other-project','2026-09-26',path)['cache_miss'])

    def test_creator_rows_and_periods_follow_template_not_count_sort(self):
        labels={'统计':[['','','','2026-09-26'],['组','名字','类别','18点','12点','24点'],
                         ['组','本人','乙'],['组','本人','甲'],['组','别人','丙']]}
        counts=[{'date':'2026-09-26','sheet':'统计','category':'甲','01':1,'02':2,'03':3},
                {'date':'2026-09-26','sheet':'统计','category':'乙','01':4,'02':5,'03':6}]
        tables,warnings=make_copy_tables('2026-09-26','本人',counts,labels,labels,
            owner_rows=stats._creator_category_rows,periods=stats._period_columns,normalize=stats._key)
        self.assertFalse(warnings);self.assertEqual(tables[0]['slots'],['02','01','03'])
        self.assertEqual([r['labels'][2] for r in tables[0]['rows']],['乙','甲'])
        self.assertEqual(tables[0]['rows'][0]['values'],[5,4,6]);self.assertEqual(tables[0]['source_columns'],[4,5,6])

    def test_removed_date_is_still_counted_in_local_mode_and_uses_template_order(self):
        api,_=fake_sheet();config={'daily_quantity_sheet_url':'synthetic','task_submission_creator':'本人'}
        groups={('统计','2026-09-25','02','甲','本人'):[{'identity':'one'}]}
        with tempfile.TemporaryDirectory() as root,patch.object(stats,'collect_assignments',return_value=(groups,[])):
            result=stats.reconcile_daily_quantity(config,root,service=api,records=[],state_path=Path(root)/'state.json',
                only_dates=['2026-09-25'],write_cloud=False)
            self.assertEqual(result['counted'],1)
            self.assertEqual(result['preview_days']['2026-09-25']['tables'][0]['template_day'],'2026-09-26')
            api.spreadsheets().values().batchUpdate.assert_not_called()

    def test_cache_only_thread_does_not_call_counter(self):
        with patch('model.DailyQuantityStats.cached_daily_quantity_preview',return_value={'write_mode':'cache'}) as cached,patch(
                'app_plugins.builtin.task_delivery_daily_quantity.reconcile_daily_quantity') as counter:
            DailyQuantityThread({},'synthetic',only_dates=['2026-09-26'],cache_only=True).run()
            cached.assert_called_once();counter.assert_not_called()

    def test_invalid_cache_is_not_presented_or_silently_deleted(self):
        config={'daily_quantity_sheet_url':'synthetic','task_submission_creator':'本人'}
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'state.json'
            path.write_text(json.dumps({stats._scope_key(config,root):{'local_previews':{'本人':{'2026-09-26':{'tables':'broken'}}}}}),encoding='utf-8')
            before=path.read_bytes()
            with self.assertRaisesRegex(ValueError,'缓存格式无效'):stats.cached_daily_quantity_preview(config,root,'2026-09-26',path)
            self.assertEqual(path.read_bytes(),before)

    def test_turning_on_after_local_preview_still_performs_real_numeric_write(self):
        api,grid=fake_sheet();config={'daily_quantity_sheet_url':'synthetic','task_submission_creator':'本人'}
        groups={('统计','2026-09-26','02','甲','本人'):[{'identity':'one'}]}
        with tempfile.TemporaryDirectory() as root,patch.object(stats,'collect_assignments',return_value=(groups,[])):
            path=Path(root)/'state.json'
            stats.reconcile_daily_quantity(config,root,service=api,records=[],state_path=path,
                only_dates=['2026-09-26'],write_cloud=False)
            result=stats.reconcile_daily_quantity(config,root,service=api,records=[],state_path=path,
                only_dates=['2026-09-26'],write_cloud=True)
            api.spreadsheets().values().batchUpdate.assert_called_once()
            self.assertEqual(grid[4][6],1);self.assertEqual(len(result['updated']),1)


class CopyGridTests(TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.panel=DailyQuantityCopyPanel();self.addCleanup(self.panel.deleteLater)
        self.data={'date':'2026-09-26','cached_at':'2026-09-26T12:00:00+02:00','warnings':[],
            'tables':[{'sheet':'统计','day':'2026-09-26','template_day':'2026-09-26','slots':['02','01','03'],
                'source_columns':[4,5,6],'headers':['18点','12点','24点'],'rows':[
                    {'source_row':8,'labels':['组','本人','乙'],'values':[5,4,6],'copyable':True},
                    {'source_row':9,'labels':['','','甲'],'values':[2,1,3],'copyable':True}]}]}
        self.panel.show_result({'preview_days':{'2026-09-26':self.data}});self.panel.set_day('2026-09-26')

    def test_copy_numbers_has_no_header_and_preserves_zero_and_source_order(self):
        with patch('app_plugins.builtin.task_delivery_quantity_copy.set_internal_clipboard_text') as clip:
            self.assertTrue(self.panel.table.copy_selection(numbers=True))
            clip.assert_called_once_with('5\t4\t6\n2\t1\t3')
        self.assertEqual(self.panel.table.editTriggers(),QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

    def test_selected_rectangle_and_unsafe_noncontinuous_source_rows(self):
        self.panel.table.setRangeSelected(QtWidgets.QTableWidgetSelectionRange(0,3,1,4),True)
        with patch('app_plugins.builtin.task_delivery_quantity_copy.set_internal_clipboard_text') as clip:
            self.assertTrue(self.panel.table.copy_selection());clip.assert_called_once_with('5\t4\n2\t1')
        self.panel.table.item(1,3).setData(QtCore.Qt.ItemDataRole.UserRole,11)
        with patch('app_plugins.builtin.task_delivery_quantity_copy.set_internal_clipboard_text') as clip:
            self.assertFalse(self.panel.table.copy_selection(numbers=True));clip.assert_not_called()

    def test_date_switch_never_shows_previous_days_cells_and_reads_memory_cache(self):
        self.panel.set_day('2026-09-25');self.assertEqual(self.panel.table.rowCount(),0)
        self.panel.show_result({'preview_days':{'2026-09-26':self.data}})
        self.assertEqual(self.panel.table.rowCount(),0)
        self.panel.set_day('2026-09-26');self.assertEqual(self.panel.table.rowCount(),2)
        self.panel.clear_scope();self.assertEqual(self.panel.table.rowCount(),0)

    def test_nonadjacent_cloud_columns_cannot_overwrite_formula_columns(self):
        self.panel.table.item(0,4).setData(QtCore.Qt.ItemDataRole.UserRole+2,7)
        with patch('app_plugins.builtin.task_delivery_quantity_copy.set_internal_clipboard_text') as clip:
            self.assertFalse(self.panel.table.copy_selection(numbers=True));clip.assert_not_called()
        self.assertIn('合计公式',self.panel.note.text())

    def test_label_copy_does_not_create_an_executable_spreadsheet_formula(self):
        self.panel.table.item(0,2).setText('=IMPORTDATA("https://example.invalid")')
        self.panel.table.setRangeSelected(QtWidgets.QTableWidgetSelectionRange(0,2,0,2),True)
        with patch('app_plugins.builtin.task_delivery_quantity_copy.set_internal_clipboard_text') as clip:
            self.assertTrue(self.panel.table.copy_selection())
            self.assertIn("'=IMPORTDATA",clip.call_args.args[0])

    def test_dialog_default_write_off_and_date_change_requests_cache_only(self):
        dialog=DailyQuantityDialog();self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.auto_write.isChecked())
        requested=[];dialog.copy_date_requested.connect(requested.append)
        dialog.folder_day.setDate(QtCore.QDate(2026,9,26));self.assertEqual(requested,['2026-09-26'])
