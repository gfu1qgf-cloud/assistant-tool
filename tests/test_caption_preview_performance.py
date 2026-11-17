import copy
import json
import os
from pathlib import Path
import time
import unittest
from collections import OrderedDict
from contextlib import nullcontext
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
from app_plugins.builtin.batch_text_video.captions import _glyph,new_caption,write_caption_ass
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.caption_style_render import render_design
from app_plugins.builtin.batch_text_video.caption_styles import apply_style,PREFIX
from app_plugins.builtin.batch_text_video import text_outline,caption_glyph_cache as cache


class CaptionPreviewPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def layer(self,**changes):
        layer=new_follow_caption()
        layer.update(font='Arial',font_min=48,font_max=48,caption_follow_mode='line',
            outline_position='outside',outline_width=10,width=90,height=40,
            caption_per_line=4,shadow_enabled=False)
        layer.update(changes)
        return normalize_layers([layer])[0]

    def test_all_caption_preview_modes_and_multilayer_strokes_avoid_vector_boolean(self):
        words=[dict(text=t,start=i*.3,end=(i+1)*.3) for i,t in enumerate('Pane prosím ochraňuj moju rodinu každý deň'.split())]
        cases=[self.layer(caption_follow_mode=follow,caption_display_mode=display)
            for follow in ('word','line','none') for display in ('group','char')]
        layered=self.layer();layered['caption_effects'].update(stroke_expand_enabled=True,stroke_expand_layers=3)
        cases.append(layered)
        cases.append(apply_style(self.layer(caption_follow_mode='none'),PREFIX+'默认白字'))
        with patch.object(text_outline,'outside_ring',side_effect=AssertionError('Vector boolean in preview')):
            for layer in cases:
                with self.subTest(follow=layer['caption_follow_mode'],display=layer['caption_display_mode']):
                    image,_=render_design(layer,540,960,words=words,duration=3,time=.7)
                    self.assertFalse(image.isNull())
                    self.assertNotEqual(image,QtGui.QImage())

    def test_long_follow_cold_scrub_and_outline_parameter_changes_are_bounded(self):
        words=[dict(text='ochraňuj'+str(i),start=i*.25,end=(i+1)*.25) for i in range(400)]
        layer=self.layer();values=[]
        with patch.object(text_outline,'outside_ring',side_effect=AssertionError('Vector boolean in preview')):
            for moment,thickness in ((40,10),(41,10),(41,15),(42,7)):
                layer['outline_width']=thickness;start=time.perf_counter()
                image,_=render_design(layer,540,960,words=words,duration=100,time=moment)
                values.append(time.perf_counter()-start);self.assertFalse(image.isNull())
            for display,expanded in (('char',False),('group',True)):
                layer['caption_display_mode']=display
                layer['caption_effects'].update(stroke_expand_enabled=expanded,stroke_expand_layers=6)
                start=time.perf_counter();image,_=render_design(layer,540,960,words=words,duration=100,time=41)
                values.append(time.perf_counter()-start)
        self.assertTrue(all(value<4 for value in values),values)
        root=os.environ.get('LZX_CAPTION_PREVIEW_QA')
        if root:
            (Path(root)/'preview-times.json').write_text(json.dumps(values),encoding='utf8')
            image.save(str(Path(root)/'follow-outside.png'))

    def test_preview_does_not_poison_native_ass_or_change_source_configuration(self):
        import tempfile
        layer=self.layer();before=copy.deepcopy(layer)
        words=[dict(text='Božia',start=0,end=.5),dict(text='ochrana',start=.6,end=1)]
        render_design(layer,540,960,words=words,duration=2,time=.7)
        self.assertFalse(text_outline.is_preview());self.assertEqual(layer,before)
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'native.ass';events=write_caption_ass(target,words,layer,540,960,2)
            self.assertTrue(all(event['path'] for event in events))
            self.assertIn('\\p4',target.read_text('utf8'))

    def test_preview_and_vector_reference_keep_fill_outline_opacity_and_clip(self):
        import numpy as np
        layer=self.layer(outline_opacity=35,font_min=90,font_max=90)
        words=[dict(text='BO',start=0,end=1)]
        raster,_=render_design(layer,540,960,words=words,duration=2,time=.7)
        with patch.object(text_outline,'preview_geometry',return_value=nullcontext()):
            reference,_=render_design(layer,540,960,words=words,duration=2,time=.7)
        a=np.frombuffer(raster.constBits().asstring(raster.sizeInBytes()),np.uint8).astype(float)
        b=np.frombuffer(reference.constBits().asstring(reference.sizeInBytes()),np.uint8).astype(float)
        self.assertLess(np.mean(abs(a-b)),.3)
        self.assertEqual(raster.pixelColor(0,0).alpha(),0)

    def test_glyph_cache_returns_independent_paths_clusters_and_native_commands(self):
        layer=self.layer();first=_glyph('Božia',layer,30,native=True)
        original=QtGui.QPainterPath(first['_native_path']);edges=list(first['_cluster_edges'])
        first['_native_path'].translate(1000,1000);first['_cluster_edges'].clear()
        again=_glyph('Božia',layer,30,native=True)
        self.assertEqual(again['_native_path'],original);self.assertEqual(again['_cluster_edges'],edges)
        self.assertTrue(again['path'])
        with text_outline.preview_geometry():self.assertEqual(_glyph('Božia',layer,30,native=True)['path'],'')
        self.assertTrue(_glyph('Božia',layer,30,native=True)['path'])

    def test_glyph_cache_is_bounded_and_font_shaping_changes_invalidate(self):
        layer=self.layer();path=QtGui.QPainterPath();path.addRect(0,0,20,30)
        value=dict(path='m 0 0 l 20 0 l 20 30',_native_path=path,_cluster_edges=[0,20])
        builds=[]
        def build():builds.append(1);return cache.snapshot(value)
        with patch.object(cache,'_CACHE',OrderedDict()),patch.object(cache,'_BYTES',0),patch.object(cache,'BYTE_LIMIT',4096),patch.object(cache,'ENTRY_LIMIT',3):
            cache.get('same',layer,30,True,build);cache.get('same',layer,30,True,build)
            self.assertEqual(len(builds),1)
            for field in cache.FIELDS:
                other={**layer,field:str(layer[field])+'changed'};cache.get('same',other,30,True,build)
            self.assertEqual(len(builds),1+len(cache.FIELDS))
            for i in range(20):cache.get(str(i),layer,30,True,build)
            self.assertLessEqual(len(cache._CACHE),3);self.assertLessEqual(cache._BYTES,4096)

    def test_preview_context_is_restored_after_failure(self):
        with self.assertRaises(RuntimeError):
            with text_outline.preview_geometry():raise RuntimeError('interrupted')
        self.assertFalse(text_outline.is_preview())

    def test_font_database_change_invalidates_cached_fallback_geometry(self):
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        from app_plugins.builtin.batch_text_video import captions
        ensure_fonts()
        cache.clear()
        layer=self.layer()
        with patch.object(captions,'_uncached_glyph',wraps=captions._uncached_glyph) as build:
            _glyph('cache-reset',layer,24,native=True)
            _glyph('cache-reset',layer,24,native=True)
            self.assertEqual(build.call_count,1)
            self.assertGreater(cache._BYTES,0)
            self.app.fontDatabaseChanged.emit()
            self.assertEqual(cache._BYTES,0)
            self.assertFalse(cache._CACHE)
            _glyph('cache-reset',layer,24,native=True)
            self.assertEqual(build.call_count,2)
