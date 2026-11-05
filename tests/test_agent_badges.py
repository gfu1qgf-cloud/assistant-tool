"""Persistent unread markers, memory-only UI decoration, no cloud writes."""
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.api import TaskBadge, PluginCommand, MAIN_MENU, TASK_CONTEXT_MENU
from app_plugins.host import PluginHost
from app_plugins.builtin.ai_agent_assistant.store import Store
from app_plugins.builtin.ai_agent_assistant.tasks import snapshot
from app_plugins.builtin.ai_agent_assistant.notice_data import notice_snapshot, mark_versions_seen, unmark_versions_seen, directory_key, run_notice_snapshot
from app_plugins.builtin.ai_agent_assistant.notices import ReceiptNotices
from test_ai_agent_assistant import target


class NoticeDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root/'private')
        self.value = snapshot(target(self.root), 'sk')
        self.key = self.store.stage([self.value])['imported'][0]
        self.directory = Path(self.value['target_dir']); (self.directory/'result').mkdir(parents=True)
    def file(self, identifier='file_one', data=b'synthetic-video', *, job=None):
        job = job or self.key
        path = self.directory/'result'/f'{job}-{identifier}.mp4'; path.write_bytes(data)
        value = {'state': 'downloaded', 'path': str(path), 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        self.store.save_delivery(job, identifier, value)
        return [job, identifier, value['sha256']]
    def done(self, total=1, *, job=None):
        job = job or self.key
        self.store.save_delivery(job, '@discovery', {'state': 'checked', 'total': total})
        self.store.delivery_note(job, '已下载', '已校验')

    def test_new_download_persists_badge_and_notification_is_claimed_once(self):
        self.file(); self.done()
        first = notice_snapshot(self.store); second = notice_snapshot(Store(self.store.root.path))
        self.assertEqual(first['items'][0]['text'], '已收取')
        self.assertEqual(first['notify_files'], 1); self.assertEqual(second['notify_files'], 0)
        self.assertEqual(second['items'][0]['unread'], 1)

    def test_received_labels_stay_short_across_status_and_review_combinations(self):
        version=self.file(); self.done(3)
        for status in ('已下载', '已收取部分', '已下载·待回写', '收取待核对',
                       '收取待重试', '正在下载', '已收取·任务未完成'):
            for review in ('', 'AI预审不通过｜第2段漏读'):
                self.store.delivery_note(self.key,status,'待补第2段')
                self.store.save_delivery(self.key,'@ai_review',{'text':review})
                with self.subTest(status=status,review=review):
                    item=notice_snapshot(self.store,announce=False)['items'][0]
                    self.assertEqual(item['text'],'已收取')
                    self.assertIn('1/3',item['tooltip']); self.assertIn('待补第2段',item['tooltip'])
                    if review: self.assertEqual(item['tone'],'error')
                    mark_versions_seen(self.store,[version])
                    viewed=notice_snapshot(self.store,announce=False)['items'][0]
                    self.assertEqual(viewed['text'],'已查看')
                    if review: self.assertEqual(viewed['tone'],'error')
                    unmark_versions_seen(self.store,[version])
    def test_seen_marker_preserves_permanent_received_badge_and_file(self):
        version = self.file(); self.done(); mark_versions_seen(self.store, [version])
        value = notice_snapshot(self.store)
        self.assertEqual(value['items'][0]['text'], '已查看'); self.assertEqual(value['notify_files'], 0)
        self.assertTrue(list((self.directory/'result').glob('*.mp4')))

    def test_seen_can_be_reversed_after_restart_without_download_or_notification(self):
        version=self.file(); self.done(); mark_versions_seen(self.store,[version])
        value=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertEqual(value['seen_versions'],[version]); self.assertEqual(value['unread'],0)
        run=SimpleNamespace(check_canceled=Mock())
        result=run_notice_snapshot({'root':str(self.store.root.path),'unseen':[version],'ui_generation':4},run)
        self.assertEqual(result['items'][0]['text'],'已收取'); self.assertEqual(result['notify_files'],0)
        persisted=notice_snapshot(Store(self.store.root.path))
        self.assertEqual(persisted['items'][0]['unread'],1); self.assertEqual(persisted['notify_files'],0)
        self.assertTrue(list((self.directory/'result').glob('*.mp4')))

    def test_undo_is_scoped_to_selected_exact_version(self):
        first=self.file('first'); second=self.file('second'); self.done(2)
        mark_versions_seen(self.store,[first,second]); unmark_versions_seen(self.store,[first])
        item=notice_snapshot(self.store,announce=False)['items'][0]
        self.assertEqual(item['versions'],[first]); self.assertEqual(item['seen_versions'],[second])
        self.file('first',data=b'later-version'); mark_versions_seen(self.store,[self.file('first',data=b'later-version')])
        unmark_versions_seen(self.store,[first])
        self.assertEqual(notice_snapshot(self.store,announce=False)['items'][0]['unread'],0)
    def test_same_drive_file_new_checksum_becomes_unread_again(self):
        old = self.file(); self.done(); mark_versions_seen(self.store, [old])
        self.file(data=b'new-version')
        item = notice_snapshot(self.store)['items'][0]
        self.assertEqual(item['unread'], 1); self.assertNotEqual(item['versions'][0], old)
    def test_stale_ack_does_not_mark_later_version_seen(self):
        old = self.file(); self.done(); self.file(data=b'new-version')
        mark_versions_seen(self.store, [old]); self.assertEqual(notice_snapshot(self.store)['items'][0]['unread'], 1)
    def test_partial_download_is_amber_not_green(self):
        self.file(); self.store.save_delivery(self.key, '@discovery', {'state': 'checked', 'total': 5})
        self.store.delivery_note(self.key, '下载待重试', '第二段暂时失败')
        item = notice_snapshot(self.store)['items'][0]
        self.assertEqual(item['text'], '已收取'); self.assertIn('1/5',item['tooltip']); self.assertEqual(item['tone'], 'warning'); self.assertEqual(item['unread'], 1)
    def test_failed_download_without_file_is_error_and_never_notifies_success(self):
        self.store.delivery_note(self.key, '收取待重试', '无访问权限')
        value = notice_snapshot(self.store)
        self.assertEqual(value['items'][0]['tone'], 'error'); self.assertEqual(value['notify_files'], 0)

    def test_all_files_received_but_needs_segment_review_is_not_still_downloading(self):
        self.file(); self.store.save_delivery(self.key, '@discovery', {'state':'checked', 'total':1})
        self.store.delivery_note(self.key, '收取待核对', '段号需核对')
        item=notice_snapshot(self.store)['items'][0]
        self.assertEqual(item['text'],'已收取'); self.assertEqual(item['tone'],'warning')
        self.assertIn('段号需核对', item['tooltip'])

    def test_archive_write_failure_is_separate_from_download_and_success(self):
        self.file(); self.store.save_delivery(self.key,'@discovery',{'state':'checked','total':1})
        self.store.delivery_note(self.key,'已下载·待回写','Sheets暂不可用')
        item=notice_snapshot(self.store)['items'][0]
        self.assertEqual(item['text'],'已收取'); self.assertEqual(item['tone'],'warning')

    def test_stale_preserved_file_is_not_counted_as_current_delivery(self):
        self.file('old'); self.file('new')
        self.store.save_delivery(self.key,'@discovery',{'state':'checked','total':2,'file_ids':['new','not_yet_downloaded']})
        self.store.delivery_note(self.key,'正在下载','')
        item=notice_snapshot(self.store)['items'][0]
        self.assertEqual(item['text'],'已收取'); self.assertIn('1/2',item['tooltip']); self.assertEqual(item['received'],2)
    def test_missing_local_file_does_not_show_success_or_unread(self):
        self.file(); self.done()
        next((self.directory/'result').glob('*.mp4')).unlink()
        value = notice_snapshot(self.store)
        self.assertEqual(value['items'][0]['tone'], 'warning'); self.assertEqual(value['items'][0]['unread'], 0)
        self.assertEqual(value['notify_files'], 0)
    def test_different_project_same_task_number_has_distinct_key(self):
        self.file(); self.done()
        other = snapshot(target(self.root/'other'), 'sk'); other_key = self.store.stage([other])['imported'][0]
        self.store.delivery_note(other_key, '收取待重试', 'other failure')
        items = notice_snapshot(self.store)['items']
        self.assertEqual(len(items), 2); self.assertNotEqual(directory_key(items[0]['directory']), directory_key(items[1]['directory']))
    def test_old_failed_version_does_not_cover_later_success(self):
        self.store.delivery_note(self.key, '收取待重试', 'old failed')
        newer = self.store.stage([self.value], new_version=True)['imported'][0]
        self.file(job=newer); self.done(job=newer)
        self.assertEqual(notice_snapshot(self.store)['items'][0]['tone'], 'success')
    def test_one_batch_combines_notifications_for_multiple_tasks(self):
        self.file(); self.done()
        other = snapshot(target(self.root, '002'), 'sk'); other_key = self.store.stage([other])['imported'][0]
        directory = Path(other['target_dir'])/'result'; directory.mkdir(parents=True)
        path = directory/'other.mp4'; path.write_bytes(b'two')
        self.store.save_delivery(other_key, 'other', {'state': 'downloaded', 'path': str(path), 'size': 3, 'sha256': hashlib.sha256(b'two').hexdigest()})
        self.done(job=other_key)
        value = notice_snapshot(self.store)
        self.assertEqual((value['notify_tasks'], value['notify_files']), (2, 2))


class BadgeWindow(QtWidgets.QWidget):
    def __init__(self, project):
        super().__init__(); self.loaded_project_dir = project; self.logs = []; self.config = {}
        self.task_list = [SimpleNamespace(task_id='01', task_type='FL口播'), SimpleNamespace(task_id='02', task_type='FL口播')]
        self.task_table_widget = QtWidgets.QTableWidget(2, 2, self)
        self.task_table_widget.setHorizontalHeaderLabels(['任务编号', '负责人'])
        self.task_table_widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        for row in range(2):
            self.task_table_widget.setItem(row, 0, QtWidgets.QTableWidgetItem(f'{row+1:02}'))
            self.task_table_widget.setItem(row, 1, QtWidgets.QTableWidgetItem('original'))
    def load_config(self): return self.config
    def appendLog(self, message, **kwargs): self.logs.append(message)
    def showDesktopNotification(self, *args): pass


class TaskBadgeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        from model.TaskReferenceDownloader import task_directory_for
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.window = BadgeWindow(Path(self.temp.name)); self.host = PluginHost(self.window)
        self.plugin = SimpleNamespace(plugin_id='demo', register=lambda context: None)
        self.host.install(self.plugin); self.context = self.host._contexts['demo']
        self.directory = str(task_directory_for(self.window.loaded_project_dir, 'FL口播', '01'))
    def tearDown(self): self.window.deleteLater(); self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()
    def publish(self): self.context.set_task_badges({self.directory: TaskBadge('新成品 · 3', 'success', '3个视频可用', True)})

    def test_badge_does_not_modify_task_text_language_color_or_tooltip(self):
        item = self.window.task_table_widget.item(0, 0)
        item.setBackground(QtGui.QColor('#ffeedd')); item.setToolTip('原语言提示')
        self.publish()
        self.assertEqual(item.text(), '01'); self.assertEqual(item.background().color().name(), '#ffeedd')
        self.assertEqual(item.toolTip(), '原语言提示')
        controller = self.host.task_badge_controller()
        self.assertEqual(controller.badges(self.context.task_badge_keys([0])[0])[0].text, '新成品 · 3')
    def test_filter_has_empty_safe_exit_and_preserves_pre_hidden_rows(self):
        table = self.window.task_table_widget; table.setRowHidden(1, True); self.publish()
        self.assertTrue(table.isRowHidden(1))
        self.context.set_task_badge_filter(True)
        self.assertFalse(table.isRowHidden(0)); self.assertTrue(table.isRowHidden(1))
        self.context.set_task_badges({})
        self.assertTrue(table.isRowHidden(0)); self.assertTrue(table.isRowHidden(1))
        self.context.set_task_badge_filter(False)
        self.assertFalse(table.isRowHidden(0)); self.assertTrue(table.isRowHidden(1))
    def test_hidden_tasks_are_deselected_not_silently_processed(self):
        table = self.window.task_table_widget; table.selectAll(); self.publish()
        self.context.set_task_badge_filter(True)
        self.assertEqual([row.row() for row in table.selectionModel().selectedRows()], [0])
    def test_same_task_number_new_project_does_not_inherit_badge(self):
        self.publish(); self.window.loaded_project_dir = Path(self.temp.name)/'other'
        self.host.notify_project_changed(self.window.loaded_project_dir)
        key = self.context.task_badge_keys([0])[0]
        self.assertEqual(self.host.task_badge_controller().badges(key), [])
    def test_sorting_keeps_badge_bound_to_item_identity(self):
        self.publish(); table = self.window.task_table_widget
        table.sortItems(0, QtCore.Qt.SortOrder.DescendingOrder)
        controller = self.host.task_badge_controller()
        self.assertEqual(controller.badges(self.context.task_badge_keys([0])[0]), [])
        self.assertEqual(controller.badges(self.context.task_badge_keys([1])[0])[0].text, '新成品 · 3')
    def test_rebuilt_same_project_items_regain_badge_bindings(self):
        self.publish(); table = self.window.task_table_widget
        table.setItem(0, 0, QtWidgets.QTableWidgetItem('01'))
        self.host.refresh_task_badges()
        self.assertEqual(self.host.task_badge_controller().badges(self.context.task_badge_keys([0])[0])[0].text, '新成品 · 3')
    def test_context_menu_checkable_command_receives_checked_state(self):
        invoked = Mock(); self.host.register_command('demo', PluginCommand('filter', '只看新成品', invoked,
            frozenset({TASK_CONTEXT_MENU}), checkable=True, checked=lambda: False))
        menu = QtWidgets.QMenu(self.window); actions = self.host.populate_task_context_menu(menu, [0])
        self.assertTrue(actions[0].isCheckable()); actions[0].trigger()
        invoked.assert_called_once_with((0,), True)
    def test_disabled_plugin_layer_is_not_visible(self):
        self.publish(); self.host._enabled_states['demo'] = False
        self.assertEqual(self.host.task_badge_controller().badges(self.context.task_badge_keys([0])[0]), [])
    def test_invalid_badge_does_not_partially_overwrite_existing_layer(self):
        self.publish()
        with self.assertRaises(ValueError): self.context.set_task_badges({'relative': TaskBadge('bad')})
        self.assertEqual(self.host.task_badge_controller().badges(self.context.task_badge_keys([0])[0])[0].text, '新成品 · 3')
    def test_worker_thread_cannot_touch_task_badges(self):
        errors = []
        def worker():
            try: self.context.set_task_badges({})
            except Exception as error: errors.append(error)
        thread = threading.Thread(target=worker); thread.start(); thread.join(2)
        self.assertTrue(errors); self.assertIsInstance(errors[0], RuntimeError)


class NoticeControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def test_batch_snapshot_emits_one_notification_without_focusing_window(self):
        parent = QtCore.QObject(); context = SimpleNamespace(set_task_badges=Mock(), notify=Mock(), log=Mock())
        parent.context = context; controller = ReceiptNotices(parent)
        controller.apply_snapshot({'items': [], 'notify_tasks': 3, 'notify_files': 8})
        context.notify.assert_called_once(); context.set_task_badges.assert_called_once_with({})
        parent.deleteLater()
    def test_late_local_snapshot_does_not_erase_newer_receipt_marker(self):
        parent = QtCore.QObject(); parent.context = SimpleNamespace(set_task_badges=Mock(), notify=Mock(), log=Mock())
        controller = ReceiptNotices(parent)
        controller.apply_snapshot({'items': [], 'notify_files': 0})
        generation = controller.generation
        parent.context.set_task_badges.reset_mock()
        controller.apply_snapshot({'items': [], 'notify_files': 0, 'ui_generation': generation - 1})
        parent.context.set_task_badges.assert_not_called(); self.assertTrue(controller.pending)
        parent.deleteLater()


if __name__ == '__main__': unittest.main()
