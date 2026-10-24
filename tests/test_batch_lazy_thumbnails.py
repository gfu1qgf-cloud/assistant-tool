"""Visibility and bounded background I/O, including old private thumbnail caches."""
import os
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import thumbnails as thumbs
from app_plugins.builtin.batch_text_video.pool_ui import ResourceTable
from app_plugins.builtin.batch_text_video.custom_pool_ui import ImagePoolBindingDialog
from app_plugins.builtin.batch_text_video.layers import import_images
from app_plugins.builtin.batch_text_video.store import fresh_state


class LazyThumbnailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def wait(self, condition, seconds=5):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition(): return
            time.sleep(.005)
        self.fail('Background thumbnails did not settle')

    def picture(self, path, color='red'):
        image = QtGui.QImage(64, 48, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor(color)); self.assertTrue(image.save(str(path))); return image

    def test_thousand_hidden_rows_do_not_decode_then_only_visible_rows_are_requested(self):
        table = ResourceTable(['图片']); table.resize(350, 220)
        table.verticalHeader().setDefaultSectionSize(32)
        loaded, threads = [], []
        def decode(payload, cancel):
            loaded.append(payload[0]); threads.append(threading.get_ident())
            image = QtGui.QImage(48, 48, QtGui.QImage.Format.Format_RGB32); image.fill(QtGui.QColor('red'))
            return image
        with patch.object(thumbs, 'decode', side_effect=decode):
            table.populate([(str(i), [f'图片{i}'], '', (f'visible-test-{i}.png', '')) for i in range(1000)])
            for _ in range(20): self.app.processEvents()
            self.assertEqual(loaded, [])
            table.show()
            self.wait(lambda: loaded and not thumbs.service().running)
            first = set(loaded)
            self.assertLess(len(first), 12); self.assertNotIn('visible-test-999.png', first)
            self.assertTrue(all(t != threading.get_ident() for t in threads))
            # Very rapid scrolling discards the old wanted set before debounce.
            for row in (200, 400, 700, 999): table.scrollToItem(table.item(row, 0))
            self.wait(lambda: 'visible-test-999.png' in loaded and not thumbs.service().running)
            self.assertLess(len(set(loaded)), 24)
            self.assertNotIn('visible-test-400.png', loaded)
            self.assertTrue(table.item(999, 0).icon().isNull(), 'Model must not retain every decoded icon')
            table.hide(); self.app.processEvents(); table.deleteLater(); self.app.processEvents()

    def test_hidden_pool_combo_loads_no_images_until_it_or_popup_is_visible(self):
        state = fresh_state()
        state['image_pool'] = [dict(id=str(i), path=f'combo-test-{i}.png', thumbnail=f'legacy-{i}.png') for i in range(1000)]
        layer = dict(source='fixed', path='combo-test-700.png', pool_id='')
        loaded = []
        def decode(payload, cancel):
            loaded.append(payload[0]); image = QtGui.QImage(20, 20, QtGui.QImage.Format.Format_RGB32); image.fill(0); return image
        with patch.object(thumbs, 'decode', side_effect=decode):
            dialog = ImagePoolBindingDialog(state, layer)
            for _ in range(20): self.app.processEvents()
            self.assertEqual(loaded, [])
            dialog.show(); self.wait(lambda: 'combo-test-700.png' in loaded and not thumbs.service().running)
            self.assertLessEqual(len(set(loaded)), 1)
            dialog.image.showPopup()
            view = dialog.image.view()
            self.assertTrue(view.isVisible(), 'Popup view must be shown')
            self.wait(lambda: len(set(loaded)) > 1 and not thumbs.service().running)
            self.assertLess(len(set(loaded)), 50)
            dialog.image.hidePopup(); dialog.close(); dialog.deleteLater(); self.app.processEvents()

    def test_legacy_thumbnail_is_rebuilt_outside_private_data_only_on_demand(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            root = Path(directory); private = root/'private'; private.mkdir()
            source = private/'asset.png'; legacy = private/'asset.thumb.png'
            self.picture(source); self.picture(legacy, 'blue')
            before = source.read_bytes(), legacy.read_bytes()
            cache = thumbs.cache_root('thumbnails'); self.assertFalse(cache.exists())
            with patch('app_plugins.builtin.batch_text_video.layers.read_image', side_effect=AssertionError('Use old small thumbnail once')):
                result = thumbs.decode((str(source), str(legacy)), threading.Event())
            self.assertGreater(result.pixelColor(0, 0).blue(), 200)
            self.assertTrue(list(cache.glob('*.png'))); self.assertFalse(cache.is_relative_to(private))
            self.assertEqual((source.read_bytes(), legacy.read_bytes()), before)
            with patch('app_plugins.builtin.batch_text_video.layers.read_image', side_effect=AssertionError('Cached PNG should be reused')):
                again = thumbs.decode((str(source), ''), threading.Event())
            self.assertEqual(again, result)
            self.picture(source, 'green')
            changed = thumbs.decode((str(source), str(legacy)), threading.Event())
            self.assertGreater(changed.pixelColor(0, 0).green(), 50)

    def test_import_is_independent_of_thumbnail_generation_and_preserves_copied_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source.png'; self.picture(source)
            with patch.object(thumbs, 'decode', side_effect=AssertionError('Not visible')):
                imported = import_images([source], root/'private')
            self.assertEqual(imported['errors'], []); self.assertEqual(imported['layers'][0]['thumbnail'], '')
            self.assertFalse(list((root/'private').rglob('*.thumb.png')))
            source.unlink(); self.assertTrue(Path(imported['layers'][0]['path']).is_file())

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'), 'Real FFmpeg required')
    def test_video_poster_is_one_small_frame_and_uses_external_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            path = Path(directory)/'video.mp4'
            subprocess.run([os.environ['NODE_FFMPEG'], '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
                '-i', 'color=c=red:s=160x96:r=10:d=0.2', '-c:v', 'libx264', str(path)],
                check=True, capture_output=True, timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            image = thumbs.decode((str(path), ''), threading.Event())
            self.assertFalse(image.isNull()); self.assertLessEqual(max(image.width(), image.height()), 128)
            self.assertGreater(image.pixelColor(10, 10).red(), 200)
            self.assertEqual(len(list(thumbs.cache_root('thumbnails').glob('*.png'))), 1)

    def test_queue_and_memory_stay_bounded_and_removed_requests_are_not_decoded(self):
        service = thumbs.ThumbnailService(limit=80*1024)
        started, lock = [], threading.Lock()
        gate = threading.Event()
        def decode(payload, cancel):
            with lock: started.append(payload)
            gate.wait(2)
            image = QtGui.QImage(128, 128, QtGui.QImage.Format.Format_RGB32); image.fill(0); return image
        try:
            with patch.object(thumbs, 'decode', side_effect=decode):
                service.want('view', [(f'queued-{i}', '') for i in range(1000)])
                self.wait(lambda: len(started) == 2)
                self.assertEqual(len(service.running), 2)
                service.want('view', [('new-current', '')]); gate.set()
                self.wait(lambda: not service.running and ('new-current', '') in service.items)
                self.assertEqual(len(started), 3)
                self.assertLessEqual(service.bytes, service.limit)
        finally:
            gate.set(); service.stop(); service.pool.waitForDone(); service.deleteLater(); self.app.processEvents()


if __name__ == '__main__': unittest.main()
