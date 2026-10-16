"""Start-position controls must affect both the editor and real animation."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.scrolling_text import (
    new_scrolling_component, scroll_endpoints, scroll_position, render_scroll_preview, prepare_scroll_plane)
from app_plugins.builtin.batch_text_video.scrolling_text_ui import ScrollTextProperties
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.store import Store, fresh_state


def component(**values):
    layer = new_scrolling_component()
    layer.update(text='ONE\nTWO\nTHREE\nFOUR',font='Segoe UI',font_max=72,width=80,height=50,
                 padding=4,outline_width=0,scroll_edge_fade=0)
    layer.update(values)
    return normalize_layers([layer])[0]


class ScrollOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_new_starts_visible_but_legacy_motion_is_unchanged(self):
        new = component()
        self.assertEqual(new['scroll_start_anchor'],'top')
        self.assertEqual(scroll_position(new,10,100,200,0),0)
        legacy = dict(new,scroll_start_offset=-20,scroll_end_offset=40)
        legacy.pop('scroll_start_anchor');legacy.pop('scroll_end_anchor')
        legacy = normalize_layers([legacy])[0]
        self.assertEqual(legacy['scroll_start_anchor'],'outside')
        self.assertEqual(scroll_position(legacy,10,100,200,0),80)
        self.assertEqual(scroll_position(legacy,10,100,200,10),-160)

    def test_anchors_and_positive_negative_fine_offsets(self):
        for direction in ('up','down'):
            for anchor, base in (('top',0),('center',-50),('bottom',-100)):
                value = component(text_animation=direction,scroll_start_anchor=anchor,scroll_end_anchor=anchor,
                                  scroll_start_offset=25,scroll_end_offset=-40)
                self.assertEqual(scroll_endpoints(value,100,200),(base+25,base-40))
                self.assertEqual(scroll_position(value,10,100,200,0),base+25)
                self.assertEqual(scroll_position(value,10,100,200,10),base-40)

    def test_editor_equals_actual_start_for_all_anchors_and_offsets(self):
        for direction in ('up','down'):
            for anchor in ('outside','top','center','bottom'):
                for offset in (-25,0,25):
                    with self.subTest(direction=direction,anchor=anchor,offset=offset):
                        value=component(text_animation=direction,scroll_start_anchor=anchor,scroll_start_offset=offset)
                        editor,_=render_scroll_preview(value,240,320)
                        actual,_=render_scroll_preview(value,240,320,duration=10,time=0)
                        self.assertEqual(editor,actual)

    def test_offset_edit_changes_pixels_without_playing_video(self):
        value=component()
        images=[render_scroll_preview(dict(value,scroll_start_offset=offset),240,320)[0] for offset in (-25,0,25)]
        self.assertNotEqual(images[0],images[1])
        self.assertNotEqual(images[1],images[2])

    def test_ui_anchors_and_units_survive_selection_and_save(self):
        with tempfile.TemporaryDirectory() as directory:
            panel=LayerPanel(None,directory)
            panel.add_scrolling_text()
            identifier=panel.current()['id']
            ui=panel.scroll_properties
            self.assertEqual(ui.start_anchor.currentData(),'top')
            ui.start_anchor.setCurrentIndex(ui.start_anchor.findData('center'))
            ui.end_anchor.setCurrentIndex(ui.end_anchor.findData('bottom'))
            ui.controls['scroll_start_offset'].setValue(-25)
            before=copy.deepcopy(panel.current())
            ui.units.setCurrentIndex(ui.units.findData('pixels'))
            self.assertEqual(panel.current()['scroll_start_offset'],-25)
            panel.select_layer_id('title');panel.select_layer_id(identifier)
            self.assertEqual(ui.start_anchor.currentData(),'center')
            self.assertEqual(ui.end_anchor.currentData(),'bottom')
            self.assertEqual(panel.current()['scroll_start_offset'],-25)
            store=Store(Path(directory)/'state')
            state=fresh_state();state['settings']['layers']=panel.values()
            store.save(state,0)
            saved=next(layer for layer in store.load()['settings']['layers'] if layer['id']==identifier)
            self.assertEqual(saved['scroll_start_anchor'],'center')
            self.assertEqual(saved['scroll_end_anchor'],'bottom')
            self.assertEqual(saved['scroll_start_offset'],-25)
            panel.deleteLater();self.app.processEvents()

    def test_prepared_plane_uses_the_same_positions(self):
        value=component(scroll_start_anchor='center',scroll_end_anchor='bottom',scroll_start_offset=-15,
                        scroll_end_offset=20,scroll_start_delay=1,scroll_end_early=2)
        with tempfile.TemporaryDirectory() as directory:
            assets=prepare_scroll_plane(value,240,320,10,directory)
            expected=scroll_endpoints(value,assets['height'],assets['text_height'])
            self.assertEqual(assets['timing'],(1,8,*expected))

    def test_invalid_anchor_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'起点／终点'):
            component(scroll_start_anchor='invalid')

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'requires real FFmpeg')
    def test_real_export_first_frame_matches_editor_for_signed_offsets(self):
        import test_batch_scrolling_text as helpers
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        helper=helpers.RealScrollTextTests()
        with tempfile.TemporaryDirectory() as directory:
            for offset in (-15,0,20):
                value=component(text='START',scroll_start_offset=offset)
                state,job=helper.state(directory,value,duration=2)
                output,_=render_static_text(job,state,directory,preview=True)
                actual=helper.frame(output,0)
                expected,_=render_scroll_preview(value,240,160)
                a=helper.whites(actual)
                b=[point for point in helper.whites(expected) if expected.pixelColor(*point).alpha()>160]
                self.assertTrue(a and b)
                self.assertLess(abs(sum(p[1] for p in a)/len(a)-sum(p[1] for p in b)/len(b)),3)


if __name__=='__main__':
    unittest.main()
