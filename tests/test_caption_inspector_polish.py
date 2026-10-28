"""Small interaction regressions for the subtitle inspector's guidance controls."""
import unittest

from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.caption_effect_ui import CaptionEffectEditor
from app_plugins.builtin.batch_text_video.caption_basics_ui import CaptionIdentityEditor
from app_plugins.builtin.batch_text_video.color_ui import EffectColorEditor
from app_plugins.builtin.batch_text_video.geometry_ui import GeometryEditor
from test_caption_display_controls import caption


class CaptionInspectorPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_effect_titles_stay_plain_without_hiding_or_disabling_other_parameters(self):
        layer = caption('appear')
        layer['caption_effects']['anim_in_type'] = 'word_pop_random'
        editor = CaptionEffectEditor(layer)
        self.addCleanup(editor.deleteLater)
        self.assertEqual('逐词弹入', editor.groups['word_pop'].toggle.text())
        self.assertEqual('漂浮', editor.groups['floating'].toggle.text())
        floating = editor.controls['floating_amplitude']
        self.assertTrue(floating.isEnabled())
        self.assertFalse(editor.groups['floating'].isHidden())
        editor.controls['anim_in_type'].setCurrentIndex(
            editor.controls['anim_in_type'].findData('floating'))
        self.assertEqual('漂浮', editor.groups['floating'].toggle.text())
        self.assertEqual('逐词弹入', editor.groups['word_pop'].toggle.text())
        self.assertTrue(editor.controls['word_pop_random_duration'].isEnabled())

    def test_display_description_and_duration_target_follow_settings(self):
        layer = caption('appear', caption_display_mode='char')
        layer['caption_effects']['anim_in_type'] = 'word_pop_random'
        editor = CaptionProperties()
        self.addCleanup(editor.deleteLater)
        editor.set_layer(layer)
        self.assertIn('逐字', editor.display_note.text())
        self.assertIn('不是逐字语音对齐',editor.display_note.text())
        duration = editor.controls['caption_pop_ms']
        self.assertIn('逐词弹入', editor.form_for(duration).labelForField(duration).text())
        editor.controls['caption_display_mode'].setCurrentIndex(
            editor.controls['caption_display_mode'].findData('line'))
        self.assertIn('按行', editor.display_note.text())
        layer['caption_effects']['anim_in_type'] = 'letter_jump'
        editor.set_layer(layer)
        self.assertIn('逐词放大', editor.form_for(duration).labelForField(duration).text())

    def test_invalid_color_opens_raw_field_for_repair(self):
        editor = EffectColorEditor('#112233')
        self.addCleanup(editor.deleteLater)
        self.assertFalse(editor.advanced.isChecked())
        editor.setText('bad-color')
        self.assertTrue(editor.advanced.isChecked())
        self.assertFalse(editor.field.isHidden())
        editor.setText('#336699')
        self.assertTrue(editor.error.isHidden())

    def test_identity_is_paired_and_anchor_meaning_is_visible(self):
        layer = caption('appear')
        identity = CaptionIdentityEditor()
        geometry = GeometryEditor()
        self.addCleanup(identity.deleteLater)
        self.addCleanup(geometry.deleteLater)
        identity.set_layer(layer)
        self.assertIs(identity.identity_pair.labels['name'].buddy(), identity.name)
        self.assertIs(identity.identity_pair.labels['opacity'].buddy(), identity.opacity)
        geometry.bind(layer)
        self.assertIn('中心', geometry.geometry_labels['x'].text())
        geometry.snap_to_edges('left', 'top')
        self.assertIn('距左', geometry.geometry_labels['x'].text())
        self.assertIn('距上', geometry.geometry_labels['y'].text())


if __name__ == '__main__':
    unittest.main()
