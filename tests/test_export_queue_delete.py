import copy
from pathlib import Path
import unittest
from unittest.mock import patch
from qt_compat import QtCore, QtWidgets, QtTest
from app_plugins.builtin.batch_text_video.queue_selection import remove_jobs
from app_plugins.builtin.batch_text_video.store import fresh_state, ProfileStore
from test_export_total_list import TotalListTests, job


class AtomicDeleteTests(unittest.TestCase):
    def state(self):
        state=fresh_state();state['jobs']=[job('a'),job('keep')]
        other=fresh_state();other['jobs']=[job('a'),job('keep')]
        state['profiles'].append(dict(id='other',name='其他项目',revision=2,data=other))
        for pid in ('default','other'):
            state['render_plans'].append(dict(id=pid,profile_id=pid,status='staged',ids=['a','keep'],known_ids=['a','keep']))
        return state

    def test_cross_project_delete_keeps_other_jobs_resources_and_plan_membership(self):
        state=self.state();before=copy.deepcopy(state)
        out,changed=remove_jobs(state,{'default':['a'],'other':['a']},{'default':0,'other':2})
        self.assertTrue(changed);self.assertEqual(state,before)
        self.assertEqual([j['id'] for j in out['jobs']],['keep'])
        self.assertEqual([j['id'] for j in out['profiles'][1]['data']['jobs']],['keep'])
        self.assertEqual(out['active_profile'],state['active_profile'])
        for key in ('settings','backgrounds','copy_pool','resource_pools','music','image_pool'):
            self.assertEqual(out[key],state[key])
        for p in out['render_plans']:self.assertEqual(p['ids'],['keep']);self.assertEqual(p['known_ids'],['keep'])
        self.assertEqual(out['revision'],state['revision']+1)

    def test_running_stale_or_missing_records_refuse_entire_transaction(self):
        for mode in ('running','stale','missing'):
            with self.subTest(mode=mode):
                state=self.state();expected={'default':0,'other':2};selection={'default':['a'],'other':['a']}
                if mode=='running':state['render_plans'][1]['status']='running'
                if mode=='stale':expected['other']=1
                if mode=='missing':selection['other']=['gone']
                before=copy.deepcopy(state)
                with self.assertRaises(ValueError):remove_jobs(state,selection,expected)
                self.assertEqual(state,before)

    def test_empty_plans_removed_and_empty_selection_noop(self):
        state=self.state()
        out,changed=remove_jobs(state,{'other':['a','keep']})
        self.assertTrue(changed);self.assertEqual(len(out['render_plans']),1)
        self.assertEqual(out['profiles'][1]['data']['jobs'],[])
        same,changed=remove_jobs(state,{})
        self.assertFalse(changed);self.assertEqual(same,state)


class DeleteUiTests(TotalListTests):
    # Reuse the isolated dialog fixture, without rerunning its inherited cases.
    def test_button_cancel_then_delete_multiple_projects_keeps_media_and_editor_safe(self):
        source=self.root/'source.mp4';output=self.root/'saved.mp4'
        source.write_bytes(b'original');output.write_bytes(b'finished')
        state=self.store.load();state['jobs'][0].update(path=str(source),output=str(output))
        self.dialog.state=self.store.save(state,state['revision']);self.dialog.refresh_tables()
        self.dialog.jobs.setCurrentCell(0,0);self.app.processEvents()
        self.flat.table.selectAll();self.app.processEvents()
        self.assertTrue(self.flat.delete.isVisible());self.assertTrue(self.flat.delete.isEnabled())
        before=copy.deepcopy(self.store.load())
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.No):self.flat.delete.click()
        self.assertEqual(self.store.load(),before)
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes):self.flat.delete.click()
        self.assertEqual(self.flat.table.rowCount(),0)
        self.assertEqual(self.store.load()['jobs'],[])
        self.assertEqual(ProfileStore(self.store.directory,self.other).load()['jobs'],[])
        self.assertEqual(self.dialog.current_id,'');self.assertFalse(self.dialog.body.isEnabled())
        self.assertTrue(self.dialog.persist());self.assertEqual(self.store.load()['jobs'],[])
        self.assertEqual(source.read_bytes(),b'original');self.assertEqual(output.read_bytes(),b'finished')
        self.assertEqual(self.dialog.state['active_profile'],'default')

    def test_delete_key_only_deletes_visible_selection_and_cleans_pending_plan(self):
        self.flat.commit({'default':{'same':True},self.other:{'same':True}})
        self.flat.table.selectAll();self.flat.search.setText('项目乙');self.app.processEvents()
        self.flat.table.setFocus()
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes) as confirm:
            QtTest.QTest.keyClick(self.flat.table,QtCore.Qt.Key.Key_Delete);self.app.processEvents()
            confirm.assert_called_once()
        state=self.store.load()
        self.assertEqual(len(state['jobs']),1)
        self.assertEqual(ProfileStore(self.store.directory,self.other).load()['jobs'],[])
        self.assertEqual([p['profile_id'] for p in state['render_plans']],['default'])

    def test_running_selection_disabled_and_direct_call_does_not_delete(self):
        self.flat.start();self.flat.table.selectAll();self.app.processEvents()
        self.assertFalse(self.flat.delete.isEnabled());self.assertFalse(self.flat.delete_action.isEnabled())
        before=copy.deepcopy(self.store.load())
        with patch.object(QtWidgets.QMessageBox,'warning') as warning:self.flat.delete_selected()
        warning.assert_called_once();self.assertEqual(self.store.load(),before)

    def test_stale_other_project_refuses_atomic_delete_with_visible_error(self):
        self.flat.table.selectAll();self.app.processEvents()
        other=ProfileStore(self.store.directory,self.other);state=other.load()
        state['jobs'][0]['title']='其他窗口的新内容';other.save(state,state['revision'])
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes),patch.object(QtWidgets.QMessageBox,'warning') as warning:
            self.flat.delete.click()
        warning.assert_called_once()
        self.assertEqual(len(self.store.load()['jobs']),1)
        self.assertEqual(other.load()['jobs'][0]['title'],'其他窗口的新内容')

    def test_refresh_does_not_transfer_selection_to_a_different_task(self):
        self.flat.table.setCurrentCell(0,0);self.app.processEvents()
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes):self.flat.delete.click()
        self.assertEqual(self.flat.table.rowCount(),1)
        self.assertEqual(self.flat.selected_keys(),[])
        self.assertFalse(self.flat.delete.isEnabled())

    def test_idle_project_can_delete_while_other_project_renders(self):
        self.flat.commit({'default':{'same':True},self.other:{'same':False}});self.flat.start()
        self.flat.table.setCurrentCell(self.flat.rows[(self.other,'same')],0);self.app.processEvents()
        self.assertTrue(self.flat.delete.isEnabled())
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.Yes):self.flat.delete.click()
        self.assertEqual(ProfileStore(self.store.directory,self.other).load()['jobs'],[])
        self.assertEqual(len(self.store.load()['jobs']),1)
        self.assertIn('default',self.dialog._host_plans)

    def test_context_menu_has_enabled_delete_and_compact_controls_fit(self):
        self.flat.table.setCurrentCell(0,0);self.app.processEvents()
        with patch.object(QtWidgets.QMenu,'exec') as show:
            self.flat.menu(self.flat.table.visualItemRect(self.flat.table.item(0,0)).center())
            show.assert_called_once()
        menus=self.flat.findChildren(QtWidgets.QMenu)
        self.assertTrue(any(a.text()=='删除所选' and a.isEnabled() for m in menus for a in m.actions()))
        self.assertTrue(all(self.dialog.splitter.sizes()))
        for button in (self.flat.delete,self.flat.uncheck,self.flat.panel.start_button):
            self.assertTrue(button.isVisible());self.assertTrue(self.flat.rect().contains(button.geometry().bottomRight()))


# unittest also discovers inherited cases and imported test classes. Keep only
# this file's new cases; the fixture module is run separately in its own process.
def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite(loader.loadTestsFromTestCase(AtomicDeleteTests))
    for name in DeleteUiTests.__dict__:
        if name.startswith('test_'):suite.addTest(DeleteUiTests(name))
    return suite
