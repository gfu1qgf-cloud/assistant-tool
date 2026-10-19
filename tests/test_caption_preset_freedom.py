import copy
from pathlib import Path
import tempfile
import unittest

from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.caption_effect_ui import CaptionEffectEditor
from app_plugins.builtin.batch_text_video.caption_appearance_ui import CaptionAppearanceEditor
from app_plugins.builtin.batch_text_video.caption_styles import effects,preset_choices,uses_style_renderer,entrance_duration_key,PREFIX
from app_plugins.builtin.batch_text_video.caption_basic_render import make_primitives
from app_plugins.builtin.batch_text_video.caption_style_render import motion_at,groups_and_events
from app_plugins.builtin.batch_text_video.caption_random import size_contrast,supports_random_size
from app_plugins.builtin.batch_text_video.captions import write_caption_ass,caption_events,design_layers
from app_plugins.builtin.batch_text_video.property_sections import PropertySection
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
import test_caption_display_controls as fixtures


class PresetFreedomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self):
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_every_preset_keeps_all_caption_and_effect_parameters_enabled(self):
        for preset in ['pop','appear']+[key for _,key in preset_choices()]:
            with self.subTest(preset=preset):
                layer=fixtures.caption(preset)
                before=copy.deepcopy(layer)
                widget=CaptionProperties(); widget.set_layer(layer)
                advanced=CaptionEffectEditor(layer)
                text=CaptionAppearanceEditor(); text.set_layer(layer)
                background=CaptionAppearanceEditor(background=True); background.set_layer(layer)
                try:
                    self.assertTrue(widget.effect_section.isEnabled())
                    self.assertTrue(widget.effect_section.isAncestorOf(widget.effects_editor))
                    self.assertFalse(widget.random_widget.isHidden())
                    for key,control in {**widget.controls,**widget.font_effect_controls}.items():
                        self.assertTrue(control.isEnabled(),(preset,key))
                    for key,control in advanced.controls.items():
                        self.assertTrue(control.isEnabled(),(preset,key))
                    for key,group in advanced.groups.items():
                        self.assertFalse(group.isHidden(),(preset,key))
                        self.assertTrue(group.content.isEnabled(),(preset,key))
                    for editor in (text,background):
                        for key,control in editor.controls.items():
                            self.assertTrue(control.isEnabled(),(preset,key))
                    self.assertEqual(layer,before)
                finally:
                    for editor in (widget,advanced,text,background): editor.deleteLater()

    def test_off_switches_preserve_editability_and_preconfigured_values(self):
        layer=fixtures.caption('appear',caption_random_size=False,caption_first_line_emphasis=False,background_enabled=False)
        editor=CaptionProperties(); editor.set_layer(layer); self.addCleanup(editor.deleteLater)
        changes=[]; editor.changed.connect(changes.append)
        editor.controls['caption_size_contrast'].setValue(3.5)
        editor.controls['caption_first_line_scale'].setValue(1.75)
        editor.font_effect_controls['block_scale_max'].setValue(2.25)
        self.assertFalse(changes[-1]['caption_random_size'])
        self.assertEqual(changes[-1]['caption_size_contrast'],3.5)
        self.assertEqual(effects(changes[-1])['block_scale_max'],2.25)
        self.assertTrue(editor.controls['caption_first_line_scale'].isEnabled())
        appearance=CaptionAppearanceEditor(); appearance.set_layer(layer); self.addCleanup(appearance.deleteLater)
        appearance.controls['color_high_bg'].setText('#ff55aa')
        self.assertFalse(effects(appearance.layer)['dynamic_box'])
        self.assertEqual(effects(appearance.layer)['color_high_bg'],'#ff55aa')
        appearance.controls['dynamic_box'].setChecked(True)
        self.assertEqual(effects(appearance.layer)['color_high_bg'],'#ff55aa')

    def test_generic_section_still_supports_old_non_caption_policy(self):
        for editable in (False,True):
            content=QtWidgets.QWidget(); control=QtWidgets.QCheckBox()
            section=PropertySection('test',content)
            self.addCleanup(section.deleteLater)
            section.set_enable_control(control,disable_content=not editable)
            self.assertEqual(content.isEnabled(),editable)
            control.setChecked(True)
            self.assertTrue(content.isEnabled())
            control.setChecked(False)
            self.assertEqual(content.isEnabled(),editable)

    def test_inspector_font_details_are_editable_even_when_features_are_off(self):
        for preset in ('appear',PREFIX+'默认白字'):
            layer=fixtures.caption(preset,outline_width=0,shadow_enabled=False,background_enabled=False)
            panel=LayerPanel([layer],'.'); panel.select_layer_id(layer['id'])
            self.addCleanup(panel.deleteLater)
            controls=panel.editor.text_properties.controls
            for key in ('outline_color','outline_blur','shadow_color','shadow_blur','shadow_x',
                        'background_color','background_radius','color_hue_variation','color_seed'):
                self.assertTrue(controls[key].isEnabled(),(preset,key))
            for section in ('outline','shadow','background'):
                self.assertTrue(panel.editor.sections[section].content.isEnabled(),section)

    def test_basic_preset_can_render_advanced_effects_without_selecting_another_preset(self):
        layer=fixtures.caption('appear',caption_display_mode='word')
        original=copy.deepcopy(fixtures.WORDS)
        layer['caption_effects'].update(anim_in_type='floating',floating_amplitude=40,floating_period=2,
                                        anim_in_duration=.4,use_underline=True,color_underline='#ff55aa')
        self.assertTrue(uses_style_renderer(layer))
        values,events=make_primitives(fixtures.WORDS,layer,830,600,fixtures.DURATION)
        self.assertTrue(any(value['motion']=='floating' for value in values))
        self.assertTrue(any(value['color']==['#ff55aa'] for value in values))
        floating=next(value for value in values if value['motion']=='floating')
        self.assertNotEqual(motion_at(floating,floating['start']+.3)[2],0)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.ass'
            write_caption_ass(path,fixtures.WORDS,layer,830,600,fixtures.DURATION)
            self.assertIn('Dialogue:',path.read_text('utf8'))
        self.assertEqual(fixtures.WORDS,original)

    def test_opening_and_accepting_effect_dialog_without_edits_keeps_legacy_render_identical(self):
        for preset in ('pop','appear'):
            layer=fixtures.caption(preset,caption_random_size=True,font_min=30,font_max=70)
            dialog=CaptionEffectEditor(layer); self.addCleanup(dialog.deleteLater)
            candidate={**layer,'caption_effects':dialog.value()}
            self.assertEqual(candidate,layer)
            self.assertEqual(caption_events(fixtures.WORDS,candidate,830,600,fixtures.DURATION),
                             caption_events(fixtures.WORDS,layer,830,600,fixtures.DURATION))

    def test_preconfiguring_inactive_effects_does_not_change_current_legacy_frame(self):
        for preset in ('pop','appear'):
            layer=fixtures.caption(preset,caption_random_size=True,font_min=30,font_max=70)
            candidate=copy.deepcopy(layer)
            candidate['caption_effects'].update(floating_amplitude=39,block_typography_enabled=False,
                block_scale_max=2.5,global_mask_enabled=False,global_mask_opacity=.8)
            self.assertFalse(uses_style_renderer(candidate))
            self.assertEqual(caption_events(fixtures.WORDS,candidate,830,600,fixtures.DURATION),
                             caption_events(fixtures.WORDS,layer,830,600,fixtures.DURATION))

    def test_every_preset_can_enable_randomness_including_design_preview(self):
        for preset in ('appear',PREFIX+'默认白字'):
            layer=fixtures.caption(preset,font_min=20,font_max=80,caption_random_size=True,
                                   caption_size_contrast=3,width=98,height=98)
            self.assertTrue(supports_random_size(layer))
            values=caption_events(fixtures.WORDS,layer,1080,1920,fixtures.DURATION)
            self.assertGreater(len({value['_font_pixel_size'] for value in values}),1)
            design=design_layers([layer],'',fixtures.TEXT)[0]
            self.assertEqual(size_contrast(design),3)
            fixed=caption_events(fixtures.WORDS,{**layer,'caption_random_size':False},1080,1920,fixtures.DURATION)
            self.assertEqual(len({value['_font_pixel_size'] for value in fixed}),1)

    def test_animation_duration_has_one_backing_value_and_survives_reselection(self):
        for preset in ('pop','appear',PREFIX+'逐词弹出(随机大小)'):
            layer=fixtures.caption(preset)
            widget=CaptionProperties(); widget.set_layer(layer); self.addCleanup(widget.deleteLater)
            widget.controls['caption_pop_ms'].setValue(1250)
            selected=copy.deepcopy(widget.layer)
            key=entrance_duration_key(effects(selected))
            self.assertAlmostEqual(effects(selected)[key],1.25)
            self.assertLessEqual(selected['caption_pop_ms'],600)  # old metadata is not a second editable clock
            widget.set_layer(selected)
            self.assertEqual(widget.controls['caption_pop_ms'].value(),1250)

    def test_masks_do_not_disable_or_override_independent_ambient_parameters(self):
        layer=fixtures.caption('appear')
        layer['caption_effects'].update(global_mask_enabled=True,global_mask_color='#112244',global_mask_opacity=.2,
                                        ambient_lighting_enabled=True,ambient_dark_color='#442211',ambient_dark_opacity=.3)
        values,_=make_primitives(fixtures.WORDS,layer,830,600,fixtures.DURATION)
        self.assertTrue(any(value['color']==['#112244'] and value['opacity']==.2 for value in values))
        self.assertTrue(any(value['color']==['#442211'] and value['opacity']==.3 for value in values))
        dialog=CaptionEffectEditor(layer); self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.controls['ambient_dark_color'].isEnabled())
        dialog.controls['global_mask_enabled'].setChecked(False)
        self.assertTrue(dialog.controls['ambient_dark_color'].isEnabled())


if __name__=='__main__': unittest.main()
