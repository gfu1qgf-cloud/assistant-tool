"""Bulk row selection and context-menu actions; no real Google services."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtGui, QtTest, QtWidgets
from app_plugins.builtin.ai_agent_assistant.plugin import AIAgentAssistantPlugin
from app_plugins.builtin.ai_agent_assistant.sheets import _execute
from model.AppDataStorage import data_directory
from test_ai_agent_assistant import target, URL


class AgentBulkUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.main = QtWidgets.QWidget(); self.plugin = AIAgentAssistantPlugin()
        self.context = SimpleNamespace(parent_widget=self.main, storage=data_directory(self.temp.name),
            register_command=Mock(), register_main_widget=Mock(), register_google_authorization=Mock(),
            task_language=lambda _task: 'sk', task_targets=lambda _rows: [], selected_task_rows=lambda: [0],
            current_task_targets=lambda: [], log=Mock())
        self.plugin.register(self.context)
        self.plugin.store.save_settings({'url': URL, 'sheet': '口播表格'})
        self.dialog = self.plugin.open_dialog(); self.dialog.show(); self.app.processEvents()
    def tearDown(self):
        self.main.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()
    def stage(self, count=3):
        return self.plugin.import_targets([target(self.temp.name, identifier=str(index)) for index in range(count)])

    def test_import_auto_selects_all_new_ready_rows_without_checkboxes(self):
        self.stage(100)
        self.assertEqual(len(self.dialog.selected_ids()), 100)
        self.assertEqual(self.dialog.table.columnCount(), 10)
        self.assertIn('100', self.dialog.send.text())
        self.assertFalse(self.dialog.setup_panel.isVisible())
        for row in range(100):
            self.assertIsNone(self.dialog.table.item(row, 0).data(QtCore.Qt.ItemDataRole.CheckStateRole))

    def test_ctrl_a_and_shift_select_ranges(self):
        self.stage(6); self.dialog.clear_selection(); self.dialog.table.setFocus()
        QtTest.QTest.keyClick(self.dialog.table, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(self.dialog.selected_ids()), 6)
        table = self.dialog.table
        start = table.visualItemRect(table.item(1, 0)).center()
        end = table.visualItemRect(table.item(3, 0)).center()
        QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=start)
        QtTest.QTest.mouseClick(table.viewport(), QtCore.Qt.MouseButton.LeftButton,
                               QtCore.Qt.KeyboardModifier.ShiftModifier, pos=end)
        self.assertEqual(len(self.dialog.selected_ids()), 3)

    def test_right_click_inside_selection_preserves_batch(self):
        self.stage(4); selected = self.dialog.selected_ids()
        with patch.object(QtWidgets.QMenu, 'exec'):
            self.dialog.show_context_menu(self.dialog.table.visualItemRect(self.dialog.table.item(1, 0)).center())
        self.assertEqual(self.dialog.selected_ids(), selected)

    def test_context_actions_operate_on_exact_selected_ids(self):
        self.stage(4); chosen = self.dialog.selected_ids()[1:3]; self.dialog._select_ids(chosen)
        with patch.object(self.plugin, 'submit') as send, patch.object(self.plugin, 'copy_tasks') as copy:
            self.dialog.actions['submit'].trigger(); self.assertEqual(send.call_args.args[1:], (chosen, 'submit'))
            self.dialog.actions['check'].trigger(); self.assertEqual(send.call_args.args[1:], (chosen, 'check'))
            self.dialog.actions['copy'].trigger(); copy.assert_called_once_with(chosen)

    def test_refresh_preserves_identity_when_new_rows_shift_positions(self):
        self.stage(3); chosen = self.dialog.selected_ids()[1:2]; self.dialog._select_ids(chosen)
        self.plugin.store.stage([self.plugin.store.jobs()[0]['payload']], new_version=True)
        self.dialog.reload(); self.assertEqual(self.dialog.selected_ids(), chosen)
        self.dialog.clear_selection(); self.dialog.reload()
        self.assertEqual(self.dialog._selected_ids(), []); self.assertFalse(self.dialog.send.isEnabled())

    def test_import_new_batch_does_not_select_old_unrelated_drafts(self):
        self.stage(3)
        result = self.plugin.import_targets([target(self.temp.name, identifier='new')])
        self.assertEqual(self.dialog.selected_ids(), result['imported'])

    def test_only_ready_drafts_submit_but_history_can_be_checked(self):
        self.stage(3); jobs = self.plugin.store.jobs()
        self.plugin.store.mark([jobs[0]['id']], 'submitted', '', 'done')
        self.plugin.store.mark([jobs[1]['id']], 'uncertain', '', 'check first')
        self.dialog.reload(); self.dialog.select_all()
        with patch.object(self.plugin, 'submit') as send:
            self.dialog.submit(); self.assertEqual(send.call_args.args[1], [jobs[2]['id']])
            self.dialog.select_uncertain(); self.dialog.check_result()
            self.assertEqual(send.call_args.args[1:], ([jobs[1]['id']], 'check'))

    def test_force_remove_confirmed_batch_includes_submitted_and_uncertain_local_records(self):
        self.stage(4); jobs = self.plugin.store.jobs()
        self.plugin.store.mark([jobs[0]['id']], 'submitted', '', 'done')
        self.plugin.store.mark([jobs[1]['id']], 'uncertain', '', 'check')
        self.dialog.reload(); self.dialog.select_all()
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No):
            self.dialog.remove_drafts(); self.assertEqual(len(self.plugin.store.jobs()), 4)
        with patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.dialog.remove_drafts()
        self.assertEqual(self.plugin.store.jobs(), [])

    def test_missing_chinese_confirmation_is_selected_but_never_auto_approved(self):
        self.plugin.import_targets([target(self.temp.name, chinese=None)])
        self.assertEqual(len(self.dialog._selected_ids()), 1)
        self.assertTrue(self.dialog.send.isEnabled())
        with patch.object(self.plugin, 'submit') as submit, patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No):
            self.dialog.submit(); submit.assert_not_called()
        self.assertFalse(self.plugin.store.jobs()[0]['payload']['chinese_confirmed'])

    def test_58_legacy_drafts_bulk_confirm_and_prepare_once(self):
        values = [target(self.temp.name, identifier=str(index), chinese=None) for index in range(58)]
        for item in values: item['task'].task_reference_link = '温暖自然光，素材 https://drive.google.com/file/d/synthetic/view'
        self.plugin.import_targets(values)
        ids = self.dialog.selected_ids()
        self.assertEqual(len(ids), 58); self.assertIn('58', self.dialog.send.text())
        self.assertTrue(self.dialog.send.isEnabled()); self.assertTrue(self.dialog.actions['submit'].isEnabled())
        self.assertEqual(len(self.dialog._confirm), 58)
        with patch.object(self.plugin, 'submit') as submit, patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes) as question:
            self.dialog.submit()
            question.assert_called_once(); submit.assert_called_once()
            self.assertEqual(submit.call_args.args[1], ids)
            self.assertEqual(question.call_args.args[-1], QtWidgets.QMessageBox.StandardButton.No)
        for job in self.plugin.store.jobs():
            self.assertTrue(job['payload']['chinese_confirmed'])
            self.assertEqual(job['payload']['reference'], 'https://drive.google.com/file/d/synthetic/view')
            self.assertIn('温暖自然光，素材', job['payload']['requirements'])
            self.assertEqual(job['state'], 'draft')
        self.assertEqual(set(self.dialog.selected_ids()), set(ids))
        self.assertEqual(len(self.dialog._ready), 58)

    def test_invalid_selected_draft_explains_reason_instead_of_disabled_submit(self):
        self.stage(1); key = self.dialog.selected_ids()[0]
        self.plugin.store.edit(key, 'spoken', ''); self.dialog.reload()
        self.assertTrue(self.dialog.send.isEnabled())
        with patch.object(self.plugin, 'submit') as submit, patch.object(QtWidgets.QMessageBox, 'information') as info:
            self.dialog.submit(); submit.assert_not_called(); info.assert_called_once()
        self.assertIn('口播原文', self.dialog.status.text())

    def test_canceled_bulk_copy_does_not_modify_data_or_copy(self):
        self.plugin.import_targets([target(self.temp.name, chinese=None)])
        before = self.plugin.store.jobs()
        with patch.object(self.plugin, 'copy_tasks') as copy, patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.No):
            self.dialog.copy_rows(); copy.assert_not_called()
        self.assertEqual(self.plugin.store.jobs(), before)

    def test_copy_also_prepares_legacy_chinese_and_references(self):
        item = target(self.temp.name, chinese=None); item['task'].task_reference_link = '说明 https://drive.google.com/file/d/test/view'
        self.plugin.import_targets([item]); ids = self.dialog.selected_ids()
        with patch.object(self.plugin, 'copy_tasks') as copy, patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.dialog.copy_rows(); copy.assert_called_once_with(ids)
        self.assertTrue(self.plugin.store.jobs()[0]['payload']['chinese_confirmed'])

    def test_concurrent_edit_during_confirmation_is_not_overwritten_or_sent(self):
        self.plugin.import_targets([target(self.temp.name, chinese=None)]); ids = self.dialog.selected_ids()
        def changed(*_args):
            self.plugin.store.edit(ids[0], 'spoken', 'concurrent change')
            return QtWidgets.QMessageBox.StandardButton.Yes
        with patch.object(self.plugin, 'submit') as submit, patch.object(QtWidgets.QMessageBox, 'question', side_effect=changed):
            self.dialog.submit(); submit.assert_not_called()
        value = self.plugin.store.jobs()[0]['payload']
        self.assertEqual(value['spoken'], 'concurrent change'); self.assertFalse(value['chinese_confirmed'])
        self.assertIn('发生变化', self.dialog.status.text())

    def test_busy_has_visible_cancel_and_disables_mutating_actions(self):
        self.stage(); self.dialog.busy(True)
        self.assertTrue(self.dialog.cancel.isVisible()); self.assertTrue(self.dialog.cancel.isEnabled())
        self.assertFalse(self.dialog.send.isEnabled()); self.assertFalse(self.dialog.table.isEnabled())
        for key in ('copy', 'remove', 'submit', 'check', 'import_all'): self.assertFalse(self.dialog.actions[key].isEnabled())
        self.dialog.busy(False); self.assertFalse(self.dialog.cancel.isVisible()); self.assertTrue(self.dialog.send.isEnabled())

    def test_low_frequency_actions_are_menu_actions_not_main_buttons(self):
        self.stage()
        visible = [button.text() for button in self.dialog.findChildren(QtWidgets.QPushButton) if button.isVisible()]
        self.assertEqual(visible, [self.dialog.send.text()])
        menu = self.dialog.create_context_menu()
        titles = [action.text() for action in menu.actions()]
        self.assertIn('复制所选任务表格', titles); self.assertIn('核对所选提交结果', titles)
        menu.deleteLater()


class SheetsErrorMessagesTests(unittest.TestCase):
    def error(self, reason):
        import httplib2
        from googleapiclient.errors import HttpError
        body = {'error': {'message': 'SDK error', 'details': [{'reason': reason,
            'metadata': {'service': 'sheets.googleapis.com', 'consumer': 'projects/123456'}}]}}
        return HttpError(httplib2.Response({'status': '403'}), json.dumps(body).encode())
    def test_disabled_api_is_not_misreported_as_sharing_permission(self):
        request = Mock(); request.execute.side_effect = self.error('SERVICE_DISABLED')
        with self.assertRaisesRegex(ValueError, '尚未启用 Google Sheets API') as caught: _execute(request)
        self.assertIn('123456', str(caught.exception)); self.assertIn('不是表格共享权限', str(caught.exception))
        request.execute.assert_called_once_with(num_retries=0)
    def test_other_permission_error_is_not_relabelled(self):
        error = self.error('PERMISSION_DENIED'); request = Mock(); request.execute.side_effect = error
        with self.assertRaises(type(error)) as caught: _execute(request)
        self.assertIs(caught.exception, error)


if __name__ == '__main__': unittest.main()
