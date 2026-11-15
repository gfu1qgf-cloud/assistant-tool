import copy
import json
import re
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock
from model import DailyQuantitySelection as core
from model import DailyQuantityStats as stats
from qt_compat import QtCore,QtGui,QtWidgets,QtTest
from app_plugins.builtin.task_delivery_daily_quantity import DailyQuantityDialog
from app_plugins.builtin.task_delivery_quantity_selection import DailyQuantitySelectionController
from model.BackgroundTasks import BackgroundTaskManager

DAY='2026-11-02'
CONFIG={'daily_quantity_sheet_url':'synthetic-sheet','task_submission_creator':'本人','daily_quantity_auto_write':False}


class Request:
    def __init__(self,call):self.call=call
    def execute(self):return self.call()


class Sheets:
    def __init__(self):
        self.grids={'统计页':[['','','',DAY,'',''],['组','制作人','类别','12点','18点','24点'],
            ['组','本人','A',1,2,3],['','','B',4,5,6],['其他组','他人','A',99,99,99],
            ['','本人','合计','=SUM(D3:D4)','=SUM(E3:E4)','=SUM(F3:F4)']]}
        self.writes=[];self.reads=[];self.fail='';self.sheet_id=7
        self.format={'validation':'untouched','color':'untouched'}
    def spreadsheets(self):return self
    def values(self):return self
    def get(self,**kwargs):
        return Request(lambda:{'sheets':[{'properties':{'title':name,'sheetId':self.sheet_id,
            'gridProperties':{'rowCount':len(rows),'columnCount':len(rows[1])}}} for name,rows in self.grids.items()]})
    @staticmethod
    def split(target):
        name,address=target.split('!',1);return name[1:-1].replace("''","'"),address
    @staticmethod
    def col(text):
        number=0
        for char in text:number=number*26+ord(char)-64
        return number-1
    def batchGet(self,**kwargs):
        self.reads.append(copy.deepcopy(kwargs))
        def execute():
            result=[]
            for target in kwargs['ranges']:
                name,address=self.split(target);grid=self.grids[name]
                if address.startswith('A1:C'):value=[row[:3] for row in grid]
                elif ':' in address:value=grid[:2]
                else:
                    match=re.fullmatch(r'([A-Z]+)(\d+)',address);r=int(match[2])-1;c=self.col(match[1]);value=[[grid[r][c]]]
                result.append({'range':target,'values':copy.deepcopy(value)})
            return {'valueRanges':result}
        return Request(execute)
    def batchUpdate(self,**kwargs):
        self.writes.append(copy.deepcopy(kwargs))
        def execute():
            if self.fail=='before':raise TimeoutError('synthetic before write')
            for item in kwargs['body']['data']:
                name,address=self.split(item['range']);match=re.fullmatch(r'([A-Z]+)(\d+)',address)
                self.grids[name][int(match[2])-1][self.col(match[1])]=item['values'][0][0]
            if self.fail=='after':raise TimeoutError('synthetic after write')
            return {'totalUpdatedCells':len(kwargs['body']['data'])}
        return Request(execute)


def cell(category='A',slot='01',count=8,**kw):return {'sheet':'统计页','date':DAY,'category':category,'slot':slot,'count':count,**kw}


class SelectedQuantityModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.path=self.root/'state.json';self.api=Sheets()
    def prepare(self,cells):return core.prepare_selection(CONFIG,self.root,cells,service=self.api)
    def commit(self,plan,**kwargs):return core.commit_selection(CONFIG,self.root,plan,service=self.api,state_path=self.path,**kwargs)

    def test_prepare_is_readonly_and_maps_exact_creator_date_and_period(self):
        plan=self.prepare([cell()])
        self.assertEqual(plan['cells'][0]['range'],"'统计页'!D3");self.assertEqual(plan['cells'][0]['before'],1)
        self.assertEqual(self.api.writes,[]);self.assertFalse(self.path.exists())

    def test_single_value_overwrites_not_adds_and_only_values_endpoint_is_used(self):
        before=copy.deepcopy(self.api.grids);style=copy.deepcopy(self.api.format)
        result=self.commit(self.prepare([cell(count=8)]))
        self.assertEqual(len(result['updated']),1);self.assertEqual(self.api.grids['统计页'][2][3],8)
        before['统计页'][2][3]=8;self.assertEqual(self.api.grids,before);self.assertEqual(self.api.format,style)
        self.assertEqual(self.api.writes[0]['body'],{'valueInputOption':'RAW','data':[{'range':"'统计页'!D3",'values':[[8]]}]})
        self.assertFalse(CONFIG['daily_quantity_auto_write'])

    def test_discontiguous_multiple_values_submit_in_one_precise_batch(self):
        result=self.commit(self.prepare([cell(),cell('B','02',9)]))
        self.assertEqual(len(result['verified']),2);self.assertEqual(len(self.api.writes),1)
        self.assertEqual({item['range'] for item in self.api.writes[0]['body']['data']},{"'统计页'!D3","'统计页'!E4"})
        self.assertEqual(self.api.grids['统计页'][4][3:],[99,99,99])

    def test_formula_and_missing_category_are_skipped_without_blocking_good_selection(self):
        self.api.grids['统计页'][3][4]='=1+1'
        plan=self.prepare([cell(),cell('B','02',9),cell('Missing')])
        self.assertEqual(len(plan['cells']),1);self.assertEqual(len(plan['skipped']),2)
        result=self.commit(plan);self.assertEqual(len(result['updated']),1)
        self.assertEqual(len(result['skipped']),2);self.assertEqual(self.api.grids['统计页'][3][4],'=1+1')

    def test_changed_value_after_confirmation_is_not_overwritten_but_other_cell_continues(self):
        plan=self.prepare([cell(),cell('B','02',9)]);self.api.grids['统计页'][2][3]=42
        result=self.commit(plan);self.assertEqual(len(result['updated']),1);self.assertEqual(len(result['skipped']),1)
        self.assertEqual(self.api.grids['统计页'][2][3],42);self.assertEqual(self.api.grids['统计页'][3][4],9)

    def test_changed_sheet_identity_does_not_write_even_if_title_matches(self):
        plan=self.prepare([cell()]);self.api.sheet_id=8
        result=self.commit(plan);self.assertEqual(self.api.writes,[]);self.assertEqual(len(result['skipped']),1)

    def test_changed_creator_or_project_scope_rejected_before_write(self):
        plan=self.prepare([cell()])
        with self.assertRaisesRegex(ValueError,'改变'):
            core.commit_selection(dict(CONFIG,task_submission_creator='他人'),self.root,plan,service=self.api,state_path=self.path)
        self.assertEqual(self.api.writes,[])

    def test_already_equal_value_is_verified_without_api_write(self):
        result=self.commit(self.prepare([cell(count=1)]))
        self.assertEqual(result['unchanged'],1);self.assertEqual(self.api.writes,[])

    def test_zero_is_explicitly_writable(self):
        result=self.commit(self.prepare([cell(count=0)]))
        self.assertEqual(len(result['verified']),1);self.assertEqual(self.api.grids['统计页'][2][3],0)

    def test_uncertain_write_is_read_back_without_duplicate_retry(self):
        plan=self.prepare([cell()]);self.api.fail='after';result=self.commit(plan)
        self.assertEqual(len(self.api.writes),1);self.assertEqual(len(result['verified']),1);self.assertEqual(result['unknown'],[])
        self.assertTrue(result['warnings'])

    def test_failed_before_write_reports_unknown_without_retry_or_success_mark(self):
        plan=self.prepare([cell()]);self.api.fail='before';result=self.commit(plan)
        self.assertEqual(len(self.api.writes),1);self.assertEqual(result['verified'],[]);self.assertEqual(len(result['unknown']),1)
        self.assertFalse(self.path.exists())

    def test_partial_manual_submit_does_not_advance_whole_day_progress_or_auto_ownership(self):
        scope=stats._scope_key(CONFIG,self.root);previous={'cells':{'keep':{'written':20}},'last_manual_refresh':{'latest_date':'2026-10-28'},
            'external_videos':[{'id':'keep'}]}
        self.path.write_text(json.dumps({scope:previous}),encoding='utf8')
        self.commit(self.prepare([cell()]))
        saved=json.loads(self.path.read_text(encoding='utf8'))[scope]
        for key in previous:self.assertEqual(saved[key],previous[key])
        self.assertEqual(len(saved['selected_submissions']),1)

    def test_invalid_selections_and_unavailable_period_are_not_guessed(self):
        for value in ([],[cell(count=True)],[cell(count=-1)],[cell(slot='total')],[cell(date='wrong')]):
            with self.assertRaises(ValueError):self.prepare(value)
        self.api.grids['统计页'][1][3]='合计'
        self.assertEqual(self.prepare([cell()])['cells'],[]);self.assertEqual(self.api.writes,[])

    def test_duplicate_categories_do_not_guess_a_person_row(self):
        self.api.grids['统计页'].insert(3,['','本人','A',0,0,0])
        plan=self.prepare([cell()]);self.assertEqual(plan['cells'],[]);self.assertIn('2 行',plan['skipped'][0]['reason'])

    def test_local_journal_error_does_not_hide_already_verified_cloud_write(self):
        plan=self.prepare([cell()]);self.path.write_text(json.dumps({stats._scope_key(CONFIG,self.root):'corrupt-scope'}),encoding='utf8')
        result=self.commit(plan)
        self.assertEqual(len(result['verified']),1);self.assertTrue(result['warnings'])
        self.assertIn('corrupt-scope',self.path.read_text(encoding='utf8'))

    def test_cancellation_before_commit_never_calls_write(self):
        plan=self.prepare([cell()])
        with self.assertRaisesRegex(RuntimeError,'cancel'):
            self.commit(plan,check=lambda:(_ for _ in ()).throw(RuntimeError('cancel')))
        self.assertEqual(self.api.writes,[])

    def test_header_remapping_after_confirmation_does_not_use_stale_a1(self):
        plan=self.prepare([cell()])
        self.api.grids['统计页'][1][3:5]=['18点','12点']
        result=self.commit(plan)
        self.assertEqual(self.api.writes,[]);self.assertEqual(len(result['skipped']),1)

    def test_two_periods_mapping_same_cloud_cell_are_skipped_without_blocking_other_cell(self):
        self.api.grids['统计页'][1][3]='12点 / 18点';self.api.grids['统计页'][1][4]='其他'
        plan=self.prepare([cell(),cell(slot='02',count=9),cell('B','03',10)])
        self.assertEqual(len(plan['cells']),1);self.assertEqual(len(plan['skipped']),2)
        result=self.commit(plan);self.assertEqual(len(result['updated']),1)
        self.assertEqual(self.api.writes[0]['body']['data'],[{'range':"'统计页'!F4",'values':[[10]]}])

    def test_category_aliases_do_not_submit_conflicting_values_to_same_target(self):
        with self.assertRaisesRegex(ValueError,'不同数值'):self.prepare([cell(category='A'),cell(category=' A ',count=9)])
        self.assertEqual(self.api.writes,[])


class SelectedQuantityUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def dialog(self):
        dialog=DailyQuantityDialog();dialog.folder_day.setDate(QtCore.QDate.fromString(DAY,'yyyy-MM-dd'))
        dialog.show_result({'daily_counts':[{'date':DAY,'sheet':'统计页','category':'A','01':1,'02':2,'03':3,'total':6},
            {'date':DAY,'sheet':'统计页','category':'B','01':4,'02':5,'03':6,'total':15}],
            'counted':21,'warnings':[],'updated':[]})
        return dialog
    def select(self,table,points):
        table.clearSelection()
        for row,col in points:table.item(row,col).setSelected(True)
    def test_single_copy_and_rectangular_copy_with_ctrl_c(self):
        dialog=self.dialog();table=dialog.summary_table
        try:
            self.select(table,[(0,3)])
            with patch('app_plugins.builtin.task_delivery_quantity_selection.set_internal_clipboard_text') as clip:
                QtTest.QTest.keyClick(table,QtCore.Qt.Key.Key_C,QtCore.Qt.KeyboardModifier.ControlModifier)
            clip.assert_called_once_with('2')
            self.select(table,[(0,2),(0,3),(1,2),(1,3)])
            with patch('app_plugins.builtin.task_delivery_quantity_selection.set_internal_clipboard_text') as clip:table.copy_selection()
            clip.assert_called_once_with('1\t2\n4\t5')
        finally:dialog.close();dialog.deleteLater()

    def test_discontiguous_copy_keeps_gaps_and_totals_are_copyable_not_submittable(self):
        dialog=self.dialog();table=dialog.summary_table
        try:
            self.select(table,[(0,2),(1,4)])
            with patch('app_plugins.builtin.task_delivery_quantity_selection.set_internal_clipboard_text') as clip:table.copy_selection()
            clip.assert_called_once_with('1\t\t\n\t\t6')
            self.select(table,[(2,2),(0,5)])
            self.assertEqual(table.selected_quantities()['cells'],[])
            self.assertFalse(dialog.summary_submit_button.isEnabled())
        finally:dialog.close();dialog.deleteLater()

    def test_multiple_quantities_emit_frozen_identity_and_exclude_labels_and_total(self):
        dialog=self.dialog();table=dialog.summary_table;requests=[];dialog.selected_write_requested.connect(requests.append)
        try:
            self.select(table,[(0,2),(1,3),(0,0),(2,2)])
            dialog.summary_submit_button.click()
            self.assertEqual(len(requests),1);self.assertEqual(len(requests[0]['cells']),2)
            self.assertEqual(requests[0]['cells'][0],cell(count=1));self.assertEqual(requests[0]['cells'][1],cell('B','02',5))
            dialog.set_selection_busy(True);self.assertFalse(dialog.summary_submit_button.isEnabled())
            table.request_submission();self.assertEqual(len(requests),1)
        finally:dialog.close();dialog.deleteLater()

    def test_date_or_scope_change_clears_old_cell_metadata(self):
        dialog=self.dialog()
        try:
            dialog.folder_day.setDate(QtCore.QDate.fromString('2026-11-01','yyyy-MM-dd'))
            self.assertEqual(dialog.summary_table.rowCount(),1)
            self.select(dialog.summary_table,[(0,2)]);self.assertEqual(dialog.summary_table.selected_quantities()['cells'],[])
            dialog.clear_summary_scope();self.assertEqual(dialog.summary_table.rowCount(),0)
        finally:dialog.close();dialog.deleteLater()

    def test_controller_full_background_prepare_confirm_write_and_refresh_is_not_automatic(self):
        with tempfile.TemporaryDirectory() as root:
            dialog=self.dialog();api=Sheets();api.grids['统计页'][2][3]=0;api.grids['统计页'][3][4]=0
            manager=BackgroundTaskManager();state=Path(root)/'state.json'
            plugin=SimpleNamespace(context=SimpleNamespace(load_config=lambda:dict(CONFIG),background_tasks=manager,
                parent_widget=SimpleNamespace(task_path_edit=SimpleNamespace(text=lambda:root))),
                daily_quantity_dialog_scope=(dict(CONFIG),root))
            controller=DailyQuantitySelectionController(plugin,dialog)
            original=core.commit_selection
            def commit(config,path,plan,**options):return original(config,path,plan,state_path=state,**options)
            try:
                self.select(dialog.summary_table,[(0,2),(1,3)])
                with patch('model.DailyQuantitySelection.load_sheets_service',return_value=api), \
                     patch('model.DailyQuantitySelection.commit_selection',side_effect=commit), \
                     patch.object(QtWidgets.QMessageBox,'exec',return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                    dialog.summary_submit_button.click()
                    end=time.monotonic()+8
                    while (controller.is_busy() or not controller.receipt) and time.monotonic()<end:
                        self.app.processEvents();time.sleep(.003)
                    self.app.processEvents()
                self.assertFalse(controller.is_busy());self.assertIsNotNone(controller.receipt,dialog.status.text())
                self.assertIn('已核对 2 格',dialog.status.text());self.assertFalse(dialog.auto_write.isChecked())
                self.assertEqual(len(api.writes),1)
                self.assertEqual(api.writes[0]['body']['data'],[{'range':"'统计页'!D3",'values':[[1]]},{'range':"'统计页'!E4",'values':[[5]]}])
                self.assertEqual(api.grids['统计页'][2][4],2)
            finally:
                manager.cancel_all()
                end=time.monotonic()+8
                while manager.is_busy() and time.monotonic()<end:self.app.processEvents();time.sleep(.003)
                manager.shutdown();dialog.close();dialog.deleteLater();manager.deleteLater();self.app.processEvents()

    def test_canceled_prepare_result_never_opens_a_write_confirmation(self):
        dialog=self.dialog()
        try:
            controller=DailyQuantitySelectionController(SimpleNamespace(),dialog)
            controller.handle=SimpleNamespace(manager=SimpleNamespace(get=lambda _:{'status':'canceled'}),task_id='synthetic')
            controller._completed({'action':'prepare','plan':{'cells':[cell()]}})
            self.assertIsNone(controller.pending_plan)
            controller._finished();self.assertFalse(dialog.summary_table.submitting)
        finally:dialog.close();dialog.deleteLater()
