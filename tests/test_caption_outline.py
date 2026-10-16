"""Glyph construction seams must not become visible subtitle outlines."""
from collections import deque
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.captions import _glyph, _path_commands, new_caption, write_caption_ass
from app_plugins.builtin.batch_text_video.fonts import weight_font
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, PREFIX
from app_plugins.builtin.batch_text_video.caption_style_render import groups_and_events, render_design
from app_plugins.builtin.batch_text_video.layout import ensure_fonts


def component(**values):
    layer = new_caption()
    layer.update(font='Fredoka', font_weight=900, font_min=100, font_max=100,
                 caption_random_size=False, color='#00e9e9', outline_color='#006c6c',
                 outline_width=5, outline_opacity=100, shadow_enabled=False,
                 background_enabled=False, width=98, height=90, align='left', vertical='top')
    layer.update(values)
    return normalize_layers([layer])[0]


def raw_glyph(text, layer, size):
    font = weight_font(layer['font'], layer['font_weight'], layer['italic'])
    font.setPixelSize(round(size))
    metrics = QtGui.QFontMetricsF(font)
    path = QtGui.QPainterPath()
    path.addText(0, metrics.ascent(), font, text)
    bounds = path.boundingRect()
    path.translate(-bounds.left(), -min(0, bounds.top()))
    return path


def raster(path, pen_width=5):
    image = QtGui.QImage(700, 170, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.GlobalColor.white)
    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.translate(10,10)
    painter.setPen(QtGui.QPen(QtGui.QColor('#006c6c'), pen_width))
    painter.setBrush(QtGui.QColor('#00e9e9'))
    painter.drawPath(path)
    painter.end()
    return image


class CaptionOutlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def fredoka(self):
        if 'Fredoka' not in QtGui.QFontDatabase.families():
            self.skipTest('Fredoka is not installed; no font is downloaded by tests')
        return component()

    def test_fredoka_contours_are_unioned_before_stroking(self):
        layer = self.fredoka()
        before = copy.deepcopy(layer)
        raw = raw_glyph('toto', layer, 100)
        fixed = _glyph('toto', layer, 100, native=True)['_native_path']
        self.assertNotEqual(raw, raw.simplified(), 'regression font must contain overlapping contours')
        self.assertEqual(fixed, raw.simplified())
        old_image, new_image = raster(raw), raster(fixed)
        corrected = sum(old_image.pixelColor(x,y).green() < 140 and new_image.pixelColor(x,y).green() > 220
                        for y in range(170) for x in range(700))
        self.assertGreater(corrected, 50, 'internal dark seams must be replaced by glyph fill')
        self.assertEqual(layer, before)

    def test_real_counters_are_not_filled(self):
        layer = self.fredoka()
        def holes(path):
                bounds = path.boundingRect().toAlignedRect().adjusted(-2,-2,2,2)
                outside = {(x,y) for y in range(bounds.top(),bounds.bottom()+1)
                           for x in range(bounds.left(),bounds.right()+1)
                           if not path.contains(QtCore.QPointF(x+.5,y+.5))}
                holes = []
                while outside:
                    start = outside.pop()
                    queue, region = deque([start]), {start}
                    while queue:
                        x,y = queue.popleft()
                        for item in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                            if item in outside:
                                outside.remove(item); region.add(item); queue.append(item)
                    if len(region)>4 and not any(x in (bounds.left(),bounds.right()) or y in (bounds.top(),bounds.bottom()) for x,y in region):
                        holes.append(region)
                return len(holes)
        for family in ('Fredoka','Open Sans'):
            for text in ('B','o','8'):
                with self.subTest(family=family,text=text):
                    layer = component(font=family)
                    original = raw_glyph(text,layer,100)
                    actual = _glyph(text,layer,100,native=True)['_native_path']
                    self.assertGreaterEqual(holes(original),1)
                    self.assertEqual(holes(actual),holes(original))

    def test_union_preserves_font_metrics_and_slovak_accents(self):
        for family in ('Open Sans','Poppins','Fredoka'):
            if family not in QtGui.QFontDatabase.families():
                continue
            for text in ('toto','Boh','ukázal','čďľňôŕšťž','ŽLTÝ','gyp'):
                with self.subTest(family=family,text=text):
                    layer = component(font=family)
                    font = weight_font(family, layer['font_weight'])
                    font.setPixelSize(100)
                    metrics = QtGui.QFontMetricsF(font)
                    raw = raw_glyph(text, layer, 100)
                    actual = _glyph(text, layer, 100, native=True)
                    self.assertAlmostEqual(actual['_baseline'], metrics.ascent(), places=4)
                    self.assertAlmostEqual(actual['width'], max(metrics.horizontalAdvance(text),raw.boundingRect().width()), places=4)
                    self.assertFalse(actual['_native_path'].isEmpty())

    def test_underline_and_strikeout_form_union_not_xor_holes(self):
        layer = self.fredoka()
        plain = _glyph('toto',layer,100,native=True)['_native_path']
        font = weight_font(layer['font'],layer['font_weight']);font.setPixelSize(100)
        metrics = QtGui.QFontMetricsF(font)
        source = QtGui.QPainterPath();source.addText(0,metrics.ascent(),font,'toto')
        for values in ({'underline':True},{'strikeout':True},{'underline':True,'strikeout':True}):
            actual = _glyph('toto',{**layer,**values},100,native=True)['_native_path']
            expected = QtGui.QPainterPath(plain)
            # Decorations extend to the advance origin; original layout also
            # includes that space, so compare in the same positioned frame.
            expected.translate(source.boundingRect().left(),0)
            bounds = expected.boundingRect().toAlignedRect()
            for y in range(bounds.top(),bounds.bottom()+1):
                for x in range(bounds.left(),bounds.right()+1):
                    point = QtCore.QPointF(x+.5,y+.5)
                    if all(expected.contains(point+QtCore.QPointF(dx,dy)) for dx,dy in ((0,0),(.5,0),(-.5,0),(0,.5),(0,-.5))):
                        self.assertTrue(actual.contains(point),'decorations must not erase glyph interiors')

    def test_catalog_preview_and_ass_share_merged_glyphs(self):
        layer = self.fredoka()
        layer = {**apply_style(layer,PREFIX+'粉色气泡'), **layer, 'caption_preset':PREFIX+'粉色气泡'}
        words = [dict(text='toto',start=.1,end=.8)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'captions.ass'
            events = write_caption_ass(path,words,layer,540,960,1)
            self.assertEqual(events[0]['path'],_path_commands(events[0]['_native_path']))
            self.assertEqual(events[0]['_native_path'],events[0]['_native_path'].simplified())
            image,_ = render_design({**layer,'text':'Boh ti ukázal toto video'},540,960)
            self.assertFalse(image.isNull())
            self.assertGreater(path.stat().st_size,500)

    def test_real_ffmpeg_export_has_no_internal_dark_strokes(self):
        layer = self.fredoka()
        ffmpeg = os.environ.get('BATCH_TEST_FFMPEG')
        if not ffmpeg:
            self.skipTest('BATCH_TEST_FFMPEG not configured')
        words = [dict(text='toto',start=0,end=1)]
        layer.update(caption_preset='appear', x=50, y=50)
        with tempfile.TemporaryDirectory() as directory:
            events = write_caption_ass(Path(directory)/'caption.ass',words,layer,540,240,1)
            event = events[0]
            result = subprocess.run([ffmpeg,'-nostdin','-hide_banner','-loglevel','error',
                '-f','lavfi','-i','color=c=white:s=540x240:d=1:r=1',
                '-vf','ass=filename=caption.ass','-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],
                cwd=directory,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
            image = QtGui.QImage.fromData(result.stdout,'PNG')
            self.assertFalse(image.isNull())
            raw = raw_glyph('toto',layer,50)
            clean = raw.simplified()
            stroker = QtGui.QPainterPathStroker()
            stroker.setWidth(4)
            seams = stroker.createStroke(raw)
            stroker.setWidth(8)
            safe = clean.subtracted(stroker.createStroke(clean)).intersected(seams)
            samples=[]
            bounds=safe.boundingRect().toAlignedRect()
            for y in range(bounds.top(),bounds.bottom()+1):
                for x in range(bounds.left(),bounds.right()+1):
                    if safe.contains(QtCore.QPointF(x+.5,y+.5)):
                        color=image.pixelColor(round(x+event['x']),round(y+event['y']))
                        samples.append(color.green()>170 and color.blue()>170)
            self.assertGreater(len(samples),4)
            self.assertGreater(sum(samples)/len(samples),.8)
            # The two "o" counters must remain background, not cyan fill.
            bounds = clean.boundingRect().toAlignedRect().adjusted(-1,-1,1,1)
            white = {(x,y) for y in range(bounds.top(),bounds.bottom()+1)
                     for x in range(bounds.left(),bounds.right()+1)
                     if not clean.contains(QtCore.QPointF(x+.5,y+.5))}
            counters = []
            while white:
                start = white.pop(); region={start}; queue=deque([start])
                while queue:
                    x,y=queue.popleft()
                    for item in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                        if item in white:
                            white.remove(item);region.add(item);queue.append(item)
                if len(region)>5 and not any(x in (bounds.left(),bounds.right()) or y in (bounds.top(),bounds.bottom()) for x,y in region):
                    counters.append(region)
            self.assertEqual(len(counters),2)
            for region in counters:
                x=sum(p[0] for p in region)/len(region)
                y=sum(p[1] for p in region)/len(region)
                color=image.pixelColor(round(x+event['x']),round(y+event['y']))
                # At this small output size the border antialiases into the
                # counter. Its white-background red must still dominate;
                # filled cyan or a dark seam would have red close to zero.
                self.assertGreater(min(color.red(),color.green(),color.blue()),180)
            qa = os.environ.get('CAPTION_OUTLINE_QA_DIR')
            if qa:
                image.save(str(Path(qa)/'caption-outline-export.png'))


if __name__=='__main__':
    unittest.main()
