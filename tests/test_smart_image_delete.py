import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from qt_compat import QtCore, QtWidgets
from model.image_retrieval.index import ImageSearchIndex
from app_plugins.builtin.smart_image_search.image_delete import trash_selected
from app_plugins.builtin.smart_image_search.ui import SmartImageSearchDialog
import test_smart_image_search as existing


MODULE = 'app_plugins.builtin.smart_image_search.'


class ImageDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.paths = [self.library / f'image-{i}.png' for i in range(23)]
        for path in self.paths:
            Image.new('RGB', (8, 8), 'red').save(path)
        self.encoder = existing.FakeEncoder()
        self.index = ImageSearchIndex(self.root / 'index')
        self.index.sync(existing.group(*self.paths, kind='folder'), self.encoder)
        self.dialog = SmartImageSearchDialog(
            {'library_roots': [str(self.library)], 'result_limit': 20},
            index=self.index, encoder=self.encoder, store=object())
        self.addCleanup(self.close_dialog)
        self.wait_workers()
        self.dialog._show_results(self.index.search(self.encoder.model_id, [1, 0], None))
        self.trash = self.root / 'fake-trash'
        self.trash.mkdir()

    def wait_workers(self):
        deadline = time.monotonic() + 5
        while ((self.dialog.worker is not None or self.dialog._count_worker is not None)
               and time.monotonic() < deadline):
            self.app.processEvents()
            time.sleep(.002)
        self.assertIsNone(self.dialog.worker)
        self.assertIsNone(self.dialog._count_worker)

    def close_dialog(self):
        self.dialog.close()
        self.wait_workers()

    def fake_file(self, raw):
        file = Mock()
        def recycle():
            Path(raw).rename(self.trash / Path(raw).name)
            return True
        file.moveToTrash.side_effect = recycle
        return file

    def confirm_delete(self):
        with patch(MODULE + 'ui.QtWidgets.QMessageBox.exec',
                   return_value=QtWidgets.QMessageBox.StandardButton.Yes), \
             patch(MODULE + 'image_delete.QtCore.QFile', side_effect=self.fake_file):
            self.dialog._delete_selected()
            self.wait_workers()

    def test_multiselect_deletes_only_selected_and_pagination_stays_consistent(self):
        selected = [self.dialog.results.item(i) for i in (0, 1)]
        paths = [Path(item.data(QtCore.Qt.ItemDataRole.UserRole)) for item in selected]
        for item in selected:
            item.setSelected(True)
        calls = len(self.encoder.calls)
        self.confirm_delete()
        self.assertEqual(self.dialog.results.count(), 18)
        self.assertEqual(len(self.dialog._search_rows), 21)
        self.assertEqual(self.index.count(), 21)
        self.assertIn('2 张图片移入回收站', self.dialog.status.text())
        for path in paths:
            self.assertFalse(path.exists())
            self.assertTrue((self.trash / path.name).is_file())
        self.dialog._append_page()
        self.assertEqual(self.dialog.results.count(), 21)
        self.assertEqual(len(self.encoder.calls), calls)
        self.assertEqual(len(self.index.search(self.encoder.model_id, [1, 0], None)), 21)

    def test_confirmation_defaults_to_no_and_cancel_changes_nothing(self):
        self.dialog.results.item(0).setSelected(True)
        def decline(box):
            self.assertEqual(box.defaultButton(), box.button(QtWidgets.QMessageBox.StandardButton.No))
            self.assertIn('image-', box.detailedText())
            return QtWidgets.QMessageBox.StandardButton.No
        with patch(MODULE + 'ui.QtWidgets.QMessageBox.exec', new=decline), \
             patch(MODULE + 'image_delete.QtCore.QFile') as file:
            self.dialog._delete_selected()
        file.assert_not_called()
        self.assertEqual(self.index.count(), 23)
        self.assertTrue(all(path.exists() for path in self.paths))

    def test_failed_recycle_keeps_file_and_index_while_other_selection_succeeds(self):
        items = [self.dialog.results.item(i) for i in (0, 1)]
        failed = str(items[0].data(QtCore.Qt.ItemDataRole.UserRole))
        for item in items:
            item.setSelected(True)
        recycle = self.fake_file
        def partial(raw):
            if raw == failed:
                return Mock(moveToTrash=Mock(return_value=False), errorString=Mock(return_value='文件被占用'))
            return recycle(raw)
        with patch.object(self, 'fake_file', side_effect=partial):
            self.confirm_delete()
        self.assertTrue(Path(failed).is_file())
        self.assertEqual(self.index.count(), 22)
        self.assertEqual(len(self.dialog._search_rows), 22)
        self.assertIn('1 张删除失败', self.dialog.status.text())

    def test_index_failure_still_removes_deleted_result_and_reports_repair(self):
        self.dialog.results.item(0).setSelected(True)
        with patch.object(self.index, 'remove_paths', side_effect=OSError('索引只读')):
            self.confirm_delete()
        self.assertEqual(len(self.dialog._search_rows), 22)
        self.assertIn('索引更新失败', self.dialog.status.text())

    def test_cancel_between_files_preserves_remaining_files(self):
        paths = [str(path) for path in self.paths[:2]]
        with patch(MODULE + 'image_delete.QtCore.QFile', side_effect=self.fake_file):
            result = trash_selected(paths, [self.library], Mock(),
                                    lambda: not self.paths[0].exists())
        self.assertEqual(result['deleted'], paths[:1])
        self.assertTrue(result['cancelled'])
        self.assertTrue(self.paths[1].is_file())

    def test_outside_library_directory_missing_and_nonimage_never_reach_trash(self):
        outside = self.root / 'outside.png'
        Image.new('RGB', (8, 8), 'red').save(outside)
        document = self.library / 'keep.txt'
        document.write_text('keep')
        paths = [outside, self.library, self.library / 'missing.png', document]
        with patch(MODULE + 'image_delete.QtCore.QFile') as file:
            result = trash_selected([str(path) for path in paths], [self.library], Mock(), lambda: False)
        file.assert_not_called()
        self.assertEqual(len(result['failures']), 4)
        self.assertEqual(result['deleted'], [])
        self.assertTrue(outside.is_file())
        self.assertTrue(document.is_file())

    def test_locked_volume_prevents_deletion(self):
        storage = Mock()
        storage.check.side_effect = OSError('密盘不可用')
        with patch(MODULE + 'image_delete.application_storage', return_value=storage), \
             patch(MODULE + 'image_delete.QtCore.QFile') as file:
            with self.assertRaisesRegex(OSError, '密盘不可用'):
                trash_selected([str(self.paths[0])], [self.library], Mock(), lambda: False)
        file.assert_not_called()
        self.assertTrue(self.paths[0].is_file())

    def test_busy_and_empty_selection_do_not_prompt_or_delete(self):
        with patch(MODULE + 'ui.QtWidgets.QMessageBox.exec') as confirm:
            self.dialog._delete_selected()
            self.dialog.results.item(0).setSelected(True)
            with patch.object(self.dialog, 'is_busy', return_value=True):
                self.dialog._delete_selected()
        confirm.assert_not_called()
        self.assertEqual(self.index.count(), 23)
