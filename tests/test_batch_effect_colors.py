import copy
import gc
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.caption_ui import EffectDialog
from app_plugins.builtin.batch_text_video.color_ui import EffectColorEditor
from app_plugins.builtin.batch_text_video.caption_styles import COLOR_KEYS, PREFIX, colors, effects
from app_plugins.builtin.batch_text_video.caption_style_catalog import PRESETS
from app_plugins.builtin.batch_text_video.captions import new_caption


class EffectColorTests(unittest.TestCase):
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

    def test_color_button_opens_picker_and_preserves_alpha(self):
        editor = EffectColorEditor('#faebdb')
        self.assertTrue(editor.field.isHidden())
        with patch.object(QtWidgets.QColorDialog, 'getColor', return_value=QtGui.QColor('#806d2540')) as choose:
            editor.buttons[0].click()
        self.assertEqual(colors(editor.text()), ['#806d2540'])
        self.assertTrue(choose.call_args.args[-1] & QtWidgets.QColorDialog.ColorDialogOption.ShowAlphaChannel)
        self.assertFalse(editor.buttons[0].icon().isNull())
        self.assertFalse(editor.buttons[0].autoDefault())

    def test_cancel_picker_does_not_change_existing_value(self):
        editor = EffectColorEditor('red,#123456')
        before = editor.text()
        with patch.object(QtWidgets.QColorDialog, 'getColor', return_value=QtGui.QColor()):
            editor.buttons[0].click()
            editor.add_button.click()
        self.assertEqual(editor.text(), before)

    def test_gradient_colors_add_remove_order_and_limit(self):
        editor = EffectColorEditor(['#cc2266', '#77ccaa'])
        with patch.object(QtWidgets.QColorDialog, 'getColor', return_value=QtGui.QColor('#5577cc')):
            editor.add_button.click()
        self.assertEqual(colors(editor.text()), ['#cc2266', '#77ccaa', '#5577cc'])
        editor.remove_color(1)
        self.assertEqual(colors(editor.text()), ['#cc2266', '#5577cc'])
        editor.setText(','.join(['#5577cc'] * 8))
        self.assertFalse(editor.add_button.isEnabled())
        with patch.object(QtWidgets.QColorDialog, 'getColor') as choose:
            editor.add_color()
            choose.assert_not_called()
        for _ in range(9):
            editor.remove_color(0)
        self.assertEqual(colors(editor.text()), ['#5577cc'])

    def test_optional_raw_input_cannot_silently_replace_invalid_data(self):
        editor = EffectColorEditor('#112233')
        editor.advanced.click()
        self.assertFalse(editor.field.isHidden())
        editor.setText('not-a-color')
        self.assertEqual(editor.text(), 'not-a-color')
        self.assertFalse(editor.error.isHidden())
        self.assertFalse(editor.add_button.isEnabled())
        editor.setText('#33aabb')
        self.assertTrue(editor.error.isHidden())
        self.assertEqual(len(editor.buttons), 1)

    def test_all_catalog_effect_color_fields_are_visual_and_roundtrip(self):
        for name in PRESETS:
            with self.subTest(preset=name):
                layer = new_caption()
                layer.update(caption_preset=PREFIX + name)
                before = copy.deepcopy(layer)
                dialog = EffectDialog(layer)
                for key in COLOR_KEYS & dialog.controls.keys():
                    self.assertIsInstance(dialog.controls[key], EffectColorEditor)
                result = effects({**layer, 'caption_effects': dialog.value()})
                original = effects(layer)
                for key in dialog.controls:
                    if key in COLOR_KEYS:
                        self.assertEqual(colors(result[key]), colors(original[key]))
                self.assertEqual(layer, before)
                dialog.reject()
                dialog.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_picked_color_becomes_effect_override_not_task_or_timing_change(self):
        from app_plugins.builtin.batch_text_video.caption_appearance_ui import CaptionAppearanceEditor
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        layer = new_caption()
        layer.update(caption_preset=PREFIX + next(iter(PRESETS)))
        layer = normalize_layers([layer])[0]
        before = copy.deepcopy(layer)
        dialog = CaptionAppearanceEditor(); dialog.set_layer(layer)
        changes=[]; dialog.changed.connect(changes.append)
        editor = dialog.controls['color_high']
        with patch.object(QtWidgets.QColorDialog, 'getColor', return_value=QtGui.QColor('#c31e6c')):
            editor.buttons[0].click()
        self.assertEqual(effects({**layer, **changes[-1]})['color_high'], '#c31e6c')
        self.assertEqual(layer, before)

    def test_word_color_and_highlight_box_are_independent_controls(self):
        from app_plugins.builtin.batch_text_video.caption_appearance_ui import CaptionAppearanceEditor
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        layer = new_caption()
        layer.update(caption_preset=PREFIX + next(iter(PRESETS)),
            caption_effects={'karaoke_highlight': False, 'dynamic_box': False})
        layer=normalize_layers([layer])[0]
        dialog = CaptionAppearanceEditor(); dialog.set_layer(layer)
        changes=[]; dialog.changed.connect(changes.append)
        dialog.controls['karaoke_highlight'].setChecked(True)
        dialog.controls['color_high'].setText('#dc3574')
        style = effects({**layer, **changes[-1]})
        self.assertTrue(style['karaoke_highlight'])
        self.assertFalse(style['dynamic_box'])
        self.assertEqual(style['color_high'], '#dc3574')
