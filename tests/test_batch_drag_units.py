import copy
import gc
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore,QtGui,QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.preview import Preview,dragged_geometry
from app_plugins.builtin.batch_text_video.store import DEFAULTS,Store,fresh_state
from app_plugins.builtin.batch_text_video.layers import componentize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts,render_overlay
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class DragUnitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    @classmethod
    def tearDownClass(cls):
        # Keep QApplication alive while Qt destroys widgets retained by signal
        # cycles. Interpreter finalization otherwise destroys them in an
        # unpredictable order and can access freed native Qt state on Windows.
        for widget in cls.app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        cls.app.processEvents()
        gc.collect()

    def layer(self,identifier="title"):
        layer = next(layer for layer in componentize_layers(None,DEFAULTS) if layer["id"] == identifier)
        layer.update(x=50,y=30,width=60,height=20)
        return layer

    def panel(self):
        panel = LayerPanel(componentize_layers(None,DEFAULTS),".",settings=DEFAULTS)
        panel.rebuild("title")
        panel.set_canvas_size(1080,1920)
        return panel

    def mouse_event(self,kind,point,button=QtCore.Qt.MouseButton.LeftButton):
        return QtGui.QMouseEvent(kind,QtCore.QPointF(point),QtCore.QPointF(point),button,
            QtCore.Qt.MouseButton.LeftButton,QtCore.Qt.KeyboardModifier.NoModifier)

    def test_percent_to_pixels_roundtrip_does_not_change_any_layer_data(self):
        panel = self.panel()
        before = panel.values()
        layer = panel.current()
        for _ in range(30):
            panel.units.setCurrentIndex(1)
            self.assertAlmostEqual(panel.properties["x"].value(),layer["x"]*10.8,places=2)
            self.assertAlmostEqual(panel.properties["height"].value(),layer["height"]*19.2,places=2)
            panel.units.setCurrentIndex(0)
        self.assertEqual(panel.values(),before)

    def test_pixel_edit_converts_only_edited_field(self):
        panel = self.panel()
        before = copy.deepcopy(panel.current())
        panel.units.setCurrentIndex(1)
        panel.properties["width"].setValue(756)
        self.assertAlmostEqual(panel.current()["width"],70)
        for key in before.keys()-{"width"}:
            self.assertEqual(panel.current()[key],before[key])
        panel.properties["y"].setValue(960)
        self.assertAlmostEqual(panel.current()["y"],50)
        panel.units.setCurrentIndex(0)
        self.assertAlmostEqual(panel.properties["y"].value(),50)

    def test_switching_canvas_refreshes_pixel_readout_but_preserves_relative_layout(self):
        panel = self.panel()
        panel.units.setCurrentIndex(1)
        before = panel.values()
        panel.set_canvas_size(1920,1080)
        self.assertEqual(panel.values(),before)
        self.assertAlmostEqual(panel.properties["x"].value(),panel.current()["x"]*19.2,places=2)
        self.assertIn("1920 × 1080",panel.canvas_label.text())
        self.assertEqual(panel.properties["x"].maximum(),1920)
        self.assertEqual(panel.properties["y"].maximum(),1080)

    def test_opacity_edit_does_not_quantize_geometry(self):
        panel = self.panel()
        geometry = {key:panel.current()[key] for key in ("x","y","width","height")}
        panel.opacity.setValue(76.7)
        self.assertEqual(geometry,{key:panel.current()[key] for key in geometry})

    def test_drag_resize_all_eight_handles_keeps_opposite_edges_and_limits(self):
        original = {"x":50,"y":50,"width":60,"height":40}
        from app_plugins.builtin.batch_text_video.preview import HANDLES
        for handle in HANDLES:
            values = dragged_geometry(original,5,7,handle)
            if handle[0] < 0:
                self.assertAlmostEqual(values["x"]+values["width"]/2,80)
            elif handle[0] > 0:
                self.assertAlmostEqual(values["x"]-values["width"]/2,20)
            else:
                self.assertEqual(values["width"],60)
            if handle[1] < 0:
                self.assertAlmostEqual(values["y"]+values["height"]/2,70)
            elif handle[1] > 0:
                self.assertAlmostEqual(values["y"]-values["height"]/2,30)
            else:
                self.assertEqual(values["height"],40)
            extreme = dragged_geometry(original,-999 if handle[0]>0 else 999,-999 if handle[1]>0 else 999,handle)
            self.assertGreaterEqual(extreme["width"],1)
            self.assertGreaterEqual(extreme["height"],1)
            self.assertLessEqual(extreme["width"],100)
            self.assertLessEqual(extreme["height"],100)

    def test_move_stays_inside_canvas(self):
        original = {"x":50,"y":50,"width":60,"height":40}
        self.assertEqual(dragged_geometry(original,999,999),{"x":70,"y":80,"width":60,"height":40})
        self.assertEqual(dragged_geometry(original,-999,-999),{"x":30,"y":20,"width":60,"height":40})

    def test_fractional_edges_do_not_underflow_minimum_size(self):
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        for fraction in (0.11111111,0.33333333,0.77777777):
            layer = self.layer()
            layer.update(x=47+fraction,y=41+fraction,width=56+fraction,height=25+fraction)
            for handle in ((1,1),(-1,-1),(1,-1),(-1,1)):
                values = dragged_geometry(layer,-999 if handle[0]>0 else 999,-999 if handle[1]>0 else 999,handle)
                self.assertGreaterEqual(values["width"],1)
                self.assertGreaterEqual(values["height"],1)
                normalize_layers([{**layer,**values}])

    def test_box_hit_testing_obeys_stack_and_selected_handles(self):
        preview = Preview()
        preview.resize(800,600)
        lower,upper = self.layer("body"),self.layer("title")
        preview.set_layers([lower,upper],"body")
        center = preview.layer_rect(upper).center()
        self.assertEqual(preview.hit_test(center)[0]["id"],"title")
        handle,rect = preview.handle_rects(lower)[0]
        hit,selected_handle = preview.hit_test(rect.center())
        self.assertEqual(hit["id"],"body")
        self.assertEqual(selected_handle,handle)
        self.assertEqual(preview.hit_test(QtCore.QPointF(1,1)),(None,None))
        upper["enabled"] = False
        preview.set_layers([lower,upper],"")
        self.assertEqual(preview.hit_test(center)[0]["id"],"body")

    def test_drag_uses_canvas_width_not_letterboxed_widget_width(self):
        preview = Preview()
        preview.resize(800,600)
        layer = self.layer()
        preview.set_layers([layer],"title")
        edited,committed = [],[]
        preview.geometryEdited.connect(lambda identifier,values:edited.append(values))
        preview.geometryCommitted.connect(lambda:committed.append(True))
        start = preview.layer_rect(layer).center()
        finish = start+QtCore.QPointF(preview.canvas_rect().width()*.1,0)
        preview.mousePressEvent(self.mouse_event(QtCore.QEvent.Type.MouseButtonPress,start))
        preview.mouseReleaseEvent(self.mouse_event(QtCore.QEvent.Type.MouseButtonRelease,finish))
        self.assertAlmostEqual(edited[-1]["x"],60)
        self.assertEqual(len(committed),1)

    def test_escape_restores_original_geometry(self):
        preview = Preview()
        preview.resize(600,600)
        layer = self.layer()
        preview.set_layers([layer],"title")
        start = preview.layer_rect(layer).center()
        preview.mousePressEvent(self.mouse_event(QtCore.QEvent.Type.MouseButtonPress,start))
        preview.mouseMoveEvent(self.mouse_event(QtCore.QEvent.Type.MouseMove,start+QtCore.QPointF(20,20)))
        self.assertNotEqual(preview.layers[0]["x"],layer["x"])
        preview.keyPressEvent(QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress,QtCore.Qt.Key.Key_Escape,QtCore.Qt.KeyboardModifier.NoModifier))
        self.assertIsNone(preview._drag)
        self.assertEqual(preview.layers[0],layer)

    def test_bounds_visible_by_default_for_all_boxes_not_baked_into_overlay(self):
        preview = Preview()
        preview.resize(360,640)
        layers = [self.layer("body"),self.layer("title")]
        layers[0]["y"] = 70
        preview.set_layers(layers,"")
        image,info = render_overlay("Title","Body",360,640,{**DEFAULTS,"layers":layers,"darkness":0})
        preview.overlay = image
        original = image.copy()
        preview.show()
        self.app.processEvents()
        with_bounds = preview.grab().toImage()
        preview.show_bounds = False
        preview.update()
        self.app.processEvents()
        self.assertNotEqual(preview.grab().toImage(),with_bounds)
        self.assertEqual(preview.overlay,original)
        exported,_ = render_overlay("Title","Body",360,640,{**DEFAULTS,"layers":layers,"darkness":0})
        self.assertEqual(exported,original)
        preview.close()

    def test_real_mouse_drag_updates_panel_preview_and_saved_state(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = fresh_state()
            state["jobs"] = [{"id":"a","path":"test.mp4","name":"test","title":"Title","body":"Body","status":"待生成","output":"","error":""}]
            state["settings"]["layers"] = [self.layer("body"),self.layer("title")]
            state["settings"]["layers"][0]["y"] = 70
            store.save(state,0)
            with patch.object(BatchTextVideoDialog,"request_background"):
                dialog = BatchTextVideoDialog(store=store)
                dialog.show()
                self.app.processEvents()
                layer = next(layer for layer in dialog.layer_panel.values() if layer["id"] == "title")
                start = dialog.preview.layer_rect(layer).center().toPoint()
                finish = start+QtCore.QPoint(round(dialog.preview.canvas_rect().width()*.07),round(dialog.preview.canvas_rect().height()*.03))
                QTest.mousePress(dialog.preview,QtCore.Qt.MouseButton.LeftButton,pos=start)
                QTest.mouseMove(dialog.preview,finish)
                QTest.mouseRelease(dialog.preview,QtCore.Qt.MouseButton.LeftButton,pos=finish)
                self.assertEqual(dialog.layer_panel.current()["id"],"title")
                current = dialog.layer_panel.current()
                self.assertGreater(current["x"],56)
                self.assertGreater(current["y"],32)
                self.assertAlmostEqual(dialog.layer_panel.properties["x"].value(),current["x"],places=3)
                saved = next(layer for layer in store.load()["settings"]["layers"] if layer["id"] == "title")
                self.assertEqual(current,saved)
                dialog.layer_panel.units.setCurrentIndex(1)
                dialog.persist()
                self.assertEqual(store.load()["settings"]["geometry_units"],"pixels")
                self.assertTrue(store.load()["settings"]["show_layer_bounds"])
                dialog.close()
                reopened = BatchTextVideoDialog(store=store)
                self.assertEqual(reopened.layer_panel.units.currentData(),"pixels")
                self.assertEqual(reopened.layer_panel.values(),dialog.layer_panel.values())
                reopened.close()

    def test_units_panel_fits_large_fonts_without_sideways_scroll(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = BatchTextVideoDialog(store=Store(folder))
            dialog.tabs.setCurrentIndex(dialog.layer_tab_index)
            dialog.layer_panel.property_tabs.setCurrentIndex(1)
            dialog.layer_panel.units.setCurrentIndex(1)
            font = QtGui.QFont(dialog.font())
            font.setPointSize(13)
            dialog.setFont(font)
            dialog.resize(1250,850)
            dialog.splitter.setSizes([320,440,420])
            dialog.show()
            self.app.processEvents()
            self.assertEqual(dialog.layer_scroll.horizontalScrollBar().maximum(),0)
            field = dialog.layer_panel.properties["width"]
            point = field.mapTo(dialog.layer_scroll.viewport(),QtCore.QPoint(field.width(),0))
            self.assertLessEqual(point.x(),dialog.layer_scroll.viewport().width())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
