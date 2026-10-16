import base64
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from qt_compat import QtCore, QtGui, QtWidgets
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import ResourceRef, RenderPlan
from model.node_workflow.preview import frame_preview
from PYUI.node_preview import NodePreviewDialog

FFMPEG = os.environ.get('NODE_FFMPEG', '')


def png(width=1024, height=768):
    image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#729dc9'))
    buffer = QtCore.QBuffer(); buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return base64.b64encode(bytes(buffer.data())).decode('ascii')


class PreviewWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_resolution_label_100_percent_and_refresh_signal_preserve_result(self):
        result = {'node_id': 'synthetic', 'title': '视频', 'text': '原数据', 'png': png(),
                  'source_size': [1024, 768], 'time': 1}
        dialog = NodePreviewDialog(result)
        try:
            dialog.show(); self.app.processEvents()
            self.assertEqual(dialog.quality.currentData(), 1080)
            self.assertIn('源规格 1024×768', dialog.heading.text())
            self.assertIn('预览 1024×768', dialog.heading.text())
            self.assertFalse(dialog.text.isVisible())
            dialog.zoom.setCurrentIndex(1); self.app.processEvents()
            self.assertEqual(dialog.image.pixmap().size(), QtCore.QSize(1024, 768))
            self.assertFalse(dialog.scroll.widgetResizable())
            called = []; dialog.refreshRequested.connect(lambda: called.append(True))
            dialog.refresh_button.click(); self.assertEqual(called, [True])
            dialog.show_error('synthetic failure'); dialog.set_busy(False)
            self.assertIn('synthetic failure', dialog.status.text())
            self.assertEqual(dialog.text.toPlainText(), '原数据')
            before = dialog.image.original.cacheKey()
            with self.assertRaises(ValueError): dialog.update_result(dict(result, png='!invalid!'))
            self.assertEqual(dialog.image.original.cacheKey(), before)
            self.assertEqual(dialog.result, result)
            qa = os.environ.get('NODE_QA')
            if qa:
                Path(qa).mkdir(parents=True, exist_ok=True)
                dialog.showMaximized(); self.app.processEvents()
                dialog.grab().save(str(Path(qa) / '视频预览.png'))
        finally:
            dialog.close()

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要真实 FFmpeg')
    def test_default_keeps_1k_native_and_quality_options_only_limit_long_edge(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / '1k.mp4'
            result = subprocess.run([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=blue:s=1024x768:r=10:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)],
                capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace'))
            context = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
            context.ffmpeg = FFMPEG
            plan = RenderPlan(ResourceRef.capture(source, 'video'), (), '')
            for edge, expected in ((1080, QtCore.QSize(1024, 768)), (720, QtCore.QSize(720, 540)),
                                   (2160, QtCore.QSize(1024, 768))):
                target = root / str(edge); target.mkdir()
                data, at = frame_preview(plan, 0, context, target, edge)
                image = QtGui.QImage.fromData(base64.b64decode(data))
                self.assertEqual(image.size(), expected)
                self.assertEqual(context.preview_source_size, [1024, 768])
