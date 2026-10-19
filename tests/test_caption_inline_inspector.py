"""Live settings and a parameter-owned preset contract, with synthetic captions."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video.caption_styles import (
    STYLE_DEFAULTS, apply_style, effects, preset_choices, uses_style_renderer, PREFIX)
from app_plugins.builtin.batch_text_video.captions import new_caption, caption_events, write_caption_ass
from app_plugins.builtin.batch_text_video.caption_basic_render import make_primitives
from app_plugins.builtin.batch_text_video.caption_style_render import render_design
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.caption_effect_ui import CaptionEffectEditor
import test_caption_display_controls as fixtures


class InlineCaptionInspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def panel(self):
        caption=fixtures.caption()
        text=new_text_component(); text['id']='other'
        panel=LayerPanel([caption,text],'.'); panel.select_layer_id(caption['id'])
        self.addCleanup(panel.deleteLater)
        return panel,caption['id']

    def test_only_basic_settings_and_font_are_top_level_and_no_effect_dialog_exists(self):
        panel,_=self.panel(); editor=panel.editor; basic=editor.sections['basic']
        self.assertEqual(editor.body.count(),3)
        self.assertIs(editor.body.itemAt(0).widget(),editor.content_editor)
        self.assertIs(editor.body.itemAt(1).widget(),basic)
        for child in (editor.caption_identity,editor.geometry_editor,editor.caption_properties):
            self.assertTrue(basic.content.isAncestorOf(child))
        for key in ('caption','caption_layout'):
            self.assertTrue(basic.content.isAncestorOf(editor.sections[key]))
        self.assertTrue(basic.content.isAncestorOf(editor.targets['timing']))
        self.assertIs(editor.targets['position'],editor.geometry_editor)
        self.assertNotIn('source',editor.sections)
        properties=editor.caption_properties
        self.assertTrue(editor.sections['caption'].content.isAncestorOf(properties.effect_section))
        self.assertTrue(properties.effect_section.isAncestorOf(properties.effects_editor))
        self.assertFalse(hasattr(properties,'effect_button'))
        self.assertEqual(editor.findChildren(QtWidgets.QDialog),[])
        self.assertEqual(editor.findChildren(QtWidgets.QTabWidget),[])
        self.assertTrue(panel.header.isHidden())
        screenshot=os.environ.get('LZX_CAPTION_INLINE_SCREENSHOT')
        if screenshot:
            panel.hide_catalog()
            scroll=QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
            scroll.setWidget(panel); scroll.resize(460,1040)
            self.addCleanup(scroll.deleteLater)
            scroll.show(); self.app.processEvents()
            self.assertEqual(scroll.horizontalScrollBar().maximum(),0)
            self.assertTrue(scroll.grab().save(screenshot+'-basic.png'))
            editor.sections['caption'].set_expanded(True)
            properties.effect_section.set_expanded(True)
            self.app.processEvents()
            scroll.ensureWidgetVisible(properties.effects_editor.groups['motion'])
            self.app.processEvents()
            self.assertEqual(scroll.horizontalScrollBar().maximum(),0)
            self.assertTrue(scroll.grab().save(screenshot+'-effects.png'))

    def test_inline_changes_commit_once_and_survive_blank_selection_and_other_controls(self):
        panel,identifier=self.panel(); editor=panel.caption_properties.effects_editor
        updates=[]; panel.changed.connect(lambda:updates.append(1))
        editor.controls['floating_amplitude'].setValue(27)
        self.assertEqual(len(updates),1)
        editor.controls['anim_in_type'].setCurrentIndex(editor.controls['anim_in_type'].findData('floating'))
        panel.editor.geometry_editor.properties['x'].setValue(41)
        panel.select_layer_id('other'); self.assertFalse(panel.header.isHidden())
        panel.select_layer_id(identifier)
        self.assertEqual(effects(panel.current())['floating_amplitude'],27)
        self.assertEqual(effects(panel.current())['anim_in_type'],'floating')
        self.assertEqual(panel.current()['x'],41)
        self.assertEqual(panel.caption_properties.effects_editor.controls['floating_amplitude'].value(),27)

    def test_name_and_opacity_are_inside_basic_and_share_the_real_layer_model(self):
        panel,identifier=self.panel(); identity=panel.editor.caption_identity
        identity.name.setText('字幕甲'); identity.name.editingFinished.emit()
        identity.opacity.setValue(61.5)
        panel.select_layer_id('other'); panel.select_layer_id(identifier)
        self.assertEqual(panel.current()['name'],'字幕甲')
        self.assertEqual(panel.current()['opacity'],61.5)
        self.assertEqual(panel.editor.caption_identity.name.text(),'字幕甲')

    def test_invalid_inline_draft_is_visible_without_popup_or_invalid_model_commit(self):
        panel,_=self.panel(); editor=panel.caption_properties.effects_editor
        original=effects(panel.current())
        with patch.object(QtWidgets.QMessageBox,'warning') as warning:
            editor.controls['scatter_min_rotate'].setValue(90)
            self.assertFalse(editor.error.isHidden())
            self.assertEqual(effects(panel.current())['scatter_min_rotate'],original['scatter_min_rotate'])
            editor.controls['scatter_max_rotate'].setValue(100)
            self.assertTrue(editor.error.isHidden())
            self.assertEqual(effects(panel.current())['scatter_min_rotate'],90)
            self.assertEqual(effects(panel.current())['scatter_max_rotate'],100)
        warning.assert_not_called()

    def test_all_presets_keep_every_inline_parameter_editable_and_binding_is_read_only(self):
        editor=CaptionEffectEditor(); self.addCleanup(editor.deleteLater)
        for preset in ['pop','appear']+[key for _,key in preset_choices()]:
            with self.subTest(preset=preset):
                layer=fixtures.caption(preset); original=copy.deepcopy(layer)
                changes=[]; editor.changed.connect(changes.append)
                editor.set_layer(layer)
                self.assertEqual(layer,original); self.assertFalse(changes)
                self.assertTrue(all(control.isEnabled() for control in editor.controls.values()))
                editor.changed.disconnect(changes.append)

    def test_nested_reveal_opens_basic_caption_and_effect_sections(self):
        panel,_=self.panel(); basic=panel.editor.sections['basic']
        caption=panel.editor.sections['caption']
        basic.set_expanded(False); caption.set_expanded(False)
        panel.reveal_property('caption')
        self.assertTrue(basic.toggle.isChecked()); self.assertTrue(caption.toggle.isChecked())


class PresetParameterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_every_preset_materializes_complete_editable_parameters(self):
        for preset in ['pop','appear']+[key for _,key in preset_choices()]:
            with self.subTest(preset=preset):
                layer=apply_style(new_caption(),preset)
                self.assertEqual(layer['caption_effects_version'],1)
                self.assertEqual(set(layer['caption_effects']),set(STYLE_DEFAULTS))

    def test_preset_name_or_catalog_updates_cannot_change_a_saved_effect(self):
        for preset in ['pop','appear']+[key for _,key in preset_choices()]:
            with self.subTest(preset=preset):
                layer=fixtures.caption(preset,caption_words=3,font_min=28,font_max=28)
                renamed=copy.deepcopy(layer); renamed['caption_preset']='appear' if preset!='appear' else 'pop'
                with patch('app_plugins.builtin.batch_text_video.caption_styles.preset_values',side_effect=AssertionError('preset lookup during render')):
                    self.assertEqual(effects(layer),effects(renamed))
                    self.assertEqual(uses_style_renderer(layer),uses_style_renderer(renamed))
                    before=caption_events(fixtures.WORDS[:3],layer,360,640,fixtures.DURATION)
                    after=caption_events(fixtures.WORDS[:3],renamed,360,640,fixtures.DURATION)
                self.assertEqual([(v['text'],v['start'],v['x'],v['y'],v['width'],v['height']) for v in before],
                                 [(v['text'],v['start'],v['x'],v['y'],v['width'],v['height']) for v in after])

    def test_basic_motion_uses_parameters_and_not_the_preset_name(self):
        layer=fixtures.caption('appear',caption_words=3)
        layer['caption_effects'].update(anim_in_type='pop',anim_in_duration=.25,pop_start=.72,pop_peak=1.08)
        values,_=make_primitives(fixtures.WORDS[:3],layer,830,600,fixtures.DURATION)
        text=[v for v in values if v['z']==0]
        self.assertTrue(all(v['motion']=='pop' for v in text))
        self.assertTrue(all(v['motion_ms']==250 for v in text))
        layer['caption_effects']['anim_in_type']='none'
        self.assertTrue(all(v['motion']=='none' for v in make_primitives(fixtures.WORDS[:3],layer,830,600,fixtures.DURATION)[0] if v['z']==0))

    def test_catalog_layout_is_an_editable_parameter_not_a_hidden_preset_test(self):
        layer=fixtures.caption(PREFIX+'撞色黄底框高亮')
        self.assertTrue(effects(layer)['styled_layout'])
        self.assertTrue(uses_style_renderer(layer))
        layer['caption_preset']='appear'
        self.assertTrue(uses_style_renderer(layer))
        layer['caption_effects']['styled_layout']=False
        layer['caption_effects'].update(anim_in_type='none', anim_out_type='none', rotation=0,
            block_typography_enabled=False, only_show_active_word=False, scrolling_mode=False,
            global_mask_enabled=False, ambient_glow_enabled=False, ambient_lighting_enabled=False,
            use_underline=False, stroke_expand_enabled=False, speed_trail_enabled=False)
        self.assertFalse(uses_style_renderer(layer))

    def test_snapshot_survives_save_restore_and_drives_preview_and_ass(self):
        import json
        layer=fixtures.caption(PREFIX+'默认白字',caption_words=3)
        layer['caption_effects'].update(anim_in_type='floating',floating_amplitude=17)
        saved=normalize_layers(json.loads(json.dumps([layer])))[0]
        with patch('app_plugins.builtin.batch_text_video.caption_styles.preset_values',side_effect=AssertionError('saved effect uses catalog')):
            design={**saved,'text':'Božia láska nádej.'}
            image,_=render_design(design,360,640)
            self.assertFalse(image.isNull())
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'caption.ass'
                write_caption_ass(path,fixtures.WORDS[:3],saved,360,640,fixtures.DURATION)
                self.assertIn('Dialogue:',path.read_text('utf8'))

    def test_legacy_configuration_is_adapted_in_memory_without_mutating_original(self):
        old=fixtures.caption(PREFIX+'全屏打字机_金黄光标')
        old.pop('caption_effects_version'); old['caption_effects']={'floating_amplitude':29}
        original=copy.deepcopy(old)
        adapted=normalize_layers([old])[0]
        self.assertEqual(old,original)
        self.assertEqual(adapted['caption_effects_version'],1)
        self.assertEqual(effects(adapted)['floating_amplitude'],29)


if __name__=='__main__': unittest.main()
