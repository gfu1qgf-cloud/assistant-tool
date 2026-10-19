"""Control grouping and paired geometry regressions, without user data."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.geometry_ui import GeometryEditor
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.property_sections import PropertySection
from app_plugins.builtin.batch_text_video.caption_styles import effects
from app_plugins.builtin.batch_text_video.components import new_text_component
import test_caption_display_controls as fixtures


class CompactInspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def panel(self):
        layer=fixtures.caption()
        other=new_text_component(); other['id']='other'
        panel=LayerPanel([layer,other],'.'); panel.select_layer_id(layer['id'])
        self.addCleanup(panel.deleteLater)
        return panel,layer['id']

    def test_geometry_has_one_row_per_xy_pair(self):
        editor=GeometryEditor(); self.addCleanup(editor.deleteLater)
        editor.bind(fixtures.caption(),QtCore.QSize(1080,1920),'percent')
        form=editor.layout()
        self.assertEqual(form.rowCount(),5)  # units, reference, position, size, snap.
        self.assertEqual(form.labelForField(editor.anchor_pair).text(),'基准')
        self.assertEqual(form.labelForField(editor.size_pair).text(),'尺寸')
        for keys,pair,controls in ((('x','y'),editor.anchor_pair,editor.anchor_controls),
                (('x','y'),editor.position_pair,editor.properties),
                (('width','height'),editor.size_pair,editor.properties)):
            self.assertTrue(all(controls[key].parentWidget() is pair for key in keys))
        self.assertEqual(editor.position_label.text(),'中心')
        editor.anchor_controls['x'].setCurrentIndex(editor.anchor_controls['x'].findData('right'))
        self.assertEqual(editor.position_label.text(),'位置')
        editor.anchor_controls['y'].setCurrentIndex(editor.anchor_controls['y'].findData('top'))
        self.assertEqual(editor.position_label.text(),'边距')

    def test_rhythm_is_a_child_of_style_not_a_sibling_in_basic(self):
        panel,_=self.panel(); editor=panel.editor
        style=editor.sections['caption']; rhythm=editor.sections['caption_layout']
        self.assertEqual(rhythm.toggle.text(),'分组与节奏')
        self.assertTrue(style.content.isAncestorOf(rhythm))
        for key in ('caption_words','caption_per_line','caption_hold_ms','caption_accumulate'):
            self.assertTrue(rhythm.content.isAncestorOf(panel.caption_properties.controls[key]))
        self.assertIs(editor.targets['position'],editor.geometry_editor)
        self.assertIs(editor.targets['timing'],panel.caption_properties.basic_widget)
        self.assertTrue(editor.sections['basic'].content.isAncestorOf(editor.geometry_editor))

    def test_basic_font_fields_are_contiguous_before_any_folded_detail(self):
        panel,_=self.panel(); text=panel.text_properties; form=text.font_size_form
        fields=[text.controls['font'],text.controls['font_weight'],text.font_size_pair,
                panel.caption_properties.typography_widget,text.controls['color'],text.controls['color_mode'],
                panel.editor.caption_appearance,text.random_color_section,text.layout_section]
        rows=[form.getWidgetPosition(field)[0] for field in fields]
        self.assertEqual(rows,sorted(rows)); self.assertTrue(all(row>=0 for row in rows))
        self.assertIs(text.controls['font_min'].parentWidget(),text.font_size_pair)
        self.assertIs(text.controls['font_max'].parentWidget(),text.font_size_pair)
        appearance=panel.editor.caption_appearance
        for key in ('caption_text_gradient','word_highlight','caption_group_colors'):
            section=appearance.sections.sections[key]
            self.assertTrue(section.header.isHidden())
            self.assertTrue(section.toggle.isChecked())
            self.assertTrue(section.content.isAncestorOf(section.enable_control))
        sections=appearance.sections
        self.assertIs(sections.body.itemAt(sections.body.count()-1).widget(),sections.sections['word_highlight_box'])
        self.assertTrue(panel.editor.caption_background.sections.sections['caption_background'].header.isHidden())

    def test_compact_controls_keep_original_binding_after_reselection(self):
        panel,identifier=self.panel()
        panel.text_properties.controls['font_weight'].setCurrentIndex(
            panel.text_properties.controls['font_weight'].findData(700))
        panel.text_properties.controls['font_max'].setValue(80)
        panel.text_properties.controls['font_min'].setValue(50)
        panel.caption_properties.controls['caption_words'].setValue(6)
        panel.caption_properties.controls['caption_per_line'].setValue(4)
        panel.editor.geometry_editor.properties['x'].setValue(41)
        panel.editor.caption_appearance.controls['karaoke_highlight'].setChecked(True)
        panel.editor.caption_appearance.controls['color_high'].setText('#ff42a0')
        panel.select_layer_id('other'); panel.select_layer_id(identifier)
        for key,value in {'font_weight':700,'font_max':80,'font_min':50,
                          'caption_words':6,'caption_per_line':4,'x':41}.items():
            self.assertEqual(panel.current()[key],value)
        self.assertTrue(effects(panel.current())['karaoke_highlight'])
        self.assertEqual(effects(panel.current())['color_high'],'#ff42a0')

    def test_source_install_does_not_mix_old_font_ui_with_new_caption_composition(self):
        from app_plugins.builtin.batch_text_video.component_ui import TextBoxProperties
        from app_plugins.builtin.batch_text_video.caption_ui import caption_property_panel
        with patch.object(TextBoxProperties,'paired_font_fields',False),self.assertRaisesRegex(RuntimeError,'重启'):
            caption_property_panel()

    def test_narrow_inspector_pairs_fit_and_optional_native_screenshots(self):
        panel,_=self.panel(); panel.hide_catalog()
        panel.editor.sections['caption_layout'].set_expanded(True)
        scroll=QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(panel)
        self.addCleanup(scroll.deleteLater)
        scroll.resize(380,1000); scroll.show(); self.app.processEvents()
        self.assertEqual(scroll.horizontalScrollBar().maximum(),0)
        geometry=panel.editor.geometry_editor
        for pair in (geometry.anchor_pair,geometry.position_pair,geometry.size_pair):
            children=[pair.layout().itemAt(i).widget() for i in range(pair.layout().count())]
            self.assertTrue(all(child.geometry().left()>=0 and child.geometry().right()<pair.width() for child in children))
            self.assertTrue(all(a.geometry().right()<b.geometry().left() for a,b in zip(children,children[1:])),
                f'{pair.width()}: {[child.geometry().getRect() for child in children]}')
        output=os.environ.get('LZX_COMPACT_SCREENSHOT')
        if output:
            folder=Path(output); folder.mkdir(parents=True,exist_ok=True)
            scroll.resize(460,1080); self.app.processEvents()
            self.assertTrue(scroll.grab().save(str(folder/'caption-basic.png')))
            panel.editor.sections['basic'].set_expanded(False)
            panel.editor.sections['font'].set_expanded(True)
            self.app.processEvents(); scroll.verticalScrollBar().setValue(0)
            self.assertTrue(scroll.grab().save(str(folder/'caption-font.png')))
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            self.assertTrue(scroll.grab().save(str(folder/'caption-font-detail.png')))


if __name__=='__main__': unittest.main()
