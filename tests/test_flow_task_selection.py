"""Real Qt task selection with synthetic tasks; no account/browser actions."""
import copy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from qt_compat import QtCore, QtGui, QtWidgets
import test_flow_assistant as fixtures
from app_plugins.host import PluginContext
from app_plugins.builtin.flow_assistant.plugin import FlowAssistantPlugin
from app_plugins.builtin.flow_assistant.task_ui import TaskSelector
from app_plugins.builtin.flow_assistant.ui import FlowDialog


class TaskSelectionTests(unittest.TestCase):
    setUpClass = fixtures.FlowTests.__dict__['setUpClass']
    setUp = fixtures.FlowTests.setUp

    def host_target(self, number, title='测试任务', kind='reels', root=None):
        directory = (root or self.target.parent.parent) / kind / str(number)
        directory.mkdir(parents=True, exist_ok=True)
        image = QtGui.QImage(360, 640, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('#55bada')); image.save(str(directory / 'chosen.png'))
        return {'target_dir': str(directory), 'label': 'legacy label must not be the task number',
                'task': SimpleNamespace(task_id=str(number), task_name=title,
                    task_type=kind, task_date='1017', task_video_prompt='prompt ' + str(number))}

    def plugin(self, targets):
        parent = QtWidgets.QWidget(); self.addCleanup(parent.close)
        plugin = FlowAssistantPlugin()
        plugin.context = SimpleNamespace(parent_widget=parent, current_task_targets=MagicMock(return_value=targets),
            task_targets=MagicMock(return_value=targets), log=MagicMock())
        commands = []; plugin.command.connect(commands.append)
        self.addCleanup(lambda: plugin.dialog.close() if plugin.dialog else None)
        return plugin, commands

    def test_host_reads_loaded_table_without_changing_selection_or_reading_date_picker(self):
        window = SimpleNamespace(loaded_project_dir=self.root / 'loaded', task_list=[object(), object()],
            taskTargetsForRows=MagicMock(return_value=['safe targets']), selectedTaskRows=MagicMock(), getTodayDir=MagicMock())
        context = PluginContext(SimpleNamespace(main_window=window), 'flow_assistant')
        self.assertEqual(context.current_task_targets(), ['safe targets'])
        self.assertEqual(list(window.taskTargetsForRows.call_args.args[0]), [0, 1])
        self.assertTrue(window.taskTargetsForRows.call_args.kwargs['require_loaded'])
        window.selectedTaskRows.assert_not_called(); window.getTodayDir.assert_not_called()

    def test_unloaded_project_does_not_guess_paths_or_open_file_picker(self):
        window = SimpleNamespace(loaded_project_dir=None, task_list=[object()], taskTargetsForRows=MagicMock())
        context = PluginContext(SimpleNamespace(main_window=window), 'flow_assistant')
        self.assertEqual(context.current_task_targets(), [])
        window.taskTargetsForRows.assert_not_called()

    def test_main_menu_populates_task_numbers_and_their_own_images(self):
        targets = [self.host_target(60000, '第一条标题'), self.host_target(60001, '第二条标题')]
        plugin, commands = self.plugin(targets); dialog = plugin.open_dialog()
        picker = dialog.manual.picker
        plugin.context.current_task_targets.assert_called_once()
        self.assertEqual(picker.target.count(), 2)
        self.assertTrue(picker.target.itemText(0).startswith('60000'))
        self.assertNotIn('legacy label', picker.target.itemText(0))
        self.assertIn('第一条标题', picker.task_selector.details.text())
        picker.target.setCurrentIndex(1)
        self.assertEqual(Path(picker.image_path).parent, Path(targets[1]['target_dir']))
        self.assertEqual(dialog.manual.prompt.toPlainText(), 'prompt 60001')
        self.assertFalse(any(command['action'] == 'enqueue' for command in commands))
        self.assertIs(dialog.tabs.currentWidget(), dialog.manual)

    def test_search_matches_task_number_or_name_not_arbitrary_path(self):
        plugin, commands = self.plugin([self.host_target(8010, '冰雪'), self.host_target(8011, '春天')])
        dialog = plugin.open_dialog(); picker = dialog.manual.picker
        picker.task_selector.search.setText('8011')
        self.assertEqual(picker.target.count(), 1)
        self.assertEqual(picker.target.currentData()['task']['task_id'], '8011')
        picker.task_selector.search.setText('冰雪')
        self.assertEqual(picker.target.currentData()['task']['task_id'], '8010')
        picker.task_selector.search.setText('99999')
        self.assertIsNone(picker.target.currentData()); self.assertEqual(picker.images.count(), 0)
        dialog.manual.enqueue()
        self.assertIn('任务编号', dialog.manual.status.text())
        self.assertFalse(any(command['action'] == 'enqueue' for command in commands))
        self.assertFalse(picker.target.isEditable())

    def test_duplicate_numbers_across_types_remain_distinct_targets(self):
        targets = [self.host_target(42, kind='reels'), self.host_target(42, kind='口播')]
        plugin, _ = self.plugin(targets); picker = plugin.open_dialog().manual.picker
        self.assertNotEqual(picker.target.itemText(0), picker.target.itemText(1))
        picker.target.setCurrentIndex(1)
        self.assertEqual(picker.selection()[0]['target_dir'], targets[1]['target_dir'])
        self.assertEqual(Path(picker.selection()[1]).parent, Path(targets[1]['target_dir']))

    def test_right_click_retains_explicit_scope_and_defaults_to_clicked_task(self):
        targets = [self.host_target(8010), self.host_target(8011)]
        plugin, _ = self.plugin(targets); dialog = plugin.open_dialog()
        plugin.context.task_targets.return_value = [targets[1]]
        plugin.open_tasks([1]); picker = dialog.manual.picker
        self.assertEqual(picker.target.count(), 1)
        self.assertEqual(picker.target.currentData()['task']['task_id'], '8011')
        dialog.add_all_images()
        self.assertEqual(dialog.compose.rowCount(), 1)
        self.assertEqual(dialog.compose.item(0, 0).data(QtCore.Qt.ItemDataRole.UserRole)['target']['target_dir'], targets[1]['target_dir'])

    def test_refresh_preserves_current_image_draft_and_batch_compose(self):
        targets = [self.host_target(8010), self.host_target(8011)]
        plugin, _ = self.plugin(targets); dialog = plugin.open_dialog(); panel = dialog.manual
        panel.picker.target.setCurrentIndex(1)
        panel.prompt.setPlainText('do not roll back')
        panel.options.count.setValue(3)
        dialog.add_image(dialog.targets[0], Path(targets[0]['target_dir']) / 'chosen.png')
        targets.append(self.host_target(8012))
        plugin.refresh_tasks()
        self.assertEqual(panel.picker.target.count(), 3)
        self.assertEqual(panel.picker.target.currentData()['task']['task_id'], '8011')
        self.assertEqual(panel.prompt.toPlainText(), 'do not roll back')
        self.assertEqual(panel.options.count.value(), 3)
        self.assertEqual(dialog.compose.rowCount(), 1)

    def test_refresh_buttons_use_host_catalog_not_an_external_directory(self):
        plugin, _ = self.plugin([self.host_target(8010)])
        dialog = plugin.open_dialog()
        plugin.context.current_task_targets.reset_mock()
        dialog.manual.picker.task_selector.refresh.click()
        dialog.task_selector.refresh.click()
        self.assertEqual(plugin.context.current_task_targets.call_count, 2)

    def test_refresh_is_deferred_during_durable_enqueue(self):
        plugin, _ = self.plugin([self.host_target(8010)])
        dialog = plugin.open_dialog(); picker = dialog.manual.picker
        dialog.manual.request_id = 'saving'
        plugin.context.current_task_targets.return_value = [self.host_target(8020)]
        plugin.refresh_tasks()
        self.assertEqual(picker.target.currentData()['task']['task_id'], '8010')
        self.assertIn('保存回执', dialog.note.text())

    def test_changed_loaded_project_with_same_number_rebinds_only_new_draft(self):
        old = self.host_target(8010); plugin, _ = self.plugin([old])
        dialog = plugin.open_dialog(); panel = dialog.manual
        panel.prompt.setPlainText('old project draft'); old_key = panel.current_key
        new = self.host_target(8010, root=self.root / 'new-project')
        plugin.context.current_task_targets.return_value = [new]
        plugin.refresh_tasks()
        self.assertEqual(panel.current_target['target_dir'], new['target_dir'])
        self.assertEqual(panel.prompt.toPlainText(), 'prompt 8010')
        self.assertEqual(panel.drafts[old_key]['prompt'], 'old project draft')
        self.assertNotEqual(panel.current_key, old_key)

    def test_identical_catalog_update_does_not_reload_images_or_reset_search(self):
        selector = TaskSelector(); self.addCleanup(selector.close)
        targets = FlowAssistantPlugin._snapshots([self.host_target(8010), self.host_target(8011)])
        selector.set_targets(targets); selector.search.setText('8011')
        changed = MagicMock(); selector.changed.connect(changed)
        selector.set_targets(copy.deepcopy(targets))
        self.assertEqual(selector.search.text(), '8011')
        changed.assert_not_called()

    def test_no_tasks_has_actionable_guidance(self):
        plugin, _ = self.plugin([]); dialog = plugin.open_dialog()
        self.assertEqual(dialog.manual.picker.target.count(), 0)
        self.assertIn('先加载任务表', dialog.manual.picker.task_selector.details.text())
        self.assertIn('不用手动填写目录', dialog.manual.picker.task_selector.details.text())

    def test_optional_task_selector_visual_qa(self):
        path = os.environ.get('LZX_FLOW_TASK_PICKER_SCREENSHOT')
        if not path: self.skipTest('optional visual QA snapshot')
        plugin, _ = self.plugin([self.host_target(60000, '按编号选择任务，图片和成品自动对应到目录'), self.host_target(60001)])
        dialog = plugin.open_dialog(); self.app.processEvents(); dialog.grab().save(path)


if __name__ == '__main__': unittest.main()
