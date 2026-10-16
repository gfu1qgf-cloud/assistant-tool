import os
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image
from qt_compat import QtCore, QtGui, QtWidgets
from model.AppDataStorage import ApplicationStorage
from model.BackgroundTasks import BackgroundTaskManager
from model.GoogleDriveBrowser import save_snapshot, snapshot_key, read_bookmarks
from model.DrivePreviewCache import DrivePreviewCache
from app_plugins.builtin.google_drive_browser.ui import DriveBrowserDialog
from app_plugins.builtin.google_drive_browser.plugin import GoogleDriveBrowserPlugin


class Guard:
    def __init__(self, root): self.volume = type('Volume', (), {'root': root})(); self.available = True
    def check(self, *_args, **_kwargs):
        if not self.available: raise OSError('mounted data volume lost')


class Context:
    def __init__(self, manager): self.background_tasks, self.commands = manager, []
    def register_command(self, command): self.commands.append(command)
    def submit_background_task(self, **kwargs): return self.background_tasks.submit(owner='google_drive_browser', **kwargs)


class DriveBrowserUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.storage = ApplicationStorage(self.root/'private', guard=Guard(self.root/'private'))
        self.binding = patch('model.AppDataStorage._APPLICATION_STORAGE', self.storage); self.binding.start()
        self.manager = BackgroundTaskManager(); self.context = Context(self.manager)
        self.dialog = None
    def tearDown(self):
        if self.dialog: self.dialog.shutdown()
        self.manager.cancel_all(); self.wait_until(lambda: not self.manager.is_busy()); self.manager.shutdown(); self.manager.deleteLater()
        if self.dialog: self.dialog.deleteLater()
        self.app.processEvents(); self.binding.stop(); self.temp.cleanup()
    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline: self.app.processEvents(); time.sleep(.003)
        self.app.processEvents(); self.assertTrue(predicate())
    def seed(self, rows=None):
        value = {'kind':'list','folder':{'id':'root'},'items':rows or [],'next':'','account':'account','key':snapshot_key({'id':'root'})}
        save_snapshot(value['key'], value); return value
    def open(self): self.dialog = DriveBrowserDialog(self.context); self.dialog.cache_root = str(self.root/'cache'); return self.dialog

    def test_open_cached_gallery_is_offline_and_uncropped(self):
        thumbnail = self.root/'wide.jpg'; image = Image.new('RGB',(960,320),'white')
        image.paste('red',(0,0,90,320)); image.paste('blue',(870,0,960,320)); image.save(thumbnail)
        self.seed([{'id':'x','name':'wide.png','mimeType':'image/png','size':'2000','_thumbnail':str(thumbnail)}])
        with patch('model.GoogleDriveBrowser.load_credentials', side_effect=AssertionError('offline must not login')):
            dialog = self.open(); dialog.show(); self.app.processEvents()
            self.assertEqual(dialog.gallery.count(),1); self.assertFalse(self.manager.is_busy())
            pixels = dialog.gallery.item(0).icon().pixmap(170,170).toImage()
            self.assertGreater(pixels.pixelColor(2,85).red(),200); self.assertGreater(pixels.pixelColor(168,85).blue(),200)
            dialog.gallery.setCurrentRow(0); self.assertEqual(dialog.image.picture.pixmap().size(),QtCore.QSize(960,320))
            self.assertEqual(dialog.views.currentWidget(),dialog.image)
            self.assertIn('离线目录缓存',dialog.status.text())

    def test_bookmark_add_rename_remove_only_local_private_records(self):
        dialog = self.open()
        url = 'https://drive.google.com/drive/folders/test_folder?resourcekey=key'
        with patch.object(QtWidgets.QInputDialog,'getText',side_effect=[(url,True),('初始名称',True)]): dialog.add_bookmark()
        self.assertEqual(dialog.bookmarks.count(),1); dialog.bookmarks.setCurrentRow(0)
        with patch.object(QtWidgets.QInputDialog,'getText',return_value=('新名称',True)): dialog.rename_bookmark()
        self.assertEqual(read_bookmarks()[0]['name'],'新名称'); dialog.bookmarks.setCurrentRow(0); dialog.remove_bookmark()
        self.assertFalse(read_bookmarks()); self.assertFalse(self.manager.is_busy())

    def test_stale_result_does_not_replace_new_folder(self):
        original = self.seed(); dialog = self.open(); dialog.generation = 5
        stale = {**original,'folder':{'id':'different'},'items':[{'id':'x','name':'bad','mimeType':'image/png'}]}
        dialog.completed(4,stale); self.assertEqual(dialog.current['id'],'root'); self.assertEqual(dialog.gallery.count(),0)

    def test_cancel_queue_and_close_during_host_job_is_safe(self):
        entered, release = threading.Event(), threading.Event()
        def job(_payload, run):
            entered.set()
            while not release.wait(.01): run.check_canceled()
            return {'kind':'authorize'}
        dialog = self.open()
        with patch('app_plugins.builtin.google_drive_browser.ui.run_drive_job',job): dialog.submit('authorize')
        self.wait_until(entered.is_set); handle = dialog.handle
        dialog.close(); self.assertTrue(handle.isRunning(), 'closing view does not own the host job')
        dialog.cancel_job(); release.set(); self.wait_until(lambda:not self.manager.is_busy())
        self.assertEqual(self.manager.get(handle.task_id)['status'],'canceled')

    def test_new_request_queues_without_blocking_gui_and_drops_cancelled_result(self):
        calls = []
        def job(payload, run):
            calls.append(payload['operation'])
            for _ in range(5): time.sleep(.01); run.check_canceled()
            return {'kind':payload['operation']}
        dialog = self.open()
        with patch('app_plugins.builtin.google_drive_browser.ui.run_drive_job',job):
            dialog.submit('authorize'); dialog.submit('clear_cache')
        self.wait_until(lambda:not self.manager.is_busy())
        self.assertIn('clear_cache',calls); self.assertIn('已清理',dialog.status.text())

    def test_preview_settings_and_empty_actions_no_slot_exceptions(self):
        dialog = self.open(); dialog.show(); self.app.processEvents()
        dialog.open_selected(); dialog.go_back(); dialog.load_more(); dialog.prepare_thumbnails(); dialog.rename_bookmark(); dialog.remove_bookmark()
        dialog.save_media(); self.assertIn('先打开',dialog.status.text())
        self.assertIsNone(dialog.player, 'decoder is lazy until explicit video preview')
        self.storage.guard.available = False; dialog.open_target({'id':'root'})
        self.assertIn('volume lost',dialog.status.text())

    def test_video_full_download_is_explicit_and_declining_does_not_submit(self):
        self.seed([{'id':'v','name':'large.mp4','mimeType':'video/mp4','size':'1000000000'}])
        dialog = self.open(); dialog.gallery.setCurrentRow(0)
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.No) as ask:
            dialog.open_selected()
        ask.assert_called_once(); self.assertFalse(self.manager.is_busy())

    def test_icon_size_can_change_without_cropping(self):
        self.seed([{'id':'v','name':'a.mp4','mimeType':'video/mp4','size':'100'}]); dialog = self.open()
        dialog.thumb_size.setCurrentIndex(2); dialog.resize_thumbnails()
        self.assertEqual(dialog.gallery.iconSize().width(),260)

    def test_real_host_menu_and_plugin_management_registration(self):
        from app_plugins.host import PluginHost
        class Window(QtWidgets.QWidget):
            def load_config(self): return {}
            def appendLog(self,*args,**kwargs): pass
        window=Window(); host=PluginHost(window); plugin=host.install(GoogleDriveBrowserPlugin())
        self.assertIn('google_drive_browser',{entry['plugin_id'] for entry in host.plugin_entries()})
        self.assertIsNone(plugin.dialog); window.deleteLater()

    def test_plugin_registration_is_lazy_and_manageable(self):
        plugin = GoogleDriveBrowserPlugin(); plugin.register(self.context)
        self.assertEqual(plugin.plugin_id,'google_drive_browser'); self.assertEqual(len(self.context.commands),1)
        self.assertEqual(self.context.commands[0].command_id,'open'); self.assertIsNone(plugin.dialog)
        self.assertTrue(plugin.can_close()[0]); plugin.stop()
