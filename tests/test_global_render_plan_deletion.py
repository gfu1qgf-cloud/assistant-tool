"""Queue removal clears render intent without deleting projects or media."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from qt_compat import QtCore, QtWidgets
import test_render_checkboxes as fixtures
from app_plugins.builtin.batch_text_video.store import ProfileStore
from app_plugins.builtin.batch_text_video.render_selection import identifiers


class RenderPlanDeletionTests(unittest.TestCase):
    setUp = fixtures.RenderCheckboxTests.setUp
    setUpClass = fixtures.RenderCheckboxTests.__dict__['setUpClass']
    job = fixtures.RenderCheckboxTests.job
    dialog = fixtures.RenderCheckboxTests.dialog

    def select_plan(self, panel, identifier):
        panel.refresh()
        for row in range(panel.table.rowCount()):
            if panel.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) == identifier:
                panel.table.selectRow(row); return
        self.fail('plan missing')

    def test_staged_plan_can_be_deleted_from_visible_button_and_stays_deleted(self):
        output = self.root/'finished.mp4'; output.write_bytes(b'finished video')
        state = self.store.load(); state['jobs'][0]['output'] = str(output)
        self.store.save(state, state['revision'])
        dialog = self.dialog(); panel = dialog.render_queue_panel
        self.assertTrue(dialog.persist())
        plan = self.store.load()['render_plans'][0]
        before = copy.deepcopy(self.store.load()); self.select_plan(panel, plan['id'])
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            panel.delete_button.click()
        for _ in range(3): dialog.refresh_tables()
        after = self.store.load()
        self.assertEqual(after['render_plans'], [])
        self.assertEqual(identifiers(after), [])
        self.assertEqual(after['jobs'][0]['title'], before['jobs'][0]['title'])
        self.assertEqual(after['settings'], before['settings'])
        self.assertEqual(len(after['profiles']), len(before['profiles']))
        self.assertEqual(self.source.read_bytes(), b'synthetic')
        self.assertEqual(output.read_bytes(), b'finished video')
        self.assertFalse(panel.delete_button.isEnabled())
        self.assertIn(panel.delete_action, panel.table.actions())

    def test_terminal_records_are_removable_without_clearing_another_pending_plan(self):
        for status in ('succeeded', 'failed', 'canceled', 'interrupted'):
            with self.subTest(status=status):
                state = self.store.load(); state['jobs'][0]['render_checked'] = True
                self.store.save(state, state['revision'])
                old = self.store.add_render_plan('default', ['a'])
                self.store.update_render_plan(old['id'], status)
                current = self.store.add_render_plan('default', ['a'])
                self.store.remove_render_plan(old['id'])
                state = self.store.load()
                self.assertEqual([p['id'] for p in state['render_plans']], [current['id']])
                self.assertEqual(identifiers(state), ['a'])
                self.store.remove_render_plan(current['id'])

    def test_submitted_record_requires_terminal_host_evidence(self):
        plan = self.store.add_render_plan('default', ['a'])
        self.store.update_render_plan(plan['id'], 'submitted', 'native-task')
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '先在后台任务'):
            self.store.remove_render_plan(plan['id'])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.remove_render_plan(plan['id'], finished_task_ids={'native-task'})
        self.assertFalse(self.store.load()['render_plans'])

    def test_ui_removes_submitted_orphan_only_with_finished_manager_record(self):
        dialog = self.dialog(); panel = dialog.render_queue_panel
        plan = self.store.load()['render_plans'][0]
        self.store.update_render_plan(plan['id'], 'submitted', 'old-task')
        dialog.context = SimpleNamespace(background_tasks=SimpleNamespace(completion_states=lambda: {'old-task':'interrupted'}),
            log=lambda *_a, **_k: None)
        self.select_plan(panel, plan['id'])
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            panel.delete_selected()
        self.assertFalse(self.store.load()['render_plans'])
        self.assertTrue(dialog.persist(), dialog.status.text())

    def test_running_plan_stops_then_removes_after_finished_callback(self):
        dialog = self.dialog(); panel = dialog.render_queue_panel
        plan = self.store.load()['render_plans'][0]
        self.store.update_render_plan(plan['id'], 'submitted', 'render-task')
        record = {'status':'running', 'percent':25}
        manager = SimpleNamespace(get=lambda _: dict(record))
        handle = SimpleNamespace(task_id='render-task', manager=manager,
            isRunning=lambda: record['status'] == 'running', requestInterruption=MagicMock())
        dialog._host_plans['default'] = handle
        self.select_plan(panel, plan['id'])
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            panel.delete_selected()
        handle.requestInterruption.assert_called_once()
        self.assertIn(plan['id'], panel.pending_removals)
        self.assertTrue(self.store.load()['render_plans'], 'do not forget an executing task')
        record['status'] = 'canceled'
        dialog._host_render_finished('default', handle, plan)
        self.assertFalse(self.store.load()['render_plans'])
        self.assertFalse(panel.pending_removals)
        self.assertFalse(identifiers(self.store.load()))
        self.assertTrue(dialog.persist(), dialog.status.text())
        self.assertTrue(self.source.exists())

    def test_queued_host_cancellation_can_finish_synchronously_without_recreating_plan(self):
        from model.BackgroundTasks import BackgroundTaskManager
        dialog = self.dialog(); panel = dialog.render_queue_panel
        manager = BackgroundTaskManager(history_path=self.root/'history.json')
        dialog.context = SimpleNamespace(background_tasks=manager,
            submit_background_task=lambda **kwargs: manager.submit(owner='batch_text_video', **kwargs),
            log=lambda *_a, **_k: None)
        with patch.object(manager, '_dispatch', return_value=None):
            dialog.start_staged_plans()
            plan = self.store.load()['render_plans'][0]
            self.assertEqual(manager.get(plan['task_id'])['status'], 'queued')
            self.select_plan(panel, plan['id'])
            with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
                panel.delete_selected()
            self.app.processEvents()
            self.assertEqual(manager.get(plan['task_id'])['status'], 'canceled')
            self.assertFalse(panel.pending_removals)
            self.assertFalse(self.store.load()['render_plans'])
            self.assertFalse(dialog._host_plans)
            self.assertNotIn('正在停止', dialog.status.text())
            self.assertTrue(dialog.persist(), dialog.status.text())

    def test_cancel_confirmation_preserves_plan_and_does_not_request_stop(self):
        dialog = self.dialog(); panel = dialog.render_queue_panel
        plan = self.store.load()['render_plans'][0]; self.select_plan(panel, plan['id'])
        before = copy.deepcopy(self.store.load()['render_plans'])
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No):
            panel.delete_selected()
        self.assertEqual(self.store.load()['render_plans'], before)
        self.assertFalse(panel.pending_removals)

    def test_other_project_removal_keeps_current_project_settings_and_jobs(self):
        state = self.store.create_profile(self.store.load(), 'Other')
        other = state['active_profile']; state['jobs'] = [self.job('b')]
        state = self.store.save(state, state['revision']); state = self.store.select_profile(state, 'default')
        plan = self.store.add_render_plan(other, ['b']); before = self.store.profile_state('default')
        self.store.remove_render_plan(plan['id'])
        after = self.store.profile_state('default')
        self.assertEqual(after['jobs'], before['jobs']); self.assertEqual(after['settings'], before['settings'])
        self.assertFalse(identifiers(ProfileStore(self.store.directory, other).load()))
        self.assertEqual(len(after['profiles']), 2)


if __name__ == '__main__': unittest.main()
