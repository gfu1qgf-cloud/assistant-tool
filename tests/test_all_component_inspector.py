"""A shared flat source, compact basic fields, and no nested watermarks tabs."""
import copy
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.batch_text_video.scrolling_text import new_scrolling_component
from app_plugins.builtin.batch_text_video.watermarks import new_watermark
from app_plugins.builtin.batch_text_video.node_effects import new_effect
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.property_sections import PropertySection
from app_plugins.builtin.batch_text_video.store import fresh_state


class FlatSourceInspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def layers(self):
        values=fresh_state()['settings']['layers']
        values.extend([new_text_component(),new_scrolling_component(),new_caption(),new_watermark(),new_effect(),
            dict(id='visual',kind='image',name='图片／视频',enabled=True,opacity=100,source='fixed',path='sample.mov',
                 x=50,y=50,width=55,height=55)])
        return values

    def panel(self):
        library=SimpleNamespace(list=lambda _kind:[dict(id='a'*32,name='已保存效果')])
        panel=LayerPanel(self.layers(),'.',services=SimpleNamespace(workflow_library=library))
        self.addCleanup(panel.deleteLater)
        return panel

    def test_every_source_is_outermost_and_not_inside_any_folded_heading(self):
        panel=self.panel()
        for layer in panel.values():
            if layer['kind'] in {'watermark','node_effect'}: continue
            with self.subTest(kind=layer['kind']):
                panel.select_layer_id(layer['id']); editor=panel.editor
                self.assertIs(editor.body.itemAt(0).widget(),editor.content_editor)
                self.assertNotIn('source',editor.sections)
                current=editor.content_editor.parentWidget()
                while current is not editor:
                    self.assertNotIsInstance(current,PropertySection)
                    current=current.parentWidget()
                for section in editor.sections.values(): section.set_expanded(False)
                self.assertTrue(editor.content_editor.isVisibleTo(editor))
                editor.reveal('source'); self.app.processEvents()

    def test_geometry_is_flat_for_text_and_visuals_and_reveal_opens_caption_basic(self):
        panel=self.panel()
        for layer in panel.values():
            panel.select_layer_id(layer['id']); editor=panel.editor
            if not editor.geometry_editor: continue
            self.assertIs(editor.targets['position'],editor.geometry_editor)
            self.assertNotIn('position',editor.sections)
            if layer['kind']=='caption':
                editor.sections['basic'].set_expanded(False)
                editor.reveal('position'); self.app.processEvents()
                self.assertTrue(editor.sections['basic'].toggle.isChecked())
            else:
                self.assertIs(editor.geometry_editor.parentWidget(),editor)

    def test_switching_control_types_does_not_lose_source_position_or_style(self):
        panel=self.panel()
        text=next(layer for layer in panel.values() if layer['kind']=='text')
        panel.select_layer_id(text['id'])
        panel.content_editor.text.setPlainText('Moja skúšobná veta.')
        panel.properties['x'].setValue(37)
        panel.text_properties.controls['font_weight'].setCurrentIndex(panel.text_properties.controls['font_weight'].findData(700))
        for layer in panel.values(): panel.select_layer_id(layer['id'])
        panel.select_layer_id(text['id'])
        self.assertEqual(panel.current()['text'],'Moja skúšobná veta.')
        self.assertEqual(panel.current()['x'],37)
        self.assertEqual(panel.current()['font_weight'],700)

    def test_watermark_sources_and_corner_edits_share_one_panel_without_nested_scrolls(self):
        panel=self.panel(); mark=next(layer for layer in panel.values() if layer['kind']=='watermark')
        panel.select_layer_id(mark['id']); editor=panel.watermark_properties
        self.assertEqual(editor.findChildren(QtWidgets.QTabWidget),[])
        self.assertEqual(editor.findChildren(QtWidgets.QScrollArea),[])
        editor.inputs['top_left'].setText('LZX A')
        editor.corner.setCurrentIndex(editor.corner.findData('bottom_right'))
        editor.inputs['bottom_right'].setText('LZX B')
        editor.insets['watermark_inset_x'].setValue(23)
        editor.text_style.controls['color'].setText('#ff4499'); editor.text_style.update_values()
        panel.select_layer_id('title'); panel.select_layer_id(mark['id'])
        corners=panel.current()['corners']
        self.assertEqual(corners['top_left']['text'],'LZX A')
        self.assertEqual(corners['bottom_right']['text'],'LZX B')
        self.assertEqual(corners['bottom_right']['watermark_inset_x'],23)
        self.assertEqual(corners['bottom_right']['color'],'#ff4499')
        self.assertTrue(editor.source_editor.text.isHidden())
        self.assertNotIn('watermark',panel.editor.sections)

    def test_visual_and_node_basic_choices_are_not_hidden_by_details(self):
        panel=self.panel(); panel.select_layer_id('visual')
        visual=panel.visual_properties
        self.assertIs(visual.basic_form.labelForField(visual.controls['blend_mode']).parentWidget(),
                      visual.controls['blend_mode'].parentWidget())
        self.assertFalse(visual.sections.sections['visual'].isAncestorOf(visual.controls['blend_mode']))
        self.assertFalse(visual.sections.sections['visual'].isAncestorOf(visual.controls['media_loop']))
        effect=next(layer for layer in panel.values() if layer['kind']=='node_effect')
        panel.select_layer_id(effect['id']); editor=panel.node_effect_properties
        self.assertNotIn('node',panel.editor.sections)
        self.assertFalse(editor.help_section.toggle.isChecked())
        editor.workflow.setCurrentIndex(editor.workflow.findData('a'*32))
        panel.select_layer_id('title'); panel.select_layer_id(effect['id'])
        self.assertEqual(panel.current()['workflow_id'],'a'*32)

    def test_all_types_fit_a_narrow_outer_scroll_and_optional_visual_qa(self):
        panel=self.panel(); panel.hide_catalog()
        before=copy.deepcopy(panel.values())
        scroll=QtWidgets.QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(panel)
        self.addCleanup(scroll.deleteLater)
        output=os.environ.get('LZX_ALL_COMPONENT_SCREENSHOT')
        if output: Path(output).mkdir(parents=True,exist_ok=True)
        samples={}
        for layer in panel.values():
            panel.select_layer_id(layer['id']); scroll.resize(380,1000);scroll.show();self.app.processEvents()
            self.assertEqual(scroll.horizontalScrollBar().maximum(),0,layer['kind'])
            if layer['kind'] in {'watermark','node_effect'}:
                self.assertEqual(panel.editor.findChildren(QtWidgets.QTabWidget),[])
            if layer['kind']=='image': self.assertLess(panel.content_editor.height(),160)
            name=('scroll' if layer.get('text_animation') not in (None,'none') else layer['kind'])
            if output and name not in samples:
                if name=='scroll': panel.editor.sections['scroll'].set_expanded(True)
                scroll.resize(460,1080);self.app.processEvents();scroll.verticalScrollBar().setValue(0)
                self.assertTrue(scroll.grab().save(str(Path(output)/(name+'.png'))))
                samples[name]=1
        self.assertEqual(panel.values(),before)

    def test_partial_source_install_refuses_old_panel_api_before_creating_live_editors(self):
        from app_plugins.builtin.batch_text_video.component_inspector import ComponentPropertyPanel
        from app_plugins.builtin.batch_text_video.component_ui import text_property_panel
        from app_plugins.builtin.batch_text_video.visual_media import visual_property_panel
        from app_plugins.builtin.batch_text_video.node_effect_ui import node_property_panel
        from app_plugins.builtin.batch_text_video.watermarks import watermark_property_panel
        with patch.object(ComponentPropertyPanel,'flat_sources',False):
            for factory in (text_property_panel,visual_property_panel,node_property_panel,watermark_property_panel):
                with self.subTest(factory=factory),self.assertRaisesRegex(RuntimeError,'重启'):
                    factory()


if __name__=='__main__': unittest.main()
