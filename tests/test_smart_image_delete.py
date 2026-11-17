import ctypes
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image
from qt_compat import QtCore, QtWidgets, QtGui
from model.AppDataStorage import data_io
from model.image_retrieval.index import ImageSearchIndex, discover_external_groups, DELETED_IMAGES_FOLDER
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
        self.retained = self.library / DELETED_IMAGES_FOLDER

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

    def select(self, *positions):
        paths = []
        for position in positions:
            item = self.dialog.results.item(position)
            item.setSelected(True)
            paths.append(Path(item.data(QtCore.Qt.ItemDataRole.UserRole)))
        return paths

    def confirm_move(self):
        with patch(MODULE + 'ui.QtWidgets.QMessageBox.exec',
                   return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            self.dialog._delete_selected()
            self.wait_workers()

    def real_confirmation(self, permanent):
        observed = []
        def choose():
            box = QtWidgets.QApplication.activeModalWidget()
            if not isinstance(box, QtWidgets.QMessageBox):
                observed.append('missing confirmation')
                return
            observed.append(box.informativeText())
            label = '永久删除（不可恢复）' if permanent else '移入保留文件夹（可恢复）'
            choices = [button for button in box.buttons() if button.text() == label]
            preview = os.environ.get('LZX_DELETE_CONFIRM_PREVIEW')
            if preview:
                box.grab().save(preview)
            if len(choices) == 1:
                choices[0].click()
            else:
                box.reject()
        QtCore.QTimer.singleShot(0, choose)
        self.dialog._delete_selected()
        self.wait_workers()
        self.assertEqual(len(observed), 1)
        self.assertIn('无法撤销', observed[0])
        self.assertIn(DELETED_IMAGES_FOLDER, observed[0])

    def test_real_confirmation_moves_selected_in_qthread_and_updates_pagination(self):
        removed = self.select(0, 1)
        calls = len(self.encoder.calls)
        self.real_confirmation(False)
        self.assertTrue(all(not path.exists() for path in removed))
        for path in removed:
            retained = list(self.retained.rglob(path.name))
            self.assertEqual(len(retained), 1)
            with Image.open(retained[0]) as image:
                self.assertEqual(image.size, (8, 8))
        self.assertEqual(sum(path.exists() for path in self.paths), 21)
        self.assertEqual(self.index.count(), 21)
        self.assertEqual(len(self.index.search(self.encoder.model_id, [1, 0], None)), 21)
        self.assertEqual(len(self.dialog._search_rows), 21)
        self.assertEqual(self.dialog.results.count(), 18)
        self.dialog._append_page()
        self.assertEqual(self.dialog.results.count(), 21)
        self.assertEqual(len(self.encoder.calls), calls)

    def test_real_confirmation_permanently_deletes_selected_in_qthread(self):
        removed = self.select(0, 1)
        self.real_confirmation(True)
        self.assertTrue(all(not path.exists() for path in removed))
        self.assertFalse(self.retained.exists())
        self.assertEqual(sum(path.exists() for path in self.paths), 21)
        self.assertEqual(self.index.count(), 21)
        self.assertEqual(len(self.dialog._search_rows), 21)
        self.assertIn('已永久删除 2 张图片', self.dialog.status.text())

    def test_confirmation_defaults_to_cancel_and_changes_nothing(self):
        self.select(0)
        def decline(box):
            self.assertEqual(box.defaultButton(), box.button(QtWidgets.QMessageBox.StandardButton.No))
            self.assertIn('image-', box.detailedText())
            return QtWidgets.QMessageBox.StandardButton.No
        with patch(MODULE + 'ui.QtWidgets.QMessageBox.exec', new=decline):
            self.dialog._delete_selected()
        self.assertEqual(self.index.count(), 23)
        self.assertTrue(all(path.exists() for path in self.paths))
        self.assertFalse(self.retained.exists())

    def test_partial_move_failure_preserves_source_and_index(self):
        paths = self.select(0, 1)
        rename = data_io.rename
        def partial(source, target):
            if Path(source) == paths[0]:
                raise PermissionError('无法写入保留文件夹')
            return rename(source, target)
        with patch(MODULE + 'image_delete.data_io.rename', side_effect=partial):
            self.confirm_move()
        self.assertTrue(paths[0].is_file())
        self.assertFalse(paths[1].exists())
        self.assertEqual(self.index.count(), 22)
        self.assertEqual(len(self.dialog._search_rows), 22)
        self.assertIn('1 张删除失败', self.dialog.status.text())

    def test_index_failure_removes_moved_result_and_reports_repair(self):
        self.select(0)
        with patch.object(self.index, 'remove_paths', side_effect=OSError('索引只读')):
            self.confirm_move()
        self.assertEqual(len(self.dialog._search_rows), 22)
        self.assertIn('索引更新失败', self.dialog.status.text())

    def test_both_modes_can_stop_after_first_file(self):
        for permanent, paths in ((False, self.paths[:2]), (True, self.paths[2:4])):
            with self.subTest(permanent=permanent):
                result = trash_selected([str(path) for path in paths], [self.library], Mock(),
                                        lambda: not paths[0].exists(), permanent=permanent)
                self.assertEqual(result['deleted'], [str(paths[0])])
                self.assertTrue(result['cancelled'])
                self.assertTrue(paths[1].is_file())

    def test_outside_library_directory_missing_and_nonimage_are_preserved(self):
        outside = self.root / 'outside.png'
        Image.new('RGB', (8, 8), 'red').save(outside)
        document = self.library / 'keep.txt'
        document.write_text('keep')
        paths = [outside, self.library, self.library / 'missing.png', document]
        for permanent in (False, True):
            result = trash_selected([str(path) for path in paths], [self.library], Mock(),
                                    lambda: False, permanent=permanent)
            self.assertEqual(len(result['failures']), 4)
            self.assertEqual(result['deleted'], [])
        self.assertTrue(outside.is_file())
        self.assertTrue(document.is_file())

    def test_locked_volume_prevents_deletion(self):
        storage = Mock()
        storage.check.side_effect = OSError('密盘不可用')
        with patch(MODULE + 'image_delete.application_storage', return_value=storage):
            with self.assertRaisesRegex(OSError, '密盘不可用'):
                trash_selected([str(self.paths[0])], [self.library], Mock(), lambda: False)
        self.assertTrue(self.paths[0].is_file())

    def test_busy_and_empty_selection_do_not_prompt_or_delete(self):
        with patch(MODULE + 'ui.QtWidgets.QMessageBox.exec') as confirm:
            self.dialog._delete_selected()
            self.select(0)
            with patch.object(self.dialog, 'is_busy', return_value=True):
                self.dialog._delete_selected()
        confirm.assert_not_called()
        self.assertEqual(self.index.count(), 23)

    def test_real_windows_locked_file_is_preserved_in_both_modes(self):
        if sys.platform != 'win32':
            self.skipTest('Windows file sharing semantics')
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        locked = self.paths[0]
        handle = api.CreateFileW(str(locked), 0x80000000, 1, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            for permanent, other in ((False, self.paths[1]), (True, self.paths[2])):
                result = trash_selected([str(locked), str(other)], [self.library],
                                        Mock(), lambda: False, permanent=permanent)
                self.assertTrue(locked.is_file())
                self.assertFalse(other.exists())
                self.assertEqual(result['deleted'], [str(other)])
                self.assertEqual(len(result['failures']), 1)
        finally:
            api.CloseHandle(handle)

    def test_retained_images_are_excluded_and_manual_restore_can_be_reindexed(self):
        source = self.paths[0]
        result = trash_selected([str(source)], [self.library], Mock(), lambda: False)
        self.index.remove_paths(result['deleted'])
        retained = Path(result['moved'][0]['target'])
        self.assertTrue(retained.is_file())
        groups = discover_external_groups([self.library, self.retained])
        scanned = [Path(image['path']) for group in groups for image in group['images']]
        self.assertEqual(len(scanned), 22)
        self.assertNotIn(retained, scanned)
        calls = len(self.encoder.calls)
        self.index.sync(groups, self.encoder)
        self.assertEqual(len(self.encoder.calls), calls)
        self.index.sync(existing.group(retained, kind='folder'), self.encoder, prune_missing=False)
        self.assertEqual(len(self.encoder.calls), calls)
        retained.rename(source)
        self.index.sync(discover_external_groups([self.library]), self.encoder)
        self.assertEqual(self.index.count(), 23)
        self.assertEqual(len(self.encoder.calls), calls + 1)

    def test_multiple_libraries_and_same_names_preserve_relative_paths(self):
        other = self.root / 'other-library'
        sources = [self.library / 'A' / 'same.png', self.library / 'B' / 'same.png', other / 'same.png']
        for source, color in zip(sources, ('red', 'blue', 'green')):
            source.parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (8, 8), color).save(source)
        expected = [source.read_bytes() for source in sources]
        result = trash_selected([str(source) for source in sources], [self.library, other], Mock(), lambda: False)
        self.assertEqual(len(result['moved']), 3)
        self.assertEqual(result['failures'], [])
        targets = [Path(item['target']) for item in result['moved']]
        self.assertEqual(len(set(targets)), 3)
        self.assertEqual([target.read_bytes() for target in targets], expected)
        self.assertTrue(targets[0].is_relative_to(self.retained))
        self.assertTrue(targets[2].is_relative_to(other / DELETED_IMAGES_FOLDER))

    def test_move_failure_never_falls_back_to_permanent_deletion(self):
        with patch(MODULE + 'image_delete.data_io.rename', side_effect=OSError('移动失败')):
            result = trash_selected([str(self.paths[0])], [self.library], Mock(), lambda: False)
        self.assertFalse(result['permanent'])
        self.assertEqual(result['deleted'], [])
        self.assertTrue(self.paths[0].is_file())

    def test_existing_retained_file_is_never_overwritten(self):
        source = self.paths[0]
        with patch(MODULE + 'image_delete.uuid.uuid4', return_value=Mock(hex='same-batch-id')):
            first = trash_selected([str(source)], [self.library], Mock(), lambda: False)
            target = Path(first['moved'][0]['target'])
            expected = target.read_bytes()
            Image.new('RGB', (8, 8), 'blue').save(source)
            second = trash_selected([str(source)], [self.library], Mock(), lambda: False)
        self.assertEqual(second['deleted'], [])
        self.assertEqual(len(second['failures']), 1)
        self.assertTrue(source.is_file())
        self.assertEqual(target.read_bytes(), expected)

    def test_open_deleted_folder_points_to_the_current_library(self):
        self.select(0)
        self.confirm_move()
        with patch.object(QtGui.QDesktopServices, 'openUrl', return_value=True) as open_url:
            self.dialog._open_deleted_folder()
        self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), self.retained)
