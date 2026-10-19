import ast
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.captions import new_caption,write_caption_ass,design_layers
from app_plugins.builtin.batch_text_video.caption_display import DEFAULTS,display_settings
from app_plugins.builtin.batch_text_video.caption_styles import apply_style,effects,preset_choices,PREFIX,COLOR_KEYS,colors
from app_plugins.builtin.batch_text_video.caption_style_render import groups_and_events,make_primitives,motion_at,render_design
from app_plugins.builtin.batch_text_video.caption_basic_render import make_primitives as make_basic
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.caption_effect_ui import CaptionEffectEditor
from app_plugins.builtin.batch_text_video.caption_effect_controls import ADVANCED_KEYS,FONT_EFFECT_KEYS
from app_plugins.builtin.batch_text_video.caption_appearance import COMMON_EFFECT_KEYS
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel

TEXT='Je smutné, že len málokto odpovie na túto otázku: Je Boh dôležitý v tvojom živote?'
WORDS=[dict(text=text,start=.2+i*.3,end=.45+i*.3) for i,text in enumerate(TEXT.split())]
DURATION=6.

def caption(preset='appear',**values):
    layer=apply_style(new_caption(),preset,reset_layout=True)
    layer.update(font='Open Sans',font_min=56,font_max=56,width=90,height=90,y=50,
                 caption_words=8,caption_per_line=5,line_spacing=1.1,
                 shadow_enabled=False,outline_width=0,background_enabled=False)
    layer.update(values)
    return normalize_layers([layer])[0]

def primitives(layer,words=WORDS,width=830,height=600):
    return (make_basic if layer['caption_preset'] in {'pop','appear'} else make_primitives)(words,layer,width,height,DURATION)


class CaptionDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def tearDown(self):
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_old_typewriter_is_adapted_to_explicit_component_parameters(self):
        layer=caption(PREFIX+'全屏打字机_金黄光标')
        self.assertEqual(layer['caption_display_mode'],'char')
        self.assertTrue(layer['caption_accumulate'])
        self.assertTrue(layer['caption_first_line_emphasis'])
        self.assertTrue(layer['caption_cursor'])
        self.assertEqual(effects(layer)['anim_in_type'],'none')
        old=copy.deepcopy(layer)
        for key in DEFAULTS: old.pop(key)
        before=copy.deepcopy(old)
        restored=normalize_layers([old])[0]
        self.assertEqual(old,before)
        self.assertEqual({key:restored[key] for key in DEFAULTS},{key:layer[key] for key in DEFAULTS})
        normal=caption(PREFIX+'打字机模式')
        self.assertEqual(normal['caption_display_mode'],'word')
        self.assertFalse(normal['caption_accumulate'])
        self.assertFalse(normal['caption_first_line_emphasis'])

    def test_actual_first_line_only_is_emphasized_after_early_wrapping(self):
        layer=caption(PREFIX+'全屏打字机_金黄光标',caption_first_line_scale=1.2)
        events,_,_=groups_and_events(WORDS,layer,830,600,DURATION)
        self.assertEqual(events[4]['text'],'málokto')
        self.assertGreater(events[4]['line'],0)
        first=[event for event in events if event['line']==0]
        later=[event for event in events if event['line']>0]
        self.assertTrue(all(event['_first_line_emphasis'] for event in first))
        self.assertTrue(all(not event['_first_line_emphasis'] for event in later))
        self.assertTrue(all(event['_font_weight']==900 for event in first))
        self.assertTrue(all(event['_font_weight']==layer['font_weight'] for event in later))
        self.assertEqual(len({event['_font_pixel_size'] for event in later}),1)
        layer['caption_first_line_emphasis']=False
        off=groups_and_events(WORDS,layer,830,600,DURATION)[0]
        self.assertEqual(len({event['_font_pixel_size'] for event in off}),1)
        self.assertEqual(len({event['_font_weight'] for event in off}),1)

    def test_emphasis_works_in_basic_captions_and_preserves_base_font_and_timestamps(self):
        layer=caption(caption_accumulate=True,caption_first_line_emphasis=True,caption_first_line_scale=1.4)
        before=copy.deepcopy(layer)
        _,events=primitives(layer)
        self.assertTrue(any(event['_first_line_emphasis'] for event in events))
        self.assertTrue(any(not event['_first_line_emphasis'] for event in events))
        self.assertEqual([(e['text'],e['start'],e['spoken_end']) for e in events],
                         [(e['text'],e['start'],e['end']) for e in WORDS])
        self.assertEqual(layer,before)

    def test_reveal_order_is_independent_of_all_68_style_choices(self):
        words=WORDS[:3]
        for preset in ['pop','appear']+[key for _,key in preset_choices()]:
            for mode in ('group','word','char','line'):
                with self.subTest(preset=preset,mode=mode):
                    layer=caption(preset,caption_display_mode=mode,caption_accumulate=False,
                        caption_first_line_emphasis=False,caption_cursor=False,caption_words=3,
                        caption_per_line=1,font_min=28,font_max=28)
                    layer['caption_effects'].update(only_show_active_word=False,scrolling_mode=False,
                        global_mask_enabled=False,ambient_glow_enabled=False,ambient_lighting_enabled=False,
                        dynamic_box=False,stroke_expand_enabled=False,speed_trail_enabled=False,use_underline=False,
                        karaoke_highlight=False,flash_duration=0)
                    values,events=primitives(layer,words,width=360,height=640)
                    text=[p for p in values if p['z']==0]
                    starts={round(p['start'],4) for p in text}
                    if mode=='group': self.assertEqual(starts,{round(e['group_start'],4) for e in events})
                    elif mode in {'word','line'}:
                        self.assertEqual(starts,{round(w['start'],4) for w in words})
                    else:
                        self.assertGreater(len(starts),len(words))
                        self.assertTrue(all(start>=words[0]['start'] for start in starts))
                    self.assertEqual([(e['text'],e['start'],e['spoken_end']) for e in events],
                                     [(w['text'],w['start'],w['end']) for w in words])

    def test_character_highlight_draws_one_box_per_word_not_one_per_character(self):
        for preset in ('appear',PREFIX+'打字机模式'):
            layer=caption(preset,caption_display_mode='char')
            layer['caption_effects'].update(dynamic_box=True,color_high_bg='#00ff00',opacity_high_bg=128)
            values,_=primitives(layer)
            boxes=[p for p in values if p['z']==-3]
            self.assertEqual(len(boxes),len(WORDS))
            self.assertTrue(all(abs(p['opacity']-128/255)<1e-9 for p in boxes))

    def test_invalid_reveal_size_and_switch_values_are_rejected(self):
        for updates in ({'caption_display_mode':'unknown'},{'caption_accumulate':1},
                {'caption_first_line_emphasis':'true'},{'caption_first_line_scale':0},
                {'caption_first_line_scale':float('nan')},{'caption_cursor_color':'bad color'}):
            with self.subTest(updates=updates),self.assertRaises(ValueError):
                normalize_layers([{**caption(),**updates}])

    def test_preview_uses_the_full_text_only_when_accumulation_is_enabled(self):
        layer=caption(caption_accumulate=True,caption_words=2)
        value=design_layers([layer],'',TEXT)[0]
        self.assertEqual(value['text'].split(),TEXT.split())
        layer['caption_accumulate']=False
        self.assertEqual(len(design_layers([layer],'',TEXT)[0]['text'].split()),2)

    def test_partial_source_install_refuses_to_mix_old_parameter_schema(self):
        from unittest.mock import patch
        from app_plugins.builtin.batch_text_video import captions
        from app_plugins.builtin.batch_text_video.caption_ui import caption_property_panel
        old={key:value for key,value in captions.DEFAULTS.items() if key not in DEFAULTS}
        with patch.dict(captions.DEFAULTS,old,clear=True),self.assertRaisesRegex(RuntimeError,'重启'):
            caption_property_panel()

    def test_font_controls_share_base_font_form_and_persist_after_reselection(self):
        layer=caption('pop')
        panel=LayerPanel([layer],'.'); self.addCleanup(panel.deleteLater)
        panel.select_layer_id(layer['id'])
        editor=panel.editor; properties=editor.caption_properties
        font=editor.sections['font']
        self.assertIs(editor.sections['font_size'],font)
        self.assertNotIn('random_size',editor.sections)
        for key in ('font_min','font_max'):
            self.assertIs(panel.text_properties.controls[key].parentWidget(),panel.text_properties.font_size_pair)
            self.assertTrue(font.content.isAncestorOf(panel.text_properties.controls[key]))
        for key in ('caption_random_size','caption_first_line_emphasis','caption_first_line_scale'):
            self.assertTrue(font.content.isAncestorOf(properties.controls[key]))
        properties.controls['caption_display_mode'].setCurrentIndex(properties.controls['caption_display_mode'].findData('char'))
        properties.controls['caption_accumulate'].setChecked(True)
        properties.controls['caption_first_line_emphasis'].setChecked(True)
        properties.controls['caption_first_line_scale'].setValue(1.55)
        panel.select_layer_id('title'); panel.select_layer_id(layer['id'])
        self.assertEqual(panel.current()['caption_display_mode'],'char')
        self.assertEqual(panel.current()['caption_first_line_scale'],1.55)
        self.assertTrue(panel.current()['caption_accumulate'])
        self.assertTrue(panel.caption_properties.controls['caption_words'].isEnabled())
        self.assertTrue(panel.caption_properties.controls['caption_cursor'].isVisibleTo(panel.caption_properties))
        properties.controls['caption_first_line_emphasis'].setChecked(False)
        self.assertEqual(panel.current()['caption_first_line_scale'],1.55)
        self.assertTrue(properties.controls['caption_first_line_scale'].isEnabled())

    def test_node_editor_and_component_conversion_preserve_display_parameters(self):
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        layer=caption(caption_timing='task',caption_display_mode='char',caption_first_line_emphasis=True)
        updates=[]; editor=CaptionNodeEditor({'caption':layer},updates.append)
        self.addCleanup(editor.deleteLater)
        editor.panel.caption_properties.controls['caption_first_line_scale'].setValue(1.75)
        restored=CaptionNodeEditor(updates[-1],lambda _:None); self.addCleanup(restored.deleteLater)
        self.assertEqual(restored.panel.caption_properties.layer['caption_first_line_scale'],1.75)
        self.assertEqual(restored.panel.caption_properties.layer['caption_display_mode'],'char')

    def test_optional_font_and_effect_dialog_visual_qa(self):
        output=os.environ.get('LZX_CAPTION_DISPLAY_SCREENSHOT')
        if not output: return
        layer=caption(PREFIX+'全屏打字机_金黄光标')
        panel=LayerPanel([layer],'.'); panel.select_layer_id(layer['id'])
        panel.catalog.hide(); panel.header.hide()
        scroll=QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(panel)
        self.addCleanup(scroll.deleteLater)
        scroll.resize(500,920)
        editor=panel.editor
        editor.sections['basic'].set_expanded(False)
        editor.sections['font'].set_expanded(True)
        scroll.show(); self.app.processEvents()
        self.assertTrue(scroll.grab().save(output))
        dialog=CaptionEffectEditor(layer); self.addCleanup(dialog.deleteLater)
        dialog.show(); dialog.categories.sections['decoration'].set_expanded(True); self.app.processEvents()
        self.assertTrue(dialog.grab().save(str(Path(output).with_stem('caption-effects-controls'))))


class EffectCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_every_implemented_style_field_has_an_explicit_control_owner(self):
        from app_plugins.builtin.batch_text_video import caption_style_render,caption_basic_render,caption_appearance,caption_random
        used=set()
        for module in (caption_style_render,caption_basic_render,caption_appearance,caption_random):
            for node in ast.walk(ast.parse(Path(module.__file__).read_text('utf-8-sig'))):
                if isinstance(node,ast.Subscript) and isinstance(node.value,ast.Name) and node.value.id=='style':
                    if isinstance(node.slice,ast.Constant) and isinstance(node.slice.value,str): used.add(node.slice.value)
        self.assertFalse(used-(ADVANCED_KEYS|FONT_EFFECT_KEYS|COMMON_EFFECT_KEYS))

    def test_all_presets_expose_the_entire_effect_schema_not_only_explicit_preset_keys(self):
        for _title,preset in preset_choices():
            with self.subTest(preset=preset):
                layer=caption(preset)
                dialog=CaptionEffectEditor(layer)
                try:
                    self.assertEqual(set(dialog.controls),ADVANCED_KEYS)
                    self.assertFalse(set(dialog.controls)&COMMON_EFFECT_KEYS)
                    self.assertFalse(set(dialog.controls)&FONT_EFFECT_KEYS)
                    before=effects(layer); after=effects({**layer,'caption_effects':dialog.value()})
                    for key in ADVANCED_KEYS:
                        if key in COLOR_KEYS: self.assertEqual(colors(before[key]),colors(after[key]),key)
                        elif isinstance(before[key],float): self.assertAlmostEqual(before[key],after[key],places=3,msg=key)
                        else: self.assertEqual(before[key],after[key],key)
                finally: dialog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_changing_animation_reveals_inherited_parameters_and_preserves_unsaved_values(self):
        layer=caption(PREFIX+'默认白字'); before=copy.deepcopy(layer)
        dialog=CaptionEffectEditor(layer); self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.groups['floating'].isHidden())
        mode=dialog.controls['anim_in_type']; mode.setCurrentIndex(mode.findData('floating'))
        self.assertFalse(dialog.groups['floating'].isHidden())
        dialog.controls['floating_amplitude'].setValue(21)
        mode.setCurrentIndex(mode.findData('none')); mode.setCurrentIndex(mode.findData('floating'))
        self.assertEqual(dialog.controls['floating_amplitude'].value(),21)
        self.assertEqual(dialog.value()['floating_amplitude'],21)
        dialog.controls['stroke_expand_enabled'].setChecked(True)
        dialog.controls['stroke_expand_layers'].setValue(5)
        dialog.controls['stroke_expand_enabled'].setChecked(False)
        self.assertTrue(dialog.groups['stroke'].content.isEnabled())
        self.assertEqual(dialog.value()['stroke_expand_layers'],5)
        self.assertEqual(layer,before)

    def test_text_gradient_and_top_glow_have_common_switches(self):
        from app_plugins.builtin.batch_text_video.caption_appearance_ui import CaptionAppearanceEditor
        layer=caption(PREFIX+'默认白字')
        for background in (False,True):
            editor=CaptionAppearanceEditor(background=background); self.addCleanup(editor.deleteLater)
            editor.set_layer(layer)
            expected={'bg_gradient_highlight'} if background else {'text_gradient_enabled','color_text','text_gradient_direction'}
            self.assertTrue(expected<=set(editor.controls))

    def test_gradient_remains_controllable_with_current_word_highlight(self):
        for preset in ('appear',PREFIX+'默认白字'):
            layer=caption(preset,caption_display_mode='group')
            layer['caption_effects'].update(text_gradient_enabled=True,color_text='#ee55ff,#55eeff',
                                          karaoke_highlight=True,color_high='#ffffff')
            values,_=primitives(layer)
            self.assertTrue(any(len(p['color'])==2 for p in values if p['z']==0))
            self.assertTrue(any(p['color']==['#ffffff'] for p in values if p['z']==0))
            layer['caption_effects']['text_gradient_enabled']=False
            self.assertFalse(any(len(p['color'])>1 for p in primitives(layer)[0] if p['z']==0))

    def test_default_background_direction_does_not_block_other_controls(self):
        from app_plugins.builtin.batch_text_video.caption_appearance_ui import CaptionAppearanceEditor
        layer=caption(PREFIX+'暗夜青_青高亮',background_enabled=True)
        editor=CaptionAppearanceEditor(background=True); self.addCleanup(editor.deleteLater)
        editor.set_layer(layer); updates=[]; editor.changed.connect(updates.append)
        self.assertEqual(editor.controls['bg_gradient_type'].currentData(),'linear_h')
        editor.controls['box_padding_x'].setValue(37)
        self.assertTrue(updates)
        self.assertEqual(effects({**layer,**updates[-1]})['box_padding_x'],37)


class CaptionDisplayNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()
        cls.ffmpeg=os.environ.get('NODE_FFMPEG','')

    def test_real_ass_render_reveals_characters_without_changing_word_timestamps(self):
        if not self.ffmpeg: self.skipTest('Set NODE_FFMPEG for native media regression')
        words=[dict(text='nádej',start=.5,end=1.5)]
        layer=caption(caption_display_mode='char',caption_cursor=False,caption_words=1,caption_per_line=1,
                      font_min=96,font_max=96,color='#ffffff',width=96,height=96)
        before=copy.deepcopy(words)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'caption.ass'
            events=write_caption_ass(path,words,layer,360,640,2)
            images=[]
            for time in (.58,1.39):
                result=subprocess.run([self.ffmpeg,'-v','error','-f','lavfi','-i','color=c=black:s=360x640:r=25:d=2',
                    '-vf',"ass=filename='caption.ass'",'-ss',str(time),'-frames:v','1','-f','image2pipe','-vcodec','png','-'],
                    cwd=directory,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
                image=QtGui.QImage.fromData(result.stdout)
                self.assertFalse(image.isNull())
                images.append(sum(image.pixelColor(x,y).lightness()>80 for y in range(image.height()) for x in range(image.width())))
            self.assertGreater(images[1],images[0]*2)
            self.assertEqual([(e['text'],e['start'],e['spoken_end']) for e in events],[('nádej',.5,1.5)])
        self.assertEqual(words,before)

