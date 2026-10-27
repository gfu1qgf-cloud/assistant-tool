import copy
import os
from pathlib import Path
from unittest.mock import patch
import unittest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.render_selection import identifiers
from app_plugins.builtin.batch_text_video.store import ProfileStore
import test_global_render_queue as fixtures


class RenderCheckboxTests(unittest.TestCase):
    setUp=fixtures.GlobalRenderQueueTests.setUp
    job=fixtures.GlobalRenderQueueTests.job

    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def dialog(self):
        for name in ('request_background','_start_overlay'):
            p=patch.object(BatchTextVideoDialog,name);p.start();self.addCleanup(p.stop)
        dialog=BatchTextVideoDialog(store=self.store)
        def dispose():
            # Production closeEvent hides this reusable plugin window. Tests
            # own it outright: destroy it on Qt's thread before QApplication
            # and Python's native wrappers enter unordered interpreter teardown.
            dialog.close()
            dialog.deleteLater()
            self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
        self.addCleanup(dispose)
        return dialog

    def test_opening_lists_pending_videos_without_starting_render_and_old_buttons_are_removed(self):
        dialog=self.dialog()
        self.assertFalse(hasattr(dialog,'generate'));self.assertFalse(hasattr(dialog,'selected'))
        self.assertEqual(self.store.load()['render_plans'][0]['ids'],['a'])
        self.assertEqual(dialog.jobs.item(0,0).checkState(),QtCore.Qt.CheckState.Checked)
        self.assertFalse(dialog._host_plans)

    def test_checkbox_indicator_is_painted_and_mouse_click_controls_render_intent(self):
        from qt_compat import QtTest
        dialog=self.dialog();dialog.resize(1300,900);dialog.open_global_queue();dialog.show()
        self.app.processEvents()
        rect=dialog.jobs.visualItemRect(dialog.jobs.item(0,0))
        point=QtCore.QPoint(rect.left()+10,rect.center().y())
        checked=dialog.jobs.viewport().grab().toImage()
        QtTest.QTest.mouseClick(dialog.jobs.viewport(),QtCore.Qt.MouseButton.LeftButton,pos=point)
        self.app.processEvents()
        self.assertEqual(dialog.jobs.item(0,0).checkState(),QtCore.Qt.CheckState.Unchecked)
        unchecked=dialog.jobs.viewport().grab().toImage()
        region=QtCore.QRect(rect.left()+2,rect.top()+3,20,rect.height()-6)
        self.assertNotEqual(checked.copy(region),unchecked.copy(region))
        self.assertEqual(self.store.load()['render_plans'],[])

    def test_checkbox_is_persisted_and_independent_of_highlighted_row(self):
        state=self.store.load();state['jobs'].append(self.job('b'));self.store.save(state,state['revision'])
        dialog=self.dialog()
        dialog.jobs.item(1,0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        dialog.jobs.selectRow(1)
        self.assertEqual(self.store.load()['render_plans'][0]['ids'],['a'])
        self.assertEqual(identifiers(self.store.load()),['a'])
        dialog.state=self.store.load();dialog.refresh_tables()
        self.assertEqual(dialog.jobs.item(1,0).checkState(),QtCore.Qt.CheckState.Unchecked)
        dialog.jobs.item(0,0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        self.assertEqual(self.store.load()['render_plans'],[])
        self.assertEqual(len(self.store.load()['jobs']),2)

    def test_newly_added_video_is_checked_and_global_plan_refreshes_without_enqueue_buttons(self):
        dialog=self.dialog()
        dialog.state['jobs'].append(self.job('b'))
        self.assertTrue(dialog.persist());dialog.refresh_tables()
        self.assertEqual(dialog.render_queue_panel.table.rowCount(),1)
        self.assertEqual(self.store.load()['render_plans'][0]['ids'],['a','b'])
        self.assertEqual(dialog.jobs.item(1,0).checkState(),QtCore.Qt.CheckState.Checked)
        self.assertIn('2 条',dialog.render_queue_panel.table.item(0,0).text())

    def test_success_unchecks_the_video_without_changing_its_copy(self):
        dialog=self.dialog();state=self.store.profile_state('default')
        state['jobs'][0]['render_checked']=True
        result=self.store.completion_state(state,'a',self.root/'made.mp4',None)
        self.assertFalse(result['jobs'][0]['render_checked'])
        self.assertEqual(result['jobs'][0]['title'],'One')
        self.assertEqual(result['jobs'][0]['body'],'two')

    def test_submitted_snapshot_cannot_be_changed_or_unchecked(self):
        dialog=self.dialog();plan=self.store.load()['render_plans'][0]
        self.store.update_render_plan(plan['id'],'submitted','worker')
        before=copy.deepcopy(self.store.load())
        self.assertFalse(self.store.sync_render_selection('default',[]))
        with self.assertRaisesRegex(ValueError,'执行中'):self.store.clear_render_selection('default')
        self.assertEqual(self.store.load(),before)

    def test_cancel_other_projects_checks_preserves_current_editor_and_business_data(self):
        state=self.store.create_profile(self.store.load(),'其他项目');other=state['active_profile']
        state['jobs']=[self.job('b')];state=self.store.save(state,state['revision'])
        state=self.store.select_profile(state,'default')
        self.store.sync_render_selection(other,['b'])
        before=self.store.load()
        self.store.clear_render_selection(other)
        after=self.store.load()
        self.assertEqual(after['active_profile'],'default')
        self.assertEqual(after['jobs'],before['jobs']);self.assertEqual(after['settings'],before['settings'])
        saved=ProfileStore(self.store.directory,other).load()['jobs'][0]
        self.assertFalse(saved['render_checked']);self.assertEqual(saved['body'],'two')


if __name__=='__main__':unittest.main()
