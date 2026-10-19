import copy
import tempfile
import unittest
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay, render_preview_overlay, TextDoesNotFit
from app_plugins.builtin.batch_text_video.layers import normalize_layers, componentize_layers
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.store import DEFAULTS, Store, fresh_state
from app_plugins.builtin.batch_text_video.watermarks import CORNERS, OBJECT_ID, POSITION_DEFAULTS, apply_corner_watermarks, expand_watermarks
from app_plugins.builtin.batch_text_video.watermark_render import alpha_bounds
from app_plugins.builtin.batch_text_video.text_components import render_text_box


class EdgeAnchorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        self.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def watermarks(self):
        layers = apply_corner_watermarks(componentize_layers(None, DEFAULTS), {key: 'Generované AI LZX' for key, *_ in CORNERS})
        for child in layers[-1]['corners'].values():
            child.update(watermark_inset_x=0, watermark_inset_y=0)
        return layers

    def test_each_visible_watermark_touches_its_exact_corner_with_zero_inset(self):
        for width, height in ((540, 960), (1080, 1920), (1920, 1080)):
            for part in expand_watermarks(self.watermarks()):
                if not part.get('_watermark_corner'): continue
                image, _ = render_text_box(part, width, height)
                bounds = alpha_bounds(image)
                key = part['_watermark_corner']
                self.assertFalse(bounds.isEmpty())
                self.assertEqual(bounds.left() if key.endswith('left') else width-1-bounds.right(), 0)
                self.assertEqual(bounds.top() if key.startswith('top') else height-1-bounds.bottom(), 0)

    def test_pixel_and_percent_insets_are_measured_from_visible_effects(self):
        for effects in ({}, {'background_enabled':True, 'background_mode':'text', 'background_extra_left':14,
                            'background_extra_right':31,'background_extra_top':10,'background_extra_bottom':6,
                            'shadow_enabled':True,'shadow_x':-9,'shadow_y':12,'outline_width':5}):
            for unit, mx, my in (('pixels', 17, 29), ('percent', 2.5, 4)):
                for part in expand_watermarks(self.watermarks()):
                    if not part.get('_watermark_corner'): continue
                    part.update(effects, watermark_inset_unit=unit, watermark_inset_x=mx, watermark_inset_y=my)
                    image, _ = render_text_box(part, 1080, 1920)
                    bounds, key = alpha_bounds(image), part['_watermark_corner']
                    x, y = (17, 29) if unit == 'pixels' else (27, 77)
                    self.assertEqual(bounds.left() if key.endswith('left') else 1079-bounds.right(), x)
                    self.assertEqual(bounds.top() if key.startswith('top') else 1919-bounds.bottom(), y)

    def test_preview_export_and_font_layout_are_identical(self):
        layers = self.watermarks()
        settings = {**DEFAULTS, 'layers':layers, 'darkness':0}
        for width, height in ((540,960),(1920,1080)):
            final, sizes = render_overlay('', '', width, height, settings)
            preview, preview_sizes = render_preview_overlay('', '', width, height, settings)
            self.assertEqual(preview, final)
            self.assertEqual(sizes, preview_sizes)
            for part in expand_watermarks(layers):
                if not part.get('_watermark_corner'): continue
                image, size = render_text_box(part,width,height)
                moved, moved_size = render_text_box({**part,'watermark_inset_x':41,'watermark_inset_y':58},width,height)
                self.assertEqual(size,moved_size)
                self.assertEqual(image.copy(alpha_bounds(image)), moved.copy(alpha_bounds(moved)))

    def test_empty_corner_remains_disabled_and_impossible_inset_is_not_clipped(self):
        layers = apply_corner_watermarks(componentize_layers(None,DEFAULTS),{'top_right':'LZX'})
        self.assertEqual(sum(part['enabled'] for part in expand_watermarks(layers) if part.get('_watermark_corner')),1)
        part = next(part for part in expand_watermarks(layers) if part.get('_watermark_corner')=='top_right')
        with self.assertRaisesRegex(TextDoesNotFit,'未裁掉水印'):
            render_text_box({**part,'watermark_inset_x':539},540,960)

    def test_legacy_geometry_is_preserved_until_user_selects_corner_mode(self):
        layers = self.watermarks()
        original = layers[-1]['corners']['top_right']
        for field in POSITION_DEFAULTS: original.pop(field)
        original.update(x=71,y=9,width=35,height=7)
        normalized = normalize_layers(layers)
        child = normalized[-1]['corners']['top_right']
        self.assertEqual(child['watermark_placement'],'free')
        self.assertEqual((child['x'],child['y'],child['width'],child['height']),(71,9,35,7))
        part = next(part for part in expand_watermarks(normalized) if part.get('_watermark_corner')=='top_right')
        self.assertEqual(render_text_box(part,1080,1920)[0], render_text_box(original,1080,1920)[0])

    def test_invalid_insets_and_anchors_have_clear_validation(self):
        for changes in ({'watermark_inset_x':-1},{'watermark_inset_y':float('nan')},
                        {'watermark_inset_unit':'unknown'},{'watermark_placement':[]},
                        {'watermark_inset_unit':'percent','watermark_inset_x':101}):
            layers=self.watermarks(); layers[-1]['corners']['top_right'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError): normalize_layers(layers)
        layer=componentize_layers(None,DEFAULTS)[0]
        with self.assertRaisesRegex(ValueError,'位置基准'):
            normalize_layers([{**layer,'position_anchor_x':'top'}])

    def test_watermark_controls_convert_units_without_moving_and_copy_only_position(self):
        panel=LayerPanel(self.watermarks(),'.',settings=DEFAULTS)
        panel.select_layer_id(OBJECT_ID)
        editor=panel.watermark_properties
        editor.corner.setCurrentIndex(editor.corner.findData('top_right'))
        editor.insets['watermark_inset_x'].setValue(27)
        editor.insets['watermark_inset_y'].setValue(77)
        panel.set_canvas_size(1080,1920)
        before=copy.deepcopy(panel.current())
        editor.inset_unit.setCurrentIndex(editor.inset_unit.findData('percent'))
        self.assertAlmostEqual(editor.insets['watermark_inset_x'].value(),2.5)
        self.assertAlmostEqual(editor.insets['watermark_inset_y'].value(),77/19.2,places=4)
        a=next(x for x in expand_watermarks([before]) if x['_watermark_corner']=='top_right')
        b=next(x for x in expand_watermarks([panel.current()]) if x['_watermark_corner']=='top_right')
        self.assertEqual(render_text_box(a,1080,1920)[0],render_text_box(b,1080,1920)[0])
        editor.inset_unit.setCurrentIndex(editor.inset_unit.findData('pixels'))
        originals=copy.deepcopy(panel.current()['corners'])
        editor.apply_positions_button.click()
        for key,child in panel.current()['corners'].items():
            self.assertAlmostEqual(child['watermark_inset_x'],27)
            self.assertAlmostEqual(child['watermark_inset_y'],77)
            for field in originals[key].keys()-POSITION_DEFAULTS.keys(): self.assertEqual(child[field],originals[key][field])

    def test_text_anchor_switch_does_not_move_and_zero_means_frame_edge(self):
        panel=LayerPanel(componentize_layers(None,DEFAULTS),'.',settings=DEFAULTS)
        panel.select_layer_id('title')
        geometry={key:panel.current()[key] for key in ('x','y','width','height')}
        panel.anchor_controls['x'].setCurrentIndex(panel.anchor_controls['x'].findData('right'))
        panel.anchor_controls['y'].setCurrentIndex(panel.anchor_controls['y'].findData('bottom'))
        self.assertEqual(geometry,{key:panel.current()[key] for key in geometry})
        self.assertAlmostEqual(panel.properties['x'].value(),100-geometry['x']-geometry['width']/2,places=3)
        panel.properties['x'].setValue(0); panel.properties['y'].setValue(0)
        self.assertAlmostEqual(panel.current()['x']+panel.current()['width']/2,100)
        self.assertAlmostEqual(panel.current()['y']+panel.current()['height']/2,100)
        panel.properties['width'].setValue(55)
        panel.properties['height'].setValue(20)
        self.assertAlmostEqual(panel.current()['x']+panel.current()['width']/2,100)
        self.assertAlmostEqual(panel.current()['y']+panel.current()['height']/2,100)

    def test_text_pixels_drag_and_snap_menu_stay_consistent(self):
        panel=LayerPanel(componentize_layers(None,DEFAULTS),'.',settings=DEFAULTS)
        panel.select_layer_id('title'); panel.units.setCurrentIndex(panel.units.findData('pixels'))
        panel.snap_button.menu().actions()[5].trigger()
        self.assertEqual(panel.properties['x'].value(),0)
        self.assertEqual(panel.properties['y'].value(),0)
        panel.properties['x'].setValue(54); panel.properties['y'].setValue(96)
        self.assertAlmostEqual(panel.current()['x']+panel.current()['width']/2,95)
        self.assertAlmostEqual(panel.current()['y']-panel.current()['height']/2,5)
        panel.set_geometry('title',{'x':60,'y':20,'width':50,'height':20})
        self.assertAlmostEqual(panel.properties['x'].value(),162)
        self.assertAlmostEqual(panel.properties['y'].value(),192)
        self.assertEqual(panel.current()['position_anchor_x'],'right')
        panel.snap_to_edges('left','bottom')
        panel.current().update(text='Ámen',background_enabled=True,background_opacity=100,background_radius=0)
        image,_=render_text_box(panel.current(),1080,1920)
        bounds=alpha_bounds(image)
        self.assertEqual(bounds.left(),0)
        self.assertEqual(bounds.bottom(),1919)

    def test_anchor_settings_save_and_restore_per_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            state=fresh_state(); state['settings']['layers']=self.watermarks()
            state['settings']['layers'][0].update(position_anchor_x='left',position_anchor_y='bottom',x=42,y=80)
            saved=Store(directory).save(state,0)
            restored=Store(directory).load()
            self.assertEqual(saved['settings']['layers'],restored['settings']['layers'])

    def test_flat_watermark_stays_in_one_outer_scroll_without_inner_tabs(self):
        panel=LayerPanel(self.watermarks(),'.',settings=DEFAULTS)
        panel.select_layer_id(OBJECT_ID)
        panel.hide_catalog()
        scroll=QtWidgets.QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(panel)
        scroll.resize(440,780);scroll.show();self.app.processEvents()
        self.assertEqual(scroll.height(),780)
        self.assertEqual(scroll.horizontalScrollBar().maximum(),0)
        self.assertEqual(panel.watermark_properties.findChildren(QtWidgets.QTabWidget),[])
        self.assertTrue(panel.inspector.isAncestorOf(panel.watermark_properties))

    def test_inset_edit_does_not_quantize_existing_text_box_size(self):
        layers=self.watermarks()
        child=layers[-1]['corners']['top_right']
        child.update(x=75.123456,y=7.654321,width=44.123456,height=8.987654)
        panel=LayerPanel(layers,'.',settings=DEFAULTS);panel.select_layer_id(OBJECT_ID)
        editor=panel.watermark_properties
        editor.corner.setCurrentIndex(editor.corner.findData('top_right'))
        editor.insets['watermark_inset_x'].setValue(23)
        for key in ('x','y','width','height'):
            self.assertEqual(panel.current()['corners']['top_right'][key],child[key])


if __name__=='__main__': unittest.main()
