import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.store import Store, ProfileStore
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class ProjectDeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'source.mp4'
        self.source.write_bytes(b'original media')
        self.output = self.root/'finished.mp4'
        self.output.write_bytes(b'finished media')
        self.store = Store(self.root/'project')
        state = self.store.load()
        state['jobs'] = [dict(id='job', path=str(self.source), output=str(self.output),
            title='One', body='two', name='job', status='待生成', error='')]
        self.state = self.store.save(state, 0)

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            widget.close(); widget.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def revision(self, identifier):
        return next(p.get('revision', 0) for p in self.store.load()['profiles'] if p['id'] == identifier)

    def test_delete_current_removes_only_its_records_and_staged_plans(self):
        state = self.store.create_profile(self.state, 'Second')
        second = state['active_profile']
        state['jobs'] = [dict(self.state['jobs'][0], id='second')]
        state = self.store.save(state, state['revision'])
        state = self.store.select_profile(state, 'default')
        self.store.add_render_plan('default', ['job'])
        other = self.store.add_render_plan(second, ['second'])
        before = self.store.profile_state(second)
        result = self.store.delete_profile('default', expected_profile_revision=self.revision('default'))
        self.assertEqual(result['active_profile'], second)
        self.assertEqual([p['id'] for p in result['profiles']], [second])
        self.assertEqual(result['render_plans'], [other])
        self.assertEqual(result['jobs'], before['jobs'])
        self.assertEqual(self.source.read_bytes(), b'original media')
        self.assertEqual(self.output.read_bytes(), b'finished media')
        with self.assertRaises(ValueError):
            self.store.save(self.state, self.state['revision'])

    def test_last_project_is_replaced_with_empty_project_and_new_identity(self):
        result = self.store.delete_profile('default', expected_profile_revision=self.revision('default'))
        self.assertNotEqual(result['active_profile'], 'default')
        self.assertEqual(len(result['profiles']), 1)
        for key in ('jobs', 'music', 'backgrounds', 'copy_pool', 'image_pool', 'resource_pools', 'render_plans'):
            self.assertEqual(result[key], [], key)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.output.exists())

    def test_submitted_project_and_stale_revision_cannot_be_deleted(self):
        plan = self.store.add_render_plan('default', ['job'])
        self.store.update_render_plan(plan['id'], 'submitted', 'running-task')
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '先在后台任务'):
            self.store.delete_profile('default', expected_profile_revision=self.revision('default'))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.update_render_plan(plan['id'], 'cancelled')
        with self.assertRaisesRegex(ValueError, '其他窗口'):
            self.store.delete_profile('default', expected_profile_revision=-1)

    def test_other_project_render_and_global_settings_are_untouched(self):
        state = self.store.create_profile(self.state, 'Second')
        other = state['active_profile']
        state['jobs'] = [dict(self.state['jobs'][0], id='second')]
        state = self.store.save(state, state['revision'])
        other_revision = self.revision(other)
        plan = self.store.add_render_plan(other, ['second'])
        self.store.update_render_plan(plan['id'], 'submitted', 'other-worker')
        values = dict(state['render_settings'], export_workers=4)
        self.store.save_render_settings(values)
        result = self.store.delete_profile('default', expected_profile_revision=self.revision('default'))
        self.assertEqual(result['active_profile'], other)
        self.assertEqual(result['render_settings'], values)
        self.assertEqual(self.revision(other), other_revision)
        self.assertEqual(result['render_plans'][0]['task_id'], 'other-worker')

    def test_worker_cannot_commit_after_its_project_was_deleted(self):
        worker = ProfileStore(self.store.directory, 'default')
        old = worker.load()
        self.store.delete_profile('default', expected_profile_revision=self.revision('default'))
        with self.assertRaisesRegex(ValueError, '停止该项目导出'):
            worker.save(old, old['revision'])

    def test_terminal_host_evidence_allows_cleanup_of_unfinished_plan_marker(self):
        plan = self.store.add_render_plan('default', ['job'])
        self.store.update_render_plan(plan['id'], 'submitted', 'finished-task')
        with patch.object(BatchTextVideoDialog, 'update_preview'), \
             patch.object(BatchTextVideoDialog, 'request_background'):
            context = SimpleNamespace(background_tasks=SimpleNamespace(completion_states=lambda: {'finished-task':'interrupted'}))
            dialog = BatchTextVideoDialog(store=self.store, context=context)
            dialog.rebuild_profile_menu()
            action = next(a for a in dialog.profile_menu.actions() if a.text() == '删除当前项目…')
            self.assertTrue(action.isEnabled())
            with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                dialog.delete_current_profile()
            self.assertEqual(dialog.state['render_plans'], [])
            self.assertNotEqual(dialog.state['active_profile'], 'default')
            self.assertTrue(self.output.exists())

    def test_menu_confirmation_cancellation_and_delete_refresh(self):
        with patch.object(BatchTextVideoDialog, 'update_preview'), \
             patch.object(BatchTextVideoDialog, 'request_background'):
            dialog = BatchTextVideoDialog(store=self.store)
            dialog.rebuild_profile_menu()
            action = next(a for a in dialog.profile_menu.actions() if a.text() == '删除当前项目…')
            self.assertTrue(action.isEnabled())
            before = self.store.path.read_bytes()
            with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No):
                dialog.delete_current_profile()
            self.assertEqual(self.store.path.read_bytes(), before)
            with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                dialog.delete_current_profile()
            self.assertNotEqual(dialog.state['active_profile'], 'default')
            self.assertEqual(dialog.state['jobs'], [])
            self.assertIn('均保留', dialog.status.text())
            self.assertTrue(self.output.exists())


if __name__ == '__main__': unittest.main()
