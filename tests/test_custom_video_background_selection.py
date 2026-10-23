"""Browsing an overlay pool must not silently become a background edit."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.custom_pools import create_pool, add_pool_paths
from app_plugins.builtin.batch_text_video.pools import new_copy, apply_pairs, pairing_plan
from app_plugins.builtin.batch_text_video.pool_ui import PairingDialog
from app_plugins.builtin.batch_text_video.store import fresh_state, Store
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class CustomVideoBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = fresh_state()
        self.state['backgrounds'] = [self.file('scene.mp4'), self.file('other-scene.mp4')]
        self.state['copy_pool'] = [{**new_copy(), 'title': '标题', 'body': '正文'}]
        self.pool = create_pool(self.state, '绿幕人物', 'video')
        add_pool_paths(self.pool, [self.file('green-person.mp4'), self.file('other-person.mp4')])
        apply_pairs(self.state, pairing_plan(self.state['copy_pool'], self.state['backgrounds'][:1]))
        self.store = Store(self.root/'store'); self.store.save(self.state, 0)
        self.background_patch = patch.object(BatchTextVideoDialog, 'request_background')
        self.request = self.background_patch.start(); self.addCleanup(self.background_patch.stop)
        self.overlay_patch = patch.object(BatchTextVideoDialog, '_start_overlay')
        self.overlay_patch.start(); self.addCleanup(self.overlay_patch.stop)
        self.dialog = BatchTextVideoDialog(store=self.store)
        self.addCleanup(self.close)

    def file(self, name):
        path = self.root/name; path.write_bytes(b'test-fixture'); return str(path)

    def close(self):
        self.dialog.close(); self.dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def browse(self):
        dialog = self.dialog
        dialog.tabs.setCurrentWidget(dialog.resources)
        dialog.resources.setCurrentWidget(dialog.custom_pages[self.pool['id']])
        before = (copy.deepcopy(dialog.active_entry()), dialog.current_id, dialog.current_copy_id,
                  dialog.preview_video, dialog.title.toPlainText(), dialog.body.toPlainText(), self.store.path.read_bytes())
        self.request.reset_mock()
        table = dialog.custom_tables[self.pool['id']]
        table.setCurrentCell(0, 0); table.setCurrentCell(1, 0)
        after = (dialog.active_entry(), dialog.current_id, dialog.current_copy_id,
                 dialog.preview_video, dialog.title.toPlainText(), dialog.body.toPlainText(), self.store.path.read_bytes())
        self.assertEqual(after, before); self.request.assert_not_called()
        self.assertEqual(table.identifiers(), [self.pool['items'][1]['id']])

    def test_browsing_video_pool_keeps_selected_copy_text_and_background(self):
        self.dialog.copy_pool.setCurrentCell(0, 0)
        self.dialog.video_pool.setCurrentCell(0, 0)
        self.assertEqual(self.dialog.active_entry()['path'], self.state['backgrounds'][0])
        self.browse()

    def test_browsing_video_pool_keeps_selected_queue_job_and_background(self):
        self.dialog.jobs.setCurrentCell(0, 0); self.dialog._selection_changed(0)
        self.assertTrue(self.dialog.current_id)
        self.browse()

    def test_right_click_and_refresh_of_custom_pool_do_not_change_background(self):
        dialog = self.dialog
        dialog.copy_pool.setCurrentCell(0, 0)
        before = copy.deepcopy(dialog.active_entry()); self.request.reset_mock()
        table = dialog.custom_tables[self.pool['id']]
        with patch.object(QtWidgets.QMenu, 'exec', return_value=None):
            dialog.resource_menu('pool:'+self.pool['id'], table.visualItemRect(table.item(1, 0)).center())
        dialog.refresh_pools()
        self.assertEqual(dialog.active_entry(), before); self.request.assert_not_called()

    def test_old_custom_video_override_is_ignored_for_copy_preview(self):
        dialog = self.dialog
        dialog.preview_video = self.pool['items'][0]['path']
        self.assertEqual(dialog.copy_preview_path({'path': self.state['backgrounds'][1]}), self.state['backgrounds'][1])
        self.assertEqual(dialog.copy_preview_path({}), self.state['backgrounds'][0])

    def test_dedicated_background_pool_still_changes_copy_preview(self):
        dialog = self.dialog
        dialog.copy_pool.setCurrentCell(0, 0)
        self.request.reset_mock(); dialog.video_pool.setCurrentCell(1, 0)
        self.assertEqual(dialog.active_entry()['path'], self.state['backgrounds'][1])
        self.request.assert_called_with(self.state['backgrounds'][1])
        self.assertEqual(dialog.title.toPlainText(), '标题')

    def test_explicit_custom_background_pairing_remains_available(self):
        picker = PairingDialog(self.state['copy_pool'], self.state['backgrounds'], state=self.state, selected_pool=self.pool['id'])
        self.addCleanup(picker.deleteLater)
        self.assertEqual(picker.video_source.currentData(), self.pool['id'])
        expected = {self.pool['items'][0]['path']}
        self.assertEqual({p['path'] for p in picker.pairs}, expected)
        state = copy.deepcopy(self.state); state['jobs'] = []
        apply_pairs(state, picker.pairs)
        self.assertEqual({j['path'] for j in state['jobs']}, expected)
