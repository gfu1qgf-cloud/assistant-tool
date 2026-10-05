import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.layers import componentize_layers,normalize_layers,compose_layers,TEXT_STYLE_DEFAULTS
from app_plugins.builtin.batch_text_video.layout import ensure_fonts,render_overlay,_text_layout,TextDoesNotFit
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.store import Store,DEFAULTS,fresh_state
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog


class TextStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layer(self):
        layer = next(layer for layer in componentize_layers(None,DEFAULTS) if layer["id"] == "title")
        layer.update(text="Hope and peace\nFor today",font="Segoe UI",x=50,y=50,width=90,height=80,
            font_min=60,font_max=60,align="left",vertical="top")
        return layer

    def test_old_components_gain_neutral_defaults_and_keep_rendering(self):
        layer = self.layer()
        legacy = {key:value for key,value in layer.items() if key not in TEXT_STYLE_DEFAULTS}
        before,_ = render_text_box(legacy,540,640)
        after,_ = render_text_box(normalize_layers([legacy])[0],540,640)
        self.assertEqual(before,after)

    def test_bold_button_and_nine_weights_edit_only_selected_component(self):
        panel = LayerPanel(componentize_layers(None,DEFAULTS),".")
        panel.rebuild("title")
        controls = panel.text_properties.controls
        self.assertEqual(controls["font_weight"].count(),9)
        panel.text_properties.bold.click()
        self.assertEqual(panel.current()["font_weight"],700)
        controls["font_weight"].setCurrentIndex(controls["font_weight"].findData(900))
        self.assertEqual(panel.current()["font_weight"],900)
        self.assertTrue(panel.text_properties.bold.isChecked())
        controls["italic"].setChecked(True)
        controls["underline"].setChecked(True)
        self.assertTrue(panel.current()["italic"])
        self.assertTrue(panel.current()["underline"])
        body = next(layer for layer in panel.values() if layer["id"] == "body")
        self.assertEqual(body["font_weight"],400)
        self.assertFalse(body["italic"])
        panel.text_properties.bold.click()
        self.assertEqual(panel.current()["font_weight"],400)

    def test_each_visual_font_or_effect_setting_really_changes_rendered_pixels(self):
        layer = self.layer()
        original,_ = render_text_box(layer,540,640)
        options = ({"font_weight":700},{"italic":True},{"underline":True},{"strikeout":True},
            {"capitalization":"uppercase"},{"capitalization":"lowercase"},{"letter_spacing":5},
            {"word_spacing":20},{"paragraph_spacing":35},{"outline_width":0},
            {"outline_color":"#ff0000","outline_width":8},
            {"shadow_enabled":True,"shadow_color":"#00ff00","shadow_x":20,"shadow_y":20},
            {"background_enabled":True,"background_color":"#ff0000","background_opacity":80},
            {"padding":40})
        for settings in options:
            with self.subTest(settings=settings):
                image,_ = render_text_box({**layer,**settings},540,640)
                self.assertNotEqual(image,original)

    def test_spacing_participates_in_measurement_not_just_painting(self):
        _,height = _text_layout("word word word word word word","Segoe UI",48,250,"left",1.15)
        _,spaced = _text_layout("word word word word word word","Segoe UI",48,250,"left",1.15,{"letter_spacing":10})
        self.assertGreater(spaced,height)
        _,height = _text_layout("one\ntwo\nthree","Segoe UI",48,250,"left",1.15)
        _,spaced = _text_layout("one\ntwo\nthree","Segoe UI",48,250,"left",1.15,{"paragraph_spacing":15})
        self.assertAlmostEqual(spaced-height,30)

    def test_background_tint_is_below_images_and_text_and_zero_is_neutral(self):
        with tempfile.TemporaryDirectory() as folder:
            source = QtGui.QImage(100,100,QtGui.QImage.Format.Format_ARGB32)
            source.fill(QtGui.QColor("#0000ff"))
            path = Path(folder)/"blue.png"
            source.save(str(path),"PNG")
            layers = normalize_layers([{"id":"photo","kind":"image","name":"Image","path":str(path),
                "x":50,"y":50,"width":25,"height":25}])
            blank = QtGui.QImage(200,200,QtGui.QImage.Format.Format_ARGB32)
            blank.fill(QtCore.Qt.GlobalColor.transparent)
            plain = compose_layers({"title":blank,"body":blank},layers,200,200,0)
            neutral = compose_layers({"title":blank,"body":blank},layers,200,200,0,tint_color="#ffff00",tint_strength=0)
            self.assertEqual(plain,neutral)
            tinted = compose_layers({"title":blank,"body":blank},layers,200,200,0,tint_color="#ff0000",tint_strength=50)
            self.assertEqual(tinted.pixelColor(100,100).name(),"#0000ff")
            self.assertEqual(tinted.pixelColor(10,10).name(),"#ff0000")
            self.assertGreater(tinted.pixelColor(10,10).alpha(),120)

    def test_invalid_style_values_fail_validation_instead_of_qt_crashing(self):
        for options in ({"font_weight":1001},{"letter_spacing":float("nan")},{"shadow_enabled":"true"},
                        {"outline_width":-1},{"background_color":"not-a-color"}):
            with self.subTest(options=options),self.assertRaises(ValueError):
                normalize_layers([{**self.layer(),**options}])

    def test_effects_and_tint_persist_and_preset_keeps_existing_darkness(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            dialog = BatchTextVideoDialog(store=store)
            panel = dialog.layer_panel
            panel.rebuild("title")
            controls = panel.text_properties.controls
            controls["shadow_enabled"].setChecked(True)
            controls["shadow_x"].setValue(-12)
            controls["background_enabled"].setChecked(True)
            controls["background_opacity"].setValue(45)
            controls["font_weight"].setCurrentIndex(controls["font_weight"].findData(800))
            darkness = dialog.controls["darkness"].value()
            dialog._apply_tint_preset(2)
            self.assertEqual(dialog.controls["darkness"].value(),darkness)
            self.assertGreater(dialog.controls["background_tint_strength"].value(),0)
            self.assertTrue(dialog.persist())
            saved = store.load()
            title = next(layer for layer in saved["settings"]["layers"] if layer["id"] == "title")
            self.assertEqual(title["font_weight"],800)
            self.assertEqual(title["shadow_x"],-12)
            self.assertEqual(title["background_opacity"],45)
            self.assertEqual(saved["settings"]["background_tint_color"],"#c88236")
            dialog.close()
            reopened = BatchTextVideoDialog(store=store)
            reopened.layer_panel.rebuild("title")
            self.assertEqual(reopened.layer_panel.text_properties.controls["font_weight"].currentData(),800)
            self.assertTrue(reopened.layer_panel.text_properties.controls["shadow_enabled"].isChecked())
            reopened.close()

    def test_effect_pages_fit_large_font_without_sideways_scroll(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = BatchTextVideoDialog(store=Store(folder))
            dialog.tabs.setCurrentIndex(dialog.layer_tab_index)
            panel = dialog.layer_panel
            panel.property_tabs.setCurrentIndex(0)
            font = QtGui.QFont(dialog.font())
            font.setPointSize(13)
            dialog.setFont(font)
            dialog.resize(1250,850)
            dialog.splitter.setSizes([320,450,400])
            dialog.show()
            for page in range(panel.text_properties.style_tabs.count()):
                panel.text_properties.style_tabs.setCurrentIndex(page)
                self.app.processEvents()
                self.assertEqual(dialog.layer_scroll.horizontalScrollBar().maximum(),0)
            dialog.close()


if __name__ == "__main__":
    unittest.main()
