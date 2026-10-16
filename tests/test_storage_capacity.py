"""Synthetic volumes: no real VC mount, private files or large allocations."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from model.AppDataStorage import ApplicationStorage, data_file, data_io
from model.ProtectedStorage import VolumeSession, StorageCapacityError, StorageUnavailable, MIN_FREE, WARN_FREE
from model.MediaStorage import media_cache_root, media_temporary, require_media_output
from tests.test_protected_storage import Provider


class CapacityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name); self.volume=self.base/'volume'; self.volume.mkdir()
        self.root=self.volume/'AssistantToolData'; self.root.mkdir()
        provider=Provider(self.volume)
        self.guard=VolumeSession(provider,provider.volume,self.root); self.addCleanup(self.guard.close)
        self.storage=ApplicationStorage(self.root,guard=self.guard)
        self.free=10**10
        self.stack=[]
        for p in (patch('model.AppDataStorage._APPLICATION_STORAGE',self.storage),
                  patch('model.ProtectedStorage.shutil.disk_usage',side_effect=lambda _:SimpleNamespace(free=self.free))):
            p.start(); self.addCleanup(p.stop)

    def test_atomic_write_reserves_incoming_bytes_and_keeps_original(self):
        target=data_file(self.root/'config.json'); target.write_bytes(b'original')
        self.free=MIN_FREE+8
        with self.assertRaises(StorageCapacityError): target.write_bytes(b'x'*16)
        self.assertEqual(target.read_bytes(),b'original')
        self.assertEqual(list(self.root.glob('.data-*.tmp')),[])
        self.assertTrue(self.guard.available)

    def test_private_copy_preflight_and_external_bytes_not_charged_to_vc(self):
        source=self.base/'source.mp4'; source.write_bytes(b'x'*32)
        self.free=MIN_FREE+8
        with self.assertRaises(StorageCapacityError): data_io.copy2(source,self.root/'copy.mp4')
        self.assertFalse((self.root/'copy.mp4').exists())
        data_file(self.base/'external.mp4').write_bytes(b'x'*32)
        self.assertTrue((self.base/'external.mp4').exists())

    def test_low_space_still_allows_read_and_verified_cleanup(self):
        target=data_file(self.root/'cache.bin'); target.write_bytes(b'keep')
        directory=data_io.TemporaryDirectory(prefix='private-')
        folder=Path(directory.name); data_file(folder/'temp').write_bytes(b'temp')
        self.free=MIN_FREE-1
        with self.assertRaises(StorageCapacityError): target.write_bytes(b'new')
        self.assertEqual(target.read_bytes(),b'keep')
        target.remove(); directory.cleanup()
        self.assertFalse(target.path.exists()); self.assertFalse(folder.exists())
        self.assertTrue(self.guard.available)
        self.free=10**10; data_file(self.root/'recovered').write_bytes(b'ok')

    def test_media_cache_maps_only_private_data_root_to_external_task(self):
        task=self.base/'tasks'; task.mkdir()
        root=media_cache_root(self.root/'BatchTextVideo',anchors=(task,),namespace='batch-text-video')
        self.assertEqual(root,task/'.辅助工具媒体缓存'/'batch-text-video')
        self.assertFalse(root.is_relative_to(self.root))
        external=self.base/'old-cache'
        self.assertEqual(media_cache_root(external,anchors=(task,),namespace='batch-text-video'),external)
        with self.assertRaises(ValueError):
            media_cache_root(self.root/'cache',anchors=(self.root,),namespace='preview')
        with self.assertRaises(ValueError): require_media_output(self.root/'video.mp4')
        with self.assertRaises(ValueError): require_media_output(self.volume/'other-folder'/'video.mp4')

    def test_internal_media_cache_is_not_imported_or_organized(self):
        from model.TaskResultExporter import copytree_if_updated
        from app_plugins.builtin.smart_video_editor.engine import discover_task_videos
        task=self.base/'tasks'; task.mkdir()
        original=task/'original.mp4'; original.write_bytes(b'original')
        import os, time
        old=time.time()-10; os.utime(original,(old,old))
        for name in ('.辅助工具媒体缓存', '.整理上传临时'):
            folder=task/name/'nested'; folder.mkdir(parents=True)
            (folder/'temporary.mp4').write_bytes(b'internal')
        self.assertEqual(discover_task_videos(task),[original])
        outputs=copytree_if_updated(task,self.base/'result')
        self.assertEqual([p.name for p in outputs],['original.mp4'])
        self.assertFalse((self.base/'result'/'.辅助工具媒体缓存').exists())

    def test_fb_download_scratch_is_external_and_cleaned_per_job(self):
        from app_plugins.builtin.facebook_contact_sheet import batch
        from model.BackgroundTasks import TaskRunContext
        import threading
        task=self.base/'task'; task.mkdir(); seen=[]
        job=batch.BatchJob('task','https://www.facebook.com/reel/123/',str(task))
        def download(_url,path,**_kwargs):
            seen.append(Path(path)); self.assertFalse(Path(path).is_relative_to(self.root))
            Path(path).mkdir(parents=True)
            source=Path(path)/'source.mp4'; source.write_bytes(b'synthetic'); return source,'title'
        def render(_v,_d,_cuts,folder,**_kw):
            image=Path(folder)/'sheet.jpg'; image.write_bytes(b'jpg'); return [image]
        context=TaskRunContext(threading.Event(),lambda *_:None,lambda *_:None)
        with patch.object(batch,'download_facebook_video',side_effect=download), \
             patch.object(batch,'detect_cuts',return_value=(1,[])), \
             patch.object(batch,'render_contact_sheets',side_effect=render):
            result=batch.run_reference_job({'job':job,'settings':dict(browser='',profile='',sensitivity=5,min_scene_seconds=.8,max_interval=15)},context)
        self.assertFalse(result['cached']); self.assertFalse(seen[0].exists())
        self.assertTrue(Path(result['paths'][0]).exists())

    def test_qt_image_import_reserves_bytes_before_native_png_write(self):
        from qt_compat import QtGui, QtWidgets
        from app_plugins.builtin.batch_text_video.layers import import_images
        app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        image=QtGui.QImage(64,64,QtGui.QImage.Format.Format_ARGB32); image.fill(0xffabcdef)
        source=self.base/'source.png'; self.assertTrue(image.save(str(source),'PNG'))
        self.free=MIN_FREE+8
        result = import_images([source],self.root)
        self.assertEqual(result['layers'],[]); self.assertTrue(result['errors'])
        self.assertEqual(list((self.root/'layer-assets').glob('*.png')),[])

    def test_monitor_warns_early_and_cancels_once_without_invalidating_volume(self):
        from qt_compat import QtWidgets
        from PYUI.protected_storage_pyui import StorageMonitor
        app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window=QtWidgets.QWidget(); window.background_tasks=Mock()
        monitor=StorageMonitor(window,self.storage); monitor.timer.stop()
        def probe():
            monitor.poll()
            monitor.timer.stop()
            monitor.worker.join(2)
            self.assertFalse(monitor.worker.is_alive())
            monitor.poll();monitor.timer.stop()
        with patch.object(monitor,'show_space_notice') as notice:
            self.free=WARN_FREE-1; probe(); self.assertEqual(notice.call_count,1)
            self.free=MIN_FREE-1; probe(); probe()
            window.background_tasks.cancel_all.assert_called_once()
            self.assertTrue(self.guard.available)
            self.free=WARN_FREE+1; probe(); self.assertFalse(monitor.space_paused)
        window.deleteLater(); app.processEvents()

    def test_low_space_submission_uses_existing_ui_error_contract(self):
        from qt_compat import QtWidgets
        from model.BackgroundTasks import BackgroundTaskManager
        app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        manager=BackgroundTaskManager()
        self.free=MIN_FREE-1
        with self.assertRaisesRegex(RuntimeError,'加密卷空间不足'):
            manager.submit(owner='test',title='synthetic',runner=lambda *_:None)
        self.assertEqual(manager._queue,[]); self.assertTrue(self.guard.available)
        manager.shutdown(); manager.deleteLater(); app.processEvents()
