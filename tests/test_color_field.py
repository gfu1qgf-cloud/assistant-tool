import os
from pathlib import Path
import unittest
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.color_field import ColorField
from app_plugins.builtin.batch_text_video.component_ui import TextBoxProperties
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.color_ui import EffectColorEditor
from app_plugins.builtin.batch_text_video.palette_rules import set_rule


class ColorFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_native_split_button_has_no_overlapping_child_and_keeps_raw_colour(self):
        panel=TextBoxProperties();self.addCleanup(panel.deleteLater)
        layer=normalize_layers([new_caption()])[0];layer.update(outline_width=10,outline_color='#8b0000',outline_position='outside')
        panel.set_layer(layer)
        section=panel.style_sections.sections['outline'];section.set_expanded(True)
        button=panel.controls['outline_color'];self.assertIsInstance(button,ColorField)
        self.assertEqual(button.text(),'#8b0000');self.assertEqual(button.findChildren(QtWidgets.QToolButton),[])
        self.assertEqual(len(button.menu().actions()),5)
        self.assertIn('固定颜色',button.menu().actions()[0].text())
        for width in (300,420,680):
            panel.resize(width,800);panel.show();self.app.processEvents()
            self.assertGreaterEqual(button.width(),button.minimumSizeHint().width())
        root=os.environ.get('LZX_CAPTION_PREVIEW_QA')
        if root:
            section.resize(500,section.sizeHint().height());section.grab().save(str(Path(root)/'color-field-fixed.png'))
        rule=dict(mode='palette',colors=['#ff8800','#ffaa88','#fff5e4'],name='暖橙与珊瑚',seed=0,alpha=False)
        set_rule(panel.layer,'outline_color',rule);button.update();self.app.processEvents()
        self.assertIn('暖橙',button.display_text());self.assertEqual(button.text(),'#8b0000')
        if root:section.grab().save(str(Path(root)/'color-field-palette.png'))
        panel.close()

    def test_main_colour_and_menu_fixed_picker_use_original_commit_and_clear_random(self):
        panel=TextBoxProperties();self.addCleanup(panel.deleteLater)
        panel.set_layer(normalize_layers([new_caption()])[0]);changes=[];panel.changed.connect(changes.append)
        set_rule(panel.layer,'outline_color',dict(mode='palette',colors=['red','blue'],seed=0,alpha=False))
        with patch.object(QtWidgets.QColorDialog,'getColor',return_value=QtGui.QColor('#123456')):
            panel.controls['outline_color'].menu().actions()[0].trigger()
        self.assertEqual(changes[-1]['outline_color'],'#123456');self.assertNotIn('outline_color',changes[-1]['color_rules'])

    def test_effect_gradient_colours_use_same_control_with_accessible_menu(self):
        editor=EffectColorEditor('#ff0000,#0000ff');self.addCleanup(editor.deleteLater)
        self.assertEqual(len(editor.buttons),2)
        self.assertTrue(all(isinstance(button,ColorField) and button.menu() for button in editor.buttons))
        with patch.object(editor,'pick',return_value='#ffffff'):editor.buttons[0].click()
        self.assertEqual(editor.text(),'#ffffff,#0000ff')
