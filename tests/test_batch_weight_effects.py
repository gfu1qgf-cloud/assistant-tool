import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video.component_ui import TextBoxProperties
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.layers import componentize_layers, normalize_layers
from app_plugins.builtin.batch_text_video.layout import _text_layout, ensure_fonts
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.store import DEFAULTS, Store, fresh_state
from app_plugins.builtin.batch_text_video.pools import RESOURCE_KEYS, new_copy, snapshot
from app_plugins.builtin.batch_text_video.components import new_text_component


class WeightAndEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layer(self):
        layer = next(item for item in componentize_layers(None, DEFAULTS) if item["id"] == "title")
        layer.update(text="Hope and peace", font="Segoe UI", x=50, y=50,
                     width=90, height=80, font_min=60, font_max=60)
        return layer

    def test_integer_weights_are_preserved_by_schema_and_renderer(self):
        for value in (1, 100, 450, 650, 925, 1000):
            with self.subTest(value=value):
                layer = normalize_layers([{**self.layer(), "font_weight": value}])[0]
                self.assertEqual(layer["font_weight"], value)
                groups, _ = _text_layout(layer["text"], layer["font"], 60, 500,
                                          "left", 1.15, layer)
                self.assertEqual(int(groups[0][0].font().weight()), value)
                image, size = render_text_box(layer, 540, 640)
                self.assertFalse(image.isNull())
                self.assertGreater(size, 0)

    def test_invalid_weight_types_and_ranges_are_rejected(self):
        for value in (True, False, 0, -1, 1001, 650.0, 650.5, "650", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_layers([{**self.layer(), "font_weight": value}])

    def test_typing_exact_weight_edits_only_selected_text_box(self):
        panel = LayerPanel(componentize_layers(None, DEFAULTS), ".")
        panel.rebuild("title")
        weight = panel.text_properties.controls["font_weight"]
        self.assertTrue(weight.isEditable())
        self.assertEqual(weight.count(), 9)
        weight.lineEdit().selectAll()
        QTest.keyClicks(weight.lineEdit(), "650")
        self.assertEqual(weight.currentData(), 650)
        self.assertEqual(panel.current()["font_weight"], 650)
        self.assertEqual(next(item for item in panel.values() if item["id"] == "body")["font_weight"], 400)
        panel.rebuild("body")
        panel.rebuild("title")
        self.assertEqual(weight.currentText(), "650")
        self.assertEqual(weight.currentData(), 650)
        weight.setCurrentIndex(weight.findData(300))
        self.assertEqual(panel.current()["font_weight"], 300)

    def test_incomplete_or_invalid_edit_does_not_clear_saved_weight(self):
        properties = TextBoxProperties()
        properties.set_layer({**self.layer(), "font_weight": 650})
        weight = properties.controls["font_weight"]
        for text in ("", "0", "1001", "650.5", "invalid"):
            with self.subTest(text=text):
                weight.setEditText(text)
                self.assertIsNone(weight.currentData())
                self.assertEqual(properties.layer["font_weight"], 650)
                properties.finish_weight_edit()
                self.assertEqual(weight.currentData(), 650)
                self.assertEqual(properties.layer["font_weight"], 650)

    def test_enter_in_weight_editor_never_activates_default_dialog_button(self):
        dialog = QtWidgets.QDialog()
        layout = QtWidgets.QVBoxLayout(dialog)
        properties = TextBoxProperties()
        properties.set_layer(self.layer())
        layout.addWidget(properties)
        export = QtWidgets.QPushButton("Export")
        export.setDefault(True)
        clicks = []
        export.clicked.connect(lambda: clicks.append(True))
        layout.addWidget(export)
        dialog.show()
        weight = properties.controls["font_weight"]
        weight.lineEdit().setFocus()
        weight.lineEdit().selectAll()
        QTest.keyClicks(weight.lineEdit(), "925")
        for key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            QTest.keyClick(weight.lineEdit(), key)
        self.app.processEvents()
        self.assertEqual(clicks, [])
        self.assertEqual(properties.layer["font_weight"], 925)
        dialog.close()

    def test_effect_controls_have_clear_pages_and_restore_colors_and_values(self):
        properties = TextBoxProperties()
        self.assertEqual([properties.style_tabs.tabText(i) for i in range(4)],
                         ["字体", "描边", "阴影", "底色"])
        layer = {**self.layer(), "outline_width": 6.5, "outline_color": "#ff00ff",
                 "outline_opacity": 50, "shadow_enabled": True, "shadow_color": "#00ffff",
                 "shadow_opacity": 75, "shadow_x": -18, "shadow_y": 25}
        properties.set_layer(layer)
        for key in ("outline_width", "outline_opacity", "shadow_opacity", "shadow_x", "shadow_y"):
            self.assertEqual(properties.controls[key].value(), layer[key])
        for key in ("color", "outline_color", "shadow_color", "background_color"):
            self.assertFalse(properties.controls[key].icon().isNull())
        properties.style_tabs.setCurrentIndex(1)
        properties.show()
        self.app.processEvents()
        self.assertTrue(properties.controls["outline_width"].isVisible())
        self.assertTrue(properties.controls["outline_color"].isVisible())
        properties.style_tabs.setCurrentIndex(2)
        self.assertTrue(properties.controls["shadow_enabled"].isVisible())
        self.assertTrue(properties.controls["shadow_color"].isEnabled())
        properties.controls["shadow_enabled"].setChecked(False)
        self.assertFalse(properties.controls["shadow_color"].isEnabled())
        properties.close()

    def test_color_picker_applies_non_black_outline_and_shadow(self):
        properties = TextBoxProperties()
        properties.set_layer(self.layer())
        properties.controls["shadow_enabled"].setChecked(True)
        for key, color in (("outline_color", "#ff00ff"), ("shadow_color", "#00ffff")):
            with patch.object(QtWidgets.QColorDialog, "getColor", return_value=QtGui.QColor(color)):
                properties.controls[key].click()
            self.assertEqual(properties.layer[key], color)
        with patch.object(QtWidgets.QColorDialog, "getColor", return_value=QtGui.QColor()):
            properties.controls["outline_color"].click()
        self.assertEqual(properties.layer["outline_color"], "#ff00ff")

    def test_new_components_default_to_dark_red_without_recoloring_old_styles(self):
        for layer in componentize_layers(None, DEFAULTS):
            self.assertEqual(layer["outline_color"], "#8b0000")
        self.assertEqual(normalize_layers([new_text_component()])[0]["outline_color"], "#8b0000")
        for color in ("#000000", "#aabbcc"):
            old = normalize_layers([{**self.layer(), "outline_color": color}])[0]
            self.assertEqual(old["outline_color"], color)

    def test_outline_and_shadow_parameters_change_actual_pixels_not_only_preview(self):
        layer = self.layer()
        original, _ = render_text_box(layer, 540, 640)
        for settings in ({"outline_width": 0}, {"outline_width": 10},
                         {"outline_color": "#ff00ff"}, {"outline_opacity": 30},
                         {"shadow_enabled": True, "shadow_color": "#00ffff", "shadow_x": 30, "shadow_y": 20}):
            with self.subTest(settings=settings):
                changed, _ = render_text_box({**layer, **settings}, 540, 640)
                self.assertNotEqual(original, changed)
        shadow = {**layer, "shadow_enabled": True, "shadow_x": 30, "shadow_y": 20}
        before, _ = render_text_box(shadow, 540, 640)
        for settings in ({"shadow_color": "#00ffff"}, {"shadow_opacity": 20},
                         {"shadow_x": -30}, {"shadow_y": -20}):
            with self.subTest(settings=settings):
                after, _ = render_text_box({**shadow, **settings}, 540, 640)
                self.assertNotEqual(before, after)

    def test_profiles_isolate_all_pools_queue_styles_and_progress_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            state = fresh_state()
            state["settings"]["layers"] = componentize_layers(None, state["settings"])
            title = next(layer for layer in state["settings"]["layers"] if layer["id"] == "title")
            title.update(font_weight=650, outline_color="#ff00ff", outline_width=6.5,
                         shadow_enabled=True, shadow_color="#00ffff", shadow_x=-18, shadow_y=25)
            state["backgrounds"] = [str(Path(root)/"first.mp4")]
            state["copy_pool"] = [{**new_copy(), "title": "First title", "body": "First body"}]
            state["image_pool"] = [{"id": "i", "path": "first.png", "name": "First image"}]
            state["music"] = [{"id": "m", "path": "first.mp3", "cursor": 23}]
            state["next_music_id"] = "m"
            state["jobs"] = [{"id": "j", "path": state["backgrounds"][0], "status": "已完成", "output": "out.mp4"}]
            first_snapshot = snapshot(state)
            state = store.save(state, 0)
            first = state["active_profile"]
            clone = store.create_profile(state, "副本", clone=True)
            clone_id = clone["active_profile"]
            self.assertEqual(snapshot(clone), first_snapshot)
            clone["backgrounds"].clear()
            clone["copy_pool"][0]["title"] = "Changed"
            clone["image_pool"].clear()
            clone["music"][0]["cursor"] = 99
            clone["next_music_id"] = ""
            clone["jobs"].clear()
            next(layer for layer in clone["settings"]["layers"] if layer["id"] == "title")["font_weight"] = 925
            clone_snapshot = snapshot(clone)
            clone = store.save(clone, clone["revision"])
            reopened = Store(root)
            first_state = reopened.select_profile(reopened.load(), first)
            self.assertEqual(snapshot(first_state), first_snapshot)
            second_state = reopened.select_profile(first_state, clone_id)
            self.assertEqual(snapshot(second_state), clone_snapshot)
            empty = reopened.create_profile(second_state, "空配置", clone=False)
            for key in RESOURCE_KEYS:
                if key not in {"settings", "next_music_id"}:
                    self.assertEqual(empty[key], [])
            self.assertEqual(empty["next_music_id"], "")
            again = reopened.select_profile(empty, first)
            self.assertEqual(snapshot(again), first_snapshot)


if __name__ == "__main__":
    unittest.main()
