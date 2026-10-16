"""Synthetic-data regressions for GUI blocking and asynchronous lifetimes."""
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from model import MediaThumbnail
from model.MediaPlayback import _EmbeddedFfplayReviewSurface
from model.QtProcessLifecycle import retire_process
from PyQt6 import sip
from app_plugins.builtin.batch_text_video.component_ui import TextBoxProperties, FontCombo
from app_plugins.builtin.batch_text_video.pool_ui import ResourceTable
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.smart_image_search.ui import SmartImageSearchDialog
from app_plugins.builtin.image_classifier.ui import ImageClassifierDialog


class ResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def wait(self, condition, timeout=4):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(condition(), 'asynchronous operation did not finish')

    def decoder(self, delay=.3, color='red'):
        image = QtGui.QImage(64, 32, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor(color))
        data = QtCore.QByteArray()
        buffer = QtCore.QBuffer(data)
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, 'PNG'))
        payload = base64.b64encode(bytes(data)).decode('ascii')
        original = MediaThumbnail._FrameJob
        class Decoder(original):
            def __init__(job, *args):
                original.__init__(job, *args)
                job.process.setProgram(sys.executable)
                job.process.setArguments(['-c', f'import sys,time,base64;time.sleep({delay});sys.stdout.buffer.write(base64.b64decode({payload!r}))'])
        return Decoder

    def test_slow_decoder_does_not_block_gui_timer(self):
        loader = MediaThumbnail.LatestMediaThumbnail()
        results, beats = [], []
        loader.ready.connect(lambda *args: results.append(args))
        timer = QtCore.QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: beats.append(time.monotonic()))
        timer.start()
        with patch.object(MediaThumbnail, '_FrameJob', self.decoder()):
            started = time.monotonic()
            loader.request('synthetic.mp4')
            self.assertLess(time.monotonic() - started, .15)
            self.wait(lambda: bool(results))
        timer.stop()
        self.assertGreater(len(beats), 15)
        self.assertLess(max(b-a for a,b in zip(beats, beats[1:])), .15)
        self.assertFalse(results[0][2])
        self.assertEqual(results[0][1].pixelColor(0, 0).name(), '#ff0000')
        loader.deleteLater()

    def test_timeout_kills_child_without_blocking(self):
        loader = MediaThumbnail.LatestMediaThumbnail(timeout_ms=150)
        results = []
        loader.ready.connect(lambda *args: results.append(args))
        with patch.object(MediaThumbnail, '_FrameJob', self.decoder(delay=10)):
            started = time.monotonic()
            loader.request('synthetic.mp4')
            self.wait(lambda: bool(results))
            self.assertLess(time.monotonic() - started, 2)
        self.assertIn('超时', results[0][2])
        self.assertIsNone(loader._active)
        loader.deleteLater()

    def test_replacement_ignores_stale_frame(self):
        loader = MediaThumbnail.LatestMediaThumbnail()
        results = []
        loader.ready.connect(lambda *args: results.append(args))
        with patch.object(MediaThumbnail, '_FrameJob', self.decoder(delay=2, color='red')):
            loader.request('first.mp4')
        with patch.object(MediaThumbnail, '_FrameJob', self.decoder(delay=.05, color='blue')):
            current = loader.request('second.mp4')
            self.wait(lambda: bool(results))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], current)
        self.assertEqual(results[0][1].pixelColor(0, 0).name(), '#0000ff')
        loader.deleteLater()

    def test_destroy_loader_during_decode_reaps_process(self):
        with patch.object(MediaThumbnail, '_FrameJob', self.decoder(delay=10)):
            parent = QtWidgets.QWidget()
            loader = MediaThumbnail.LatestMediaThumbnail(parent)
            loader.request('synthetic.mp4')
            job = loader._active
            parent.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            self.wait(lambda: job._done)
        self.app.processEvents()

    def test_invalid_decoder_is_async_error(self):
        loader = MediaThumbnail.LatestMediaThumbnail()
        rows = []
        loader.ready.connect(lambda *args: rows.append(args))
        loader.request('source.mp4', ffmpeg_path='not-a-real-ffmpeg.exe')
        self.wait(lambda: bool(rows))
        self.assertIn('启动失败', rows[0][2])
        loader.deleteLater()

    def test_retire_process_survives_immediate_window_deletion(self):
        for _ in range(8):
            window = QtWidgets.QWidget()
            process = QtCore.QProcess(window)
            process.start(sys.executable, ['-c', 'import time;time.sleep(10)'])
            self.wait(lambda: process.state() == QtCore.QProcess.ProcessState.Running)
            started = time.monotonic()
            retire_process(process, grace_ms=50)
            self.assertLess(time.monotonic()-started, .1)
            window.deleteLater()
            self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            self.wait(lambda: sip.isdeleted(process))

    def test_real_ffmpeg_thumbnail_preserves_aspect_ratio(self):
        executable = os.environ.get('BATCH_TEST_FFMPEG')
        if not executable:
            self.skipTest('BATCH_TEST_FFMPEG not configured')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'frame.png'
            image = QtGui.QImage(640, 320, QtGui.QImage.Format.Format_RGB32)
            image.fill(QtGui.QColor('red'))
            self.assertTrue(image.save(str(source)))
            loader = MediaThumbnail.LatestMediaThumbnail()
            rows = []
            loader.ready.connect(lambda *args: rows.append(args))
            loader.request(str(source), ffmpeg_path=executable)
            self.wait(lambda: bool(rows))
            self.assertFalse(rows[0][2], rows[0][2])
            actual = rows[0][1]
            self.assertAlmostEqual(actual.width()/actual.height(), 2, places=2)
            self.assertGreater(actual.pixelColor(10,10).red(), 240)
            loader.deleteLater()

    def test_ffplay_resize_does_not_redecode(self):
        surface = _EmbeddedFfplayReviewSurface()
        surface._thumbnail.request = Mock()
        surface.source = 'synthetic.mp4'
        surface._show_still_frame(1)
        surface._show_still_frame(1)
        surface.resize(500, 400)
        self.assertEqual(surface._thumbnail.request.call_count, 1)
        surface.close()

    def test_classifier_resize_reuses_thumbnail(self):
        dialog = ImageClassifierDialog({})
        dialog._first_selected_result = Mock(return_value={'source':'synthetic.mov'})
        dialog._thumbnail.request = Mock()
        dialog._update_preview()
        dialog._update_preview()
        dialog.resize(900, 600)
        self.assertEqual(dialog._thumbnail.request.call_count, 1)
        dialog.close()

    def test_pool_unchanged_preserves_items_and_selection(self):
        table = ResourceTable(['Name'])
        rows = [('id', ['Name'], 'tip', '')]
        table.populate(rows)
        table.selectRow(0)
        original = table.item(0, 0)
        table.populate(rows)
        self.assertIs(table.item(0, 0), original)
        self.assertEqual(table.identifiers(), ['id'])
        table.populate([('id', ['New'], 'tip', '')])
        self.assertEqual(table.item(0,0).text(), 'New')
        self.assertEqual(table.identifiers(), ['id'])
        table.close()

    def test_weight_metadata_is_not_recomputed_for_unrelated_settings(self):
        import app_plugins.builtin.batch_text_video.component_ui as module
        widget = TextBoxProperties()
        with patch.object(module, 'font_weight_status', wraps=module.font_weight_status) as status:
            widget.set_layer(normalize_layers([new_caption()])[0])
            first = status.call_count
            widget._show_effect_controls()
            widget._show_effect_controls()
            self.assertEqual(status.call_count, first)
            widget._invalidate_weight_status()
            widget._show_effect_controls()
            self.assertEqual(status.call_count, first+1)
        widget.close()

    def test_font_popup_preserves_native_face_preview_delegate(self):
        widget = FontCombo()
        reference = QtWidgets.QFontComboBox()
        self.assertEqual(widget.view().itemDelegate().metaObject().className(),
                         reference.view().itemDelegate().metaObject().className())
        self.assertNotIn('界面字体显示名称', widget.toolTip())
        widget.close()
        reference.close()

    def test_slow_index_count_runs_off_gui_and_can_cancel(self):
        threads, calls = [], []
        def count(_model, *, allowed_roots, cancelled):
            threads.append(QtCore.QThread.currentThread() == self.app.thread())
            calls.append(list(allowed_roots))
            for _ in range(60):
                if cancelled():
                    return None
                time.sleep(.005)
            return 123
        index = Mock()
        index.count = count
        encoder = Mock(model_id='synthetic', model_key='base')
        started = time.monotonic()
        dialog = SmartImageSearchDialog({'library_roots':['C:/synthetic']}, index=index, encoder=encoder, store=object())
        self.assertLess(time.monotonic()-started, .2)
        self.wait(lambda: dialog._count_worker is None)
        self.assertIn('123 张', dialog.index_status.text())
        self.assertFalse(any(threads))
        dialog._refresh_index_status()
        self.assertFalse(dialog.shutdown())
        self.wait(lambda: dialog._count_worker is None)
        self.assertTrue(dialog.shutdown())
        dialog.close()

    def test_plain_title_body_preview_runs_in_worker(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.store import Store
        import app_plugins.builtin.batch_text_video.ui as module
        seen = []
        original = module.render_preview_overlay
        def render(*args):
            seen.append(QtCore.QThread.currentThread() == self.app.thread())
            return original(*args)
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'render_preview_overlay', side_effect=render), patch.object(BatchTextVideoDialog, 'request_background'):
            dialog = BatchTextVideoDialog(store=Store(directory))
            dialog.update_preview()
            self.wait(lambda: dialog.overlay_worker is None, timeout=10)
            self.assertTrue(seen)
            self.assertFalse(any(seen))
            dialog.close()


if __name__ == '__main__':
    unittest.main()
