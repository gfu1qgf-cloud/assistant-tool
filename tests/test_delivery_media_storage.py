import tempfile
import os
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from model.AppDataStorage import ApplicationStorage
from model.TaskDeliveryBackground import upload_work_directory, require_snapshot_space, snapshot_upload_plan


class MediaStorageTests(unittest.TestCase):
    def test_protected_storage_keeps_media_snapshot_on_task_disk(self):
        from datetime import date
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); private=root/'private'; tasks=root/'tasks'
            private.mkdir(); tasks.mkdir(); result=tasks/'1015'/'result'; result.mkdir(parents=True)
            source=result/'one.mp4'; source.write_bytes(b'synthetic media')
            os.utime(source,(time.time()-30,time.time()-30))
            storage=ApplicationStorage(private,guard=SimpleNamespace(check=Mock()))
            work=upload_work_directory(tasks)
            with patch('model.AppDataStorage.application_storage',return_value=storage), \
                    patch('model.TaskDeliveryBackground.shutil.disk_usage',return_value=SimpleNamespace(free=10**9)):
                plan=snapshot_upload_plan([(result,[source],Path('.'))],{}, {},
                    {'upload_date':'2026-10-15','upload_slot':'01'}, {}, tasks,[date(2026,10,15)],work,True)
            copied=Path(plan['batches'][0][1][0])
            self.assertTrue(copied.is_relative_to(tasks/'.整理上传临时'))
            self.assertFalse(copied.is_relative_to(private))
            self.assertEqual(copied.read_bytes(),source.read_bytes())
            self.assertEqual(list(private.iterdir()),[])

    def test_low_target_disk_fails_before_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'video.mp4'; source.write_bytes(b'original')
            work=upload_work_directory(root)
            with patch('model.TaskDeliveryBackground.shutil.disk_usage',return_value=SimpleNamespace(free=1)):
                with self.assertRaisesRegex(OSError,'任务磁盘空间不足'):
                    require_snapshot_space(work,[source])
            self.assertFalse(work.exists())
            self.assertEqual(source.read_bytes(),b'original')


class FacebookOpenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def plugin(self,values):
        from app_plugins.builtin.facebook_contact_sheet.plugin import FacebookContactSheetPlugin
        plugin=FacebookContactSheetPlugin()
        plugin.context=SimpleNamespace(parent_widget=None,task_targets=lambda rows:[
            {'task':SimpleNamespace(task_reference_link=value)} for value in values])
        return plugin

    def test_open_original_link_not_contact_sheet_or_download(self):
        plugin=self.plugin(['https://www.facebook.com/reel/123/'])
        with patch('qt_compat.QtGui.QDesktopServices.openUrl',return_value=True) as opened, \
                patch('model.OfflineMode.require_online'):
            self.assertEqual(plugin.open_task_reference([0]),'https://www.facebook.com/reel/123/')
        self.assertEqual(opened.call_args.args[0].toString(),'https://www.facebook.com/reel/123/')

    def test_no_reference_and_multiple_choice_cancellation(self):
        with patch('qt_compat.QtWidgets.QMessageBox.information') as info, \
                patch('qt_compat.QtGui.QDesktopServices.openUrl') as opened:
            self.assertIsNone(self.plugin(['no link']).open_task_reference([0]))
            info.assert_called_once(); opened.assert_not_called()
        with patch('qt_compat.QtWidgets.QInputDialog.getItem',return_value=('',False)), \
                patch('qt_compat.QtGui.QDesktopServices.openUrl') as opened:
            self.assertIsNone(self.plugin(['https://fb.watch/a/','https://www.facebook.com/reel/123/']).open_task_reference([0]))
            opened.assert_not_called()
