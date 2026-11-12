import copy
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.store import Store,fresh_state,ProfileStore
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.queue_selection import apply_checks
from app_plugins.builtin.batch_text_video.row_progress import PROGRESS_ROLE
from model.BackgroundTasks import BackgroundTaskManager


def job(identifier,name=None):return dict(id=identifier,path='',name=name or identifier,title='',body='',status='待生成',output='',error='')

class AtomicSelectionTests(unittest.TestCase):
    def test_multi_project_changes_are_atomic_and_keep_resources_and_active_editor(self):
        state=fresh_state();state['jobs']=[job('same')]
        other=fresh_state();other['jobs']=[job('same')]
        state['profiles'].append(dict(id='other',name='其他项目',revision=0,data={k:other[k] for k in ('settings','jobs','music','next_music_id','backgrounds','copy_pool','image_pool','resource_pools','resource_orders')}))
        before=copy.deepcopy(state)
        changed,yes=apply_checks(state,{'default':{'same':True},'other':{'same':True}},{'default':0,'other':0})
        self.assertTrue(yes);self.assertEqual(state,before);self.assertEqual(changed['active_profile'],'default')
        self.assertEqual({p['profile_id'] for p in changed['render_plans']},{'default','other'})
        self.assertEqual(changed['settings'],state['settings'])
        changed['render_plans'][1]['status']='submitted'
        snapshot=copy.deepcopy(changed)
        with self.assertRaisesRegex(ValueError,'正在渲染'):apply_checks(changed,{'default':{'same':False},'other':{'same':False}})
        self.assertEqual(changed,snapshot)
        with self.assertRaisesRegex(ValueError,'其他窗口'):apply_checks(state,{'other':{'same':False}},{'other':9})
        self.assertEqual(state,before)

class TotalListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=Store(self.root/'private')
        state=self.store.load();state['settings'].update(layers=[],music_enabled=False,output_dir=str(self.root/'out'));state['jobs']=[job('same','第一条')]
        state=self.store.save(state,0);state=self.store.create_profile(state,'项目乙')
        self.other=state['active_profile'];state['settings'].update(layers=[],music_enabled=False,output_dir=str(self.root/'out'));state['jobs']=[job('same','第二条')]
        state=self.store.save(state,state['revision']);self.store.select_profile(state,'default')
        self.manager=BackgroundTaskManager(history_path=self.root/'history.json')
        self.dispatch=patch.object(self.manager,'_dispatch',return_value=None);self.dispatch.start()
        self.bg=patch.object(BatchTextVideoDialog,'request_background');self.bg.start()
        self.overlay=patch.object(BatchTextVideoDialog,'_start_overlay');self.overlay.start()
        self.context=SimpleNamespace(submit_background_task=lambda **kw:self.manager.submit(owner='batch_text_video',**kw),log=lambda *_a,**_kw:None)
        self.dialog=BatchTextVideoDialog(context=self.context,store=self.store);self.dialog.resize(1450,900);self.dialog.show();self.dialog.open_global_queue();self.app.processEvents()
        self.flat=self.dialog.render_queue_panel.flat

    def tearDown(self):
        for handle in list(self.dialog._host_plans.values()):
            self.manager._records[handle.task_id]['status']='queued';self.manager.cancel(handle.task_id)
        self.dialog.close();self.dialog.deleteLater();self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
        self.overlay.stop();self.bg.stop();self.dispatch.stop();self.temp.cleanup()

    def test_all_projects_share_one_default_list_and_browsing_never_switches_editor(self):
        self.assertEqual(self.flat.table.rowCount(),2)
        self.assertTrue(self.flat.isVisible());self.assertFalse(self.dialog.render_queue_panel.global_section.toggle.isChecked())
        self.assertFalse(self.dialog.render_queue_panel.editor_section.toggle.isChecked())
        active=self.dialog.state['active_profile'];before=copy.deepcopy(self.store.load())
        self.flat.table.setCurrentCell(1,0);self.flat.search.setText('项目乙');self.app.processEvents()
        self.assertTrue(self.flat.table.isRowHidden(0));self.assertFalse(self.flat.table.isRowHidden(1))
        self.assertEqual(self.dialog.state['active_profile'],active)
        self.assertEqual(self.store.load(),before)
        self.assertEqual(self.dialog.splitter.sizes()[:2],[0,0])
        self.dialog.show_design();self.app.processEvents();self.assertTrue(all(self.dialog.splitter.sizes()))

    def test_one_click_exports_checked_jobs_from_multiple_projects_without_staging_each(self):
        self.flat.start()
        self.assertEqual(len(self.manager.records()),2)
        self.assertEqual(set(self.dialog._host_plans),{'default',self.other})
        self.assertEqual(self.dialog.state['active_profile'],'default')
        for key,row in self.flat.rows.items():self.assertFalse(self.flat.table.item(row,0).flags()&QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        with patch.object(self.store,'load',side_effect=AssertionError('progress must not read disk')):
            self.dialog._host_item_progress(self.other,'same',37)
            self.dialog._host_item_stage(self.other,'same','编码中')
        one=self.flat.rows[('default','same')];two=self.flat.rows[(self.other,'same')]
        self.assertEqual(self.flat.table.item(two,0).data(PROGRESS_ROLE),37)
        self.assertEqual(self.flat.table.item(two,2).text(),'编码中')
        self.assertNotEqual(self.flat.table.item(one,0).data(PROGRESS_ROLE),37)
        self.dialog._host_item_progress('default','same',100)
        self.dialog._host_item_stage('default','same','等待保存')
        self.assertEqual(self.flat.table.item(one,0).data(PROGRESS_ROLE),99)
        self.assertEqual(self.flat.table.item(one,2).text(),'等待保存')
        self.dialog._host_item_stage('default','same','已完成')
        self.assertEqual(self.flat.table.item(one,0).data(PROGRESS_ROLE),100)

    def test_checkboxes_and_filtered_bulk_edit_target_the_correct_profile(self):
        self.flat.table.item(1,0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        self.app.processEvents()
        self.assertFalse(ProfileStore(self.store.directory,self.other).load()['jobs'][0]['render_checked'])
        self.assertEqual(self.dialog.state['active_profile'],'default')
        self.flat.search.setText('项目乙');self.flat.bulk('unfinished')
        self.assertTrue(ProfileStore(self.store.directory,self.other).load()['jobs'][0]['render_checked'])
        self.assertEqual(self.dialog.state['active_profile'],'default')

    def test_mouse_checkbox_and_empty_search_are_direct_and_do_not_edit_hidden_selections(self):
        from qt_compat import QtTest
        table=self.flat.table;table.setCurrentCell(0,0)
        rect=table.visualItemRect(table.item(0,0))
        QtTest.QTest.mouseClick(table.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=QtCore.QPoint(rect.left()+10,rect.center().y()))
        self.app.processEvents();self.assertFalse(self.store.load()['jobs'][0]['render_checked'])
        self.flat.search.setText('no-such-task');self.app.processEvents()
        self.assertTrue(self.flat.empty.isVisible());self.assertEqual(self.flat.selected_keys(),[])
        self.assertFalse(self.flat.uncheck.isEnabled())
        self.flat.reset_filter.click();self.app.processEvents();self.assertFalse(self.flat.empty.isVisible())

    def test_global_elapsed_uses_wall_clock_and_freezes_without_adding_parallel_times(self):
        self.dialog.render_queue_panel.timing.runs={
            'default':dict(submitted=85.,started=90.,ended=None,items={'a':dict(status='已完成')},result=None),
            self.other:dict(submitted=92.,started=95.,ended=None,items={'b':dict(status='已完成')},result=None)}
        with patch('app_plugins.builtin.batch_text_video.all_tasks_ui.time.monotonic',return_value=100.):
            text=self.flat.batch_clock()
        self.assertIn('00:00:10',text);self.assertIn('平均5.00秒/条',text)
        for run in self.dialog.render_queue_panel.timing.runs.values():run['ended']=100.
        with patch('app_plugins.builtin.batch_text_video.all_tasks_ui.time.monotonic',return_value=150.):
            self.assertEqual(self.flat.batch_clock(),text)

    def test_running_project_does_not_block_selecting_an_idle_project(self):
        self.dialog.start_batch(False);self.dialog.start_staged_plans()
        self.assertIn('default',self.dialog._host_plans);self.assertNotIn(self.other,self.dialog._host_plans)
        self.flat.table.item(self.flat.rows[(self.other,'same')],0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        self.assertFalse(ProfileStore(self.store.directory,self.other).load()['jobs'][0]['render_checked'])
        self.assertEqual(self.manager.records()[0]['status'],'queued')

    def test_unselected_jobs_do_not_appear_queued_and_staged_errors_remain_visible(self):
        state=self.store.load();state['jobs'].append(job('not-selected','不导出的任务'))
        state=self.store.save(state,state['revision']);self.dialog.state=state;self.dialog.refresh_tables()
        self.flat.table.item(self.flat.rows[('default','not-selected')],0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        self.dialog.start_staged_plans()
        self.assertEqual(self.flat.table.item(self.flat.rows[('default','not-selected')],2).text(),'待生成')
        # A failed preflight is visible on the affected row, without opening the project-plan foldout.
        for handle in list(self.dialog._host_plans.values()):
            self.manager._records[handle.task_id]['status']='queued';self.manager.cancel(handle.task_id)
        self.flat.commit({self.other:{'same':True}})
        plan=next(p for p in self.store.load()['render_plans'] if p['profile_id']==self.other and p['status']=='staged')
        self.store.update_render_plan(plan['id'],'staged',error='请启用人声')
        self.dialog.render_queue_panel.refresh()
        row=self.flat.rows[(self.other,'same')]
        self.assertEqual(self.flat.table.item(row,2).text(),'需处理')
        self.flat.table.setCurrentCell(row,0);self.assertIn('请启用人声',self.flat.detail.text())

    def test_explicit_edit_jumps_directly_to_the_correct_project_and_restores_the_canvas(self):
        self.flat.table.setCurrentCell(self.flat.rows[(self.other,'same')],0)
        self.flat.edit_current();self.app.processEvents()
        self.assertEqual(self.dialog.state['active_profile'],self.other)
        self.assertEqual(self.dialog.current_id,'same')
        self.assertEqual(self.dialog.right_tabs.currentIndex(),0)
        self.assertTrue(all(self.dialog.splitter.sizes()))

    def test_prefix_preview_updates_all_projects_without_renaming_saved_jobs(self):
        self.dialog.controls['output_project_prefix'].setChecked(True)
        texts=[self.flat.table.item(r,0).text() for r in range(2)]
        self.assertEqual(texts,['默认配置_第一条.mp4','项目乙_第二条.mp4'])
        self.assertEqual(self.dialog.state['jobs'][0]['name'],'第一条')
        self.assertTrue(self.dialog.save_render_settings())
        self.dialog.switch_profile(self.other)
        self.assertTrue(self.dialog.controls['output_project_prefix'].isChecked())
        if os.environ.get('LZX_TOTAL_LIST_QA'):
            self.dialog.open_global_queue();self.app.processEvents()
            self.assertTrue(self.dialog.grab().save(str(Path(os.environ['LZX_TOTAL_LIST_QA'])/'total-list.png')))

    def test_large_running_list_can_scroll_to_the_end_without_toggling_or_editor_changes(self):
        state=self.store.load();state['jobs']=[job(str(i),'任务'+str(i)) for i in range(700)];state=self.store.save(state,state['revision'])
        self.dialog.state=state;self.dialog.refresh_tables();self.flat.start();self.app.processEvents()
        table=self.flat.table;table.setCurrentCell(0,0);table.setFocus();active=self.dialog.current_id
        self.app.sendEvent(table,QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,QtCore.Qt.Key.Key_End,QtCore.Qt.KeyboardModifier.ControlModifier))
        self.app.processEvents();self.assertEqual(table.currentRow(),table.rowCount()-1);self.assertGreater(table.verticalScrollBar().value(),0)
        item=table.item(table.currentRow(),0);prior=item.checkState()
        self.app.sendEvent(table,QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,QtCore.Qt.Key.Key_Space,QtCore.Qt.KeyboardModifier.NoModifier))
        self.assertEqual(item.checkState(),prior);self.assertEqual(self.dialog.current_id,active)
        self.dialog._host_item_stage('default','699','编码等待：30秒未推进')
        self.assertEqual(table.item(self.flat.rows[('default','699')],2).text(),'编码等待：30秒未推进')
        self.assertFalse(table.findChildren(QtWidgets.QProgressBar))
