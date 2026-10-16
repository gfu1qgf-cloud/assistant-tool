"""Selection must not act like a geometry edit or restore an old snapshot."""
import copy
import gc
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video.preview import Preview
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.preview import dragged_geometry, HANDLES
from app_plugins.builtin.batch_text_video.visual_media import add_plane


class ImageSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        for widget in self.app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()
        gc.collect()

    def image_layer(self, **values):
        return normalize_layers([dict(id='picture', kind='image', source='file',
            path='picture.png', x=50, y=50, width=30, height=30, **values)])[0]

    def event(self, kind, point):
        return QtGui.QMouseEvent(kind, point, point, QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)

    def test_select_partly_offscreen_image_does_not_clamp_its_position(self):
        preview = Preview()
        preview.resize(360, 640)
        edits, commits = [], []
        preview.geometryEdited.connect(lambda *value: edits.append(value))
        preview.geometryCommitted.connect(lambda: commits.append(True))
        for x, y in ((0, 50), (100, 50), (50, 0), (50, 100), (3, 7)):
            layer = self.image_layer()
            layer.update(x=x, y=y)
            preview.set_layers([layer])
            before = copy.deepcopy(preview.layers)
            point = preview.layer_rect(layer).intersected(preview.canvas_rect()).center()
            preview.mousePressEvent(self.event(QtCore.QEvent.Type.MouseButtonPress, point))
            preview.mouseReleaseEvent(self.event(QtCore.QEvent.Type.MouseButtonRelease, point))
            self.assertEqual(preview.layers, before, (x, y))
        self.assertEqual(edits, [])
        self.assertEqual(commits, [])

    def test_selection_callback_cannot_leave_drag_using_stale_layer(self):
        preview = Preview()
        preview.resize(360, 640)
        layer = self.image_layer()
        preview.set_layers([layer])
        updated = {**layer, 'x': 55, 'y': 57, 'width': 40}
        preview.layerSelected.connect(lambda identifier: preview.set_layers([updated], identifier))
        point = preview.layer_rect(layer).center()
        preview.mousePressEvent(self.event(QtCore.QEvent.Type.MouseButtonPress, point))
        end = point + QtCore.QPointF(36, 0)
        preview.mouseReleaseEvent(self.event(QtCore.QEvent.Type.MouseButtonRelease, end))
        self.assertEqual(preview.layers[0]['x'], 65)
        self.assertEqual(preview.layers[0]['y'], 57)
        self.assertEqual(preview.layers[0]['width'], 40)

    def test_small_click_jitter_does_not_move_or_clamp(self):
        preview = Preview()
        preview.resize(360, 640)
        layer = self.image_layer()
        layer.update(x=0, y=50)
        preview.set_layers([layer])
        point = preview.layer_rect(layer).intersected(preview.canvas_rect()).center()
        preview.mousePressEvent(self.event(QtCore.QEvent.Type.MouseButtonPress, point))
        preview.mouseReleaseEvent(self.event(QtCore.QEvent.Type.MouseButtonRelease, point + QtCore.QPointF(1, 1)))
        self.assertEqual(preview.layers, [layer])

    def test_full_frame_image_drag_preserves_intentionally_offscreen_other_axis(self):
        preview = Preview()
        preview.resize(540, 960)
        layer = self.image_layer()
        layer.update(x=50, y=60, width=100, height=100)
        preview.set_layers([layer])
        point = preview.layer_rect(layer).center()
        preview.mousePressEvent(self.event(QtCore.QEvent.Type.MouseButtonPress, point))
        end = point + QtCore.QPointF(preview.canvas_rect().width() * .05, 0)
        preview.mouseReleaseEvent(self.event(QtCore.QEvent.Type.MouseButtonRelease, end))
        self.assertAlmostEqual(preview.layers[0]['x'], 55)
        self.assertAlmostEqual(preview.layers[0]['y'], 60)

    def test_visual_size_above_one_hundred_roundtrips_units_selection_and_store(self):
        with tempfile.TemporaryDirectory() as folder:
            layer = self.image_layer()
            panel = LayerPanel([layer], folder)
            panel.select_layer_id('picture')
            panel.set_canvas_size(1080, 1920)
            self.assertEqual(panel.current()['id'], 'picture')
            self.assertEqual(panel.properties['width'].maximum(), 400)
            panel.properties['width'].setValue(105)
            self.assertEqual(panel.current()['width'], 105)
            panel.properties['height'].setValue(110)
            self.assertEqual(panel.current()['width'], 105)
            expected = panel.values()
            panel.units.setCurrentIndex(1)
            self.assertAlmostEqual(panel.properties['width'].value(), 1134)
            self.assertAlmostEqual(panel.properties['height'].value(), 2112)
            panel.select_layer_id('picture')
            panel.units.setCurrentIndex(0)
            self.assertEqual(panel.values(), expected)
            self.assertEqual(panel.properties['width'].maximum(), 400)
            state = fresh_state()
            state['settings']['layers'] = panel.values()
            store = Store(folder)
            store.save(state, 0)
            self.assertEqual(store.load()['settings']['layers'], expected)

    def test_visual_corner_resize_can_cross_canvas_without_snapping(self):
        layer = self.image_layer()
        layer.update(x=50, y=60, width=100, height=100)
        result = dragged_geometry(layer, 10, 0, (1, 0), max_size=400)
        self.assertEqual(result, dict(x=55, y=60, width=110, height=100))
        for x in (0, 50, 100):
            for size in (1, 105, 250, 400):
                for handle in HANDLES:
                    for delta in (-999, 999):
                        values = dragged_geometry({**layer, 'x':x, 'width':size, 'height':size},
                            delta, delta, handle, max_size=400)
                        normalize_layers([{**layer, **values}])
                        self.assertLessEqual(values['width'], 400)
                        self.assertLessEqual(values['height'], 400)

    def test_extreme_visual_scale_is_rejected_before_native_allocation(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'image.png'
            image.touch()
            layer = self.image_layer()
            layer.update(path=str(image), width=400, height=400)
            with self.assertRaisesRegex(ValueError, '像素过大'):
                add_plane([], [], layer, 1, 'background', 'output', 8192, 4096, 10, 30)

    def test_focus_out_commits_before_drag_snapshot_is_captured(self):
        root = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(root)
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(0, 100)
        spin.setKeyboardTracking(False)
        spin.setValue(50)
        preview = Preview()
        preview.resize(360, 640)
        layer = self.image_layer()
        preview.set_layers([layer])
        spin.valueChanged.connect(lambda value: preview.set_layers([{**layer, 'x': value}]))
        layout.addWidget(spin)
        layout.addWidget(preview)
        root.resize(380, 720)
        root.show()
        root.activateWindow()
        spin.setFocus()
        self.app.processEvents()
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin.lineEdit(), '55')
        self.assertEqual(spin.value(), 50)
        point = preview.layer_rect(layer).center()
        preview.mousePressEvent(self.event(QtCore.QEvent.Type.MouseButtonPress, point))
        end = point + QtCore.QPointF(preview.canvas_rect().width() * .1, 0)
        preview.mouseReleaseEvent(self.event(QtCore.QEvent.Type.MouseButtonRelease, end))
        self.assertAlmostEqual(spin.value(), 55)
        self.assertAlmostEqual(preview.layers[0]['x'], 65)

    def test_dialog_position_survives_canvas_click_layer_switch_and_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'picture.png'
            image = QtGui.QImage(100, 100, QtGui.QImage.Format.Format_ARGB32)
            image.fill(QtGui.QColor('#c73b58'))
            self.assertTrue(image.save(str(path)))
            store = Store(folder)
            state = fresh_state()
            layer = self.image_layer()
            layer['path'] = str(path)
            state['settings']['layers'].append(layer)
            store.save(state, 0)
            with patch.object(BatchTextVideoDialog, 'request_background'), patch.object(
                    BatchTextVideoDialog, '_start_overlay'):
                dialog = BatchTextVideoDialog(store=store)
                dialog.show()
                self.app.processEvents()
                panel = dialog.layer_panel
                panel.select_layer_id('picture')
                for x, y in ((0, 50), (97, 80), (40, 25)):
                    panel.properties['x'].setValue(x)
                    panel.properties['y'].setValue(y)
                    before = copy.deepcopy(panel.current())
                    point = dialog.preview.layer_rect(before).intersected(
                        dialog.preview.canvas_rect()).center().toPoint()
                    QTest.mouseClick(dialog.preview, QtCore.Qt.MouseButton.LeftButton, pos=point)
                    self.assertEqual(panel.current(), before)
                    dialog._edit_changed()
                    panel.select_layer_id('title')
                    panel.select_layer_id('picture')
                    self.assertEqual(panel.current(), before)
                    panel.units.setCurrentIndex(1)
                    panel.units.setCurrentIndex(0)
                    panel.reveal_property('visual')
                    panel.visual_properties.controls['key_mode'].setCurrentIndex(1)
                    panel.visual_properties.controls['key_mode'].setCurrentIndex(0)
                    self.assertEqual(panel.current(), before)
                    saved = next(value for value in store.load()['settings']['layers']
                                 if value['id'] == 'picture')
                    self.assertEqual(saved, before)
                latest = panel.values()
                dialog.close()
                reopened = BatchTextVideoDialog(store=store)
                self.assertEqual(reopened.layer_panel.values(), latest)
                reopened.close()
