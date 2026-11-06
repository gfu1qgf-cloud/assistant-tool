import time
import os
import json
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import text_outline as outline
from app_plugins.builtin.batch_text_video.layout import _text_layout, ensure_fonts
from app_plugins.builtin.batch_text_video.text_components import draw_text_groups

class OutsideSpeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([]);ensure_fonts()

    def test_long_paragraph_does_not_use_vector_boolean_and_preserves_glyph_fill(self):
        layer=dict(color='#ffffff',outline_color='#ff0000',outline_width=8,outline_opacity=60,
                   outline_blur=0,outline_position='outside',shadow_enabled=False)
        groups,_=_text_layout('Prosím ťa, ochraňuj moju rodinu. '*100,'Arial',24,980,'left',1.1)
        surface=QtGui.QImage(1080,1920,QtGui.QImage.Format.Format_ARGB32_Premultiplied);surface.fill(0)
        painter=QtGui.QPainter(surface)
        started=time.perf_counter()
        try:
            with patch.object(outline,'outside_ring',side_effect=AssertionError('paragraph vector boolean')):
                draw_text_groups(painter,groups,layer,30,30,1.)
        finally:painter.end()
        elapsed=time.perf_counter()-started
        print('OUTSIDE_PARAGRAPH_SECONDS',round(elapsed,4))
        self.assertLess(elapsed,2.0)
        self.assertFalse(surface.isNull())

    def test_ring_cache_returns_independent_paths_and_is_bounded(self):
        path=QtGui.QPainterPath();path.addText(0,100,QtGui.QFont('Arial',60),'Grace')
        first=outline.outside_ring(path,6);count=len(outline._RINGS)
        second=outline.outside_ring(path,6);self.assertEqual(first,second);self.assertEqual(len(outline._RINGS),count)
        first.translate(500,500);self.assertNotEqual(first,outline.outside_ring(path,6))
        self.assertLessEqual(outline._ELEMENTS,outline.ELEMENT_LIMIT)

    @unittest.skipUnless(os.environ.get('LZX_OUTLINE_BASELINE'),'Explicit old-algorithm benchmark only')
    def test_measure_before_after_same_shaped_paragraph(self):
        root=Path(os.environ['LZX_OUTLINE_BASELINE'])
        source=root/'source-backup/app_plugins/builtin/batch_text_video/text_outline.py'
        spec=importlib.util.spec_from_file_location('app_plugins.builtin.batch_text_video._old_outline',source)
        old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
        groups,_=_text_layout('Prosím ťa, ochraňuj moju rodinu. ','Arial',24,980,'left',1.1)
        layer=dict(outline_color='#ff0000',outline_width=8,outline_opacity=60,outline_blur=0)
        results={}
        for name,module in (('before',old),('after',outline)):
            image=QtGui.QImage(1080,1920,QtGui.QImage.Format.Format_ARGB32_Premultiplied);image.fill(0)
            painter=QtGui.QPainter(image);started=time.perf_counter()
            try:module.draw_outside(painter,groups,layer,30,30,1)
            finally:painter.end()
            results[name]=time.perf_counter()-started
        (root/'outline-benchmark.json').write_text(json.dumps(results,indent=2),encoding='utf8')
        print('OUTLINE_BEFORE_AFTER',results);self.assertLess(results['after'],results['before'])
