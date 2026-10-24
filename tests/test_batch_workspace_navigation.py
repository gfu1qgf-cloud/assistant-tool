"""Real Qt task paths: direct actions, locked browsing and batched selection."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.store import Store
from app_plugins.builtin.batch_text_video.custom_pools import create_pool, add_pool_paths


class WorkspaceNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root/'data'); state = self.store.load()
        state['jobs'] = [dict(id=str(i), path='', name=f'Task-{i}', title=f'标题 {i}', body='',
            status='已完成' if i < 40 else '需处理' if i >= 70 else '待生成', output='', error='') for i in range(80)]
        pool = create_pool(state, '祷告人物图片', 'image'); self.pool_id = pool['id']
        images = []
        for i in range(3):
            path = self.root/f'picture-{i}.png'
            image = QtGui.QImage(48, 48, QtGui.QImage.Format.Format_RGBA8888)
            image.fill(QtGui.QColor(50+i*20, 120, 180)); image.save(str(path)); images.append(str(path))
        add_pool_paths(pool, images)
        state = self.store.save(state, 0)
        state = self.store.create_profile(state, '另一个项目')
        self.other_id = state['active_profile']
        self.store.select_profile(state, 'default')
        self.poster = patch.object(BatchTextVideoDialog, 'request_background'); self.poster.start()
        self.overlay = patch.object(BatchTextVideoDialog, '_start_overlay'); self.overlay.start()
        self.dialog = BatchTextVideoDialog(store=self.store)
        self.dialog.resize(1250, 850); self.dialog.show(); self.app.processEvents()
        self.dialog.edit_timer.stop(); self.dialog.render_settings_timer.stop()

    def tearDown(self):
        self.dialog._host_plans.clear()
        self.dialog.close(); self.dialog.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.poster.stop(); self.overlay.stop(); self.temp.cleanup()

    def test_project_selector_and_visible_workflow_do_the_named_actions(self):
        dialog = self.dialog
        self.assertEqual(dialog.current_id, '40')
        self.assertEqual(dialog.project_selector.currentData(), 'default')
        dialog.workflow_buttons['design'].click()
        self.assertTrue(dialog.tabs.currentWidget().isAncestorOf(dialog.layer_panel.catalog))
        self.assertEqual(dialog.right_tabs.currentIndex(), 0)
        dialog.workflow_buttons['resources'].click()
        self.assertIs(dialog.tabs.currentWidget(), dialog.resources)
        self.assertEqual(dialog.active_resource(), dialog.visible_pool_key())
        self.assertTrue(dialog.pool_tools.isVisible())
        self.assertTrue(dialog.new_pool_button.isVisible())
        dialog.workflow_buttons['queue'].click()
        self.assertIs(dialog.right_tabs.currentWidget(), dialog.render_queue_panel)
        with patch('app_plugins.builtin.batch_text_video.pool_controller.PairingDialog') as pairing:
            pairing.return_value.exec.return_value = QtWidgets.QDialog.DialogCode.Rejected
            dialog.workflow_buttons['pairing'].click()
            pairing.assert_called_once()
        with patch.object(dialog, 'persist', return_value=True) as persist:
            dialog.save_project_button.click(); persist.assert_called_once()
        dialog.project_selector.setCurrentIndex(dialog.project_selector.findData(self.other_id))
        dialog.project_selector.activated.emit(dialog.project_selector.currentIndex())
        self.assertEqual(dialog.state['active_profile'], self.other_id)
        self.assertEqual(dialog.project_selector.currentData(), self.other_id)
        self.assertEqual(dialog.quick_export_button.text(), '导出当前1条')
        self.assertEqual(dialog.render_queue_panel.start_button.text(), '开始全部计划')

    def test_pool_actions_target_the_visible_pool_after_a_queue_click(self):
        dialog = self.dialog
        dialog.resources.setCurrentWidget(dialog.custom_pages[self.pool_id])
        dialog.workflow_buttons['resources'].click()
        dialog.jobs.setFocus(); dialog._active_resource_key = 'jobs'
        key = 'pool:'+self.pool_id
        seen = []
        with patch.object(dialog, 'choose_custom_files', side_effect=lambda: seen.append(dialog.active_resource())):
            dialog.pool_add_button.click()
        self.assertEqual(seen, [key])
        dialog.custom_tables[self.pool_id].selectRow(0)
        self.assertIn('3 项', dialog.pool_count_label.text())
        self.assertIn('已选 1 项', dialog.pool_count_label.text())
        dialog._populate_pool_use_menu()
        actions = dialog.pool_use_menu.actions()
        self.assertIn('固定图层', actions[0].text())
        with patch.object(dialog, 'use_pool_images') as use:
            actions[0].trigger(); use.assert_called_once_with(False, key)
        with patch.object(dialog, 'use_pool_images') as use:
            actions[1].trigger(); use.assert_called_once_with(True, key)

    def test_live_filter_search_and_jump_work_without_storage_reads_during_render(self):
        dialog = self.dialog; tools = dialog.render_queue_panel.tools
        current = dialog.current_id
        with patch.object(dialog, 'is_batch_busy', return_value=True), \
             patch.object(dialog.store, 'load', side_effect=AssertionError('filter must not read state')):
            dialog._set_work_controls(True)
            self.assertFalse(dialog.save_project_button.isEnabled())
            self.assertFalse(dialog.workflow_buttons['pairing'].isEnabled())
            self.assertTrue(tools.search.isEnabled()); self.assertTrue(tools.filter.isEnabled())
            tools.filter.setCurrentIndex(tools.filter.findData('completed'))
            self.assertEqual(len(tools.visible_rows), 40)
            tools.filter.setCurrentIndex(tools.filter.findData('unfinished'))
            tools.search.setText('task-7')
            self.assertEqual(tools.visible_rows, set(range(70, 80)))
            dialog._host_item_stage('default', '71', '编码等待：30秒未推进')
            tools.filter.setCurrentIndex(tools.filter.findData('completed'))
            tools.search.clear()
            tools.jump_button.click()
            self.assertEqual(dialog.jobs.currentRow(), 71)
            self.assertFalse(dialog.jobs.isRowHidden(71))
            self.assertEqual(dialog.current_id, current)
            self.assertIn('30秒未推进', tools.detail.text())
            tools.filter.setCurrentIndex(tools.filter.findData('active'))
            self.assertEqual(tools.visible_rows, {71})
            dialog._host_item_stage('default', '71', '已完成')
            self.assertEqual(tools.visible_rows, set())
            self.assertIn('显示 0/80', tools.result_count.text())
            self.assertTrue(tools.empty_hint.isVisible())
            tools.clear_filter_button.click()
            self.assertEqual(len(tools.visible_rows), 80)
        dialog._set_work_controls(False)

    def test_bulk_unfinished_checks_save_once_and_keep_completed_outputs(self):
        dialog = self.dialog; tools = dialog.render_queue_panel.tools
        with patch.object(dialog.store, 'save', wraps=dialog.store.save) as save:
            tools.unfinished_button.click()
        self.assertEqual(save.call_count, 1)
        state = self.store.load()
        self.assertEqual([job['id'] for job in state['jobs'] if job['render_checked']], list(map(str, range(40,80))))
        self.assertEqual(sum(job['status'] == '已完成' for job in state['jobs']), 40)
        self.assertEqual(len(state['jobs']), 80)
        tools.filter.setCurrentIndex(tools.filter.findData('failed'))
        tools.set_checks('visible')
        self.assertEqual([job['id'] for job in self.store.load()['jobs'] if job['render_checked']], list(map(str,range(70,80))))
        tools.set_checks('none')
        self.assertFalse(any(job['render_checked'] for job in self.store.load()['jobs']))

    def test_64_is_available_saved_and_summarized_while_global_defaults_are_preserved(self):
        dialog = self.dialog; section = dialog.render_queue_panel.settings_section
        self.assertEqual(dialog.controls['export_workers'].maximum(), 64)
        self.assertEqual(dialog.controls['export_workers'].value(), 2)
        self.assertFalse(section.toggle.isChecked())
        dialog.controls['export_workers'].setValue(64)
        self.assertIn('64路', section.toggle.text())
        self.assertTrue(dialog.save_render_settings())
        self.assertEqual(self.store.load()['render_settings']['export_workers'], 64)
        dialog.switch_profile(self.other_id)
        self.assertEqual(dialog.controls['export_workers'].value(), 64)
        self.assertTrue(dialog.render_queue_panel.settings_section.content.isAncestorOf(dialog.controls['overwrite']))
        if os.environ.get('LZX_WORKSPACE_SCREENSHOT'):
            dialog.switch_profile('default')
            dialog.workflow_buttons['resources'].click()
            dialog.resources.setCurrentWidget(dialog.custom_pages[self.pool_id])
            dialog.workflow_buttons['queue'].click()
            self.app.processEvents()
            self.assertTrue(dialog.grab().save(os.environ['LZX_WORKSPACE_SCREENSHOT']))

    def test_failed_bulk_save_rolls_back_and_busy_project_still_allows_project_browsing(self):
        dialog = self.dialog; tools = dialog.render_queue_panel.tools
        before = [(job['id'], job.get('render_checked')) for job in dialog.state['jobs']]
        with patch.object(dialog, 'persist', return_value=False): tools.set_checks('none')
        self.assertEqual([(job['id'], job.get('render_checked')) for job in dialog.state['jobs']], before)
        dialog._host_plans['default'] = object()
        dialog.update_navigation_busy(True)
        self.assertTrue(dialog.project_selector.isEnabled())
        self.assertFalse(dialog.save_project_button.isEnabled())
        self.assertFalse(tools.unfinished_button.isEnabled())
        self.assertTrue(tools.search.isEnabled())
        dialog._host_plans.clear(); dialog.update_navigation_busy(False)

    def test_failed_render_settings_save_is_visible_to_the_user(self):
        with patch.object(self.store, 'save_render_settings', side_effect=ValueError('设置已被其他窗口修改')):
            self.assertFalse(self.dialog.save_render_settings())
        self.assertIn('渲染选项未保存', self.dialog.status.text())

    def test_empty_project_and_zero_search_results_offer_safe_next_steps(self):
        dialog = self.dialog; tools = dialog.render_queue_panel.tools
        before = [job.get('render_checked') for job in dialog.state['jobs']]
        tools.search.setText('a-name-that-does-not-exist')
        with patch.object(dialog, 'persist') as persist: tools.set_checks('visible')
        persist.assert_not_called()
        self.assertEqual([job.get('render_checked') for job in dialog.state['jobs']], before)
        self.assertIn('保持不变', dialog.status.text())
        dialog.switch_profile(self.other_id)
        self.assertEqual(dialog.jobs.rowCount(), 0)
        self.assertTrue(tools.empty_hint.isVisible())
        self.assertIn('批量搭配', tools.empty_text.text())
        self.assertFalse(tools.clear_filter_button.isVisible())
        self.assertFalse(tools.unfinished_button.isEnabled())
        self.assertFalse(tools.jump_button.isEnabled())
