"""Presets cannot silently overwrite layout; legacy reveal is explicitly adapted."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.caption_styles import (
    apply_style,effects,preset_choices,freeze_effect_parameters,LAYOUT_KEYS,PREFIX)
from app_plugins.builtin.batch_text_video.caption_display import DEFAULTS,normalize_display,display_settings
from app_plugins.builtin.batch_text_video.caption_custom_styles import LZX_WORD_POP
from app_plugins.builtin.batch_text_video.caption_effect_ui import CaptionEffectEditor
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives
from app_plugins.builtin.batch_text_video.captions import new_caption,write_caption_ass,filter_path
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.components import new_text_component

PRESETS=['pop','appear']+[key for _,key in preset_choices()]
WORDS=[dict(text='Božia',start=.4,end=.9),dict(text='láska',start=1.2,end=1.7),
       dict(text='dáva.',start=2.,end=2.5)]


class PresetAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def tearDown(self): QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def configured(self):
        layer=new_caption()
        layer.update(x=43.25,y=62.75,width=79.5,height=57.25,align='right',vertical='bottom',
            caption_words=6,caption_per_line=4,caption_hold_ms=420,caption_timing='model',
            caption_language='sk',caption_source='full',source='voice')
        return normalize_layers([layer])[0]

    def test_all_68_presets_keep_user_layout_and_rhythm_and_do_not_mutate_input(self):
        layer=self.configured(); original=copy.deepcopy(layer)
        protected=(*LAYOUT_KEYS,'id','caption_timing','caption_language','caption_source','source')
        for preset in PRESETS:
            with self.subTest(preset=preset):
                result=apply_style(layer,preset)
                self.assertEqual({key:result[key] for key in protected},{key:layer[key] for key in protected})
                self.assertEqual(result['caption_effects_version'],1)
                self.assertEqual(layer,original)

    def test_explicit_reset_uses_recipe_defaults_including_custom_rhythm(self):
        layer=self.configured()
        normal=apply_style(layer,PREFIX+'默认白字',reset_layout=True)
        self.assertEqual((normal['caption_words'],normal['caption_per_line']),(8,4))
        self.assertEqual((normal['align'],normal['vertical']),('center','center'))
        custom=apply_style(layer,LZX_WORD_POP,reset_layout=True)
        self.assertEqual((custom['caption_words'],custom['caption_per_line'],custom['caption_hold_ms']),(6,4,1000))
        basic=apply_style(layer,'appear',reset_layout=True)
        expected=new_caption()
        self.assertEqual({key:basic[key] for key in LAYOUT_KEYS},{key:expected[key] for key in LAYOUT_KEYS})
        for result in (normal,custom,basic):
            self.assertEqual(result['id'],layer['id']); self.assertEqual(result['caption_timing'],'model')

    def test_ui_switch_keeps_values_while_explicit_reset_really_resets(self):
        layer=self.configured(); other=new_text_component()
        panel=LayerPanel([layer,other],'.'); self.addCleanup(panel.deleteLater)
        panel.select_layer_id(layer['id'])
        combo=panel.caption_properties.controls['caption_preset']
        for preset in (PREFIX+'渐变蓝色框',PREFIX+'全屏打字机_金黄光标',LZX_WORD_POP):
            combo.setCurrentIndex(combo.findData(preset))
            self.assertEqual({key:panel.current()[key] for key in LAYOUT_KEYS},{key:layer[key] for key in LAYOUT_KEYS})
        panel.select_layer_id(other['id']); panel.select_layer_id(layer['id'])
        self.assertEqual(panel.current()['caption_hold_ms'],420)
        panel.caption_properties.reset_button.click()
        self.assertEqual(panel.current()['caption_hold_ms'],1000)
        self.assertEqual(panel.current()['height'],28)

    def test_saved_display_values_are_never_rederived_from_the_preset_name(self):
        layer=apply_style(self.configured(),PREFIX+'打字机模式')
        layer.update(caption_display_mode='char',caption_accumulate=False,caption_cursor=False,
            caption_first_line_emphasis=False,caption_first_line_scale=1.6)
        expected={key:copy.deepcopy(layer[key]) for key in DEFAULTS}
        layer['caption_preset']=PREFIX+'逐行出现'
        normalize_display(layer)
        self.assertEqual({key:layer[key] for key in DEFAULTS},expected)
        layer.pop('caption_cursor_color'); normalize_display(layer)
        for key,value in expected.items():
            if key!='caption_cursor_color': self.assertEqual(layer[key],value)

    def test_shared_node_inspector_keeps_layout_and_grouping_when_switching_presets(self):
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        layer=self.configured(); layer['caption_timing']='task'
        changes=[]
        editor=CaptionNodeEditor({'caption':layer},changes.append)
        self.addCleanup(editor.deleteLater)
        combo=editor.panel.caption_properties.controls['caption_preset']
        for preset in (PREFIX+'全屏打字机_金黄光标',LZX_WORD_POP,PREFIX+'渐变蓝色框'):
            combo.setCurrentIndex(combo.findData(preset))
            self.assertFalse(editor.error_label.isVisible())
            self.assertEqual({key:changes[-1]['caption'][key] for key in LAYOUT_KEYS},
                             {key:layer[key] for key in LAYOUT_KEYS})
            self.assertEqual(changes[-1]['caption']['caption_timing'],'task')

    def test_legacy_reveal_migrates_before_snapshot_and_without_mutating_source(self):
        for preset,mode in ((PREFIX+'打字机模式','word'),(PREFIX+'全屏打字机_金黄光标','char'),
                            (PREFIX+'逐行出现','line')):
            with self.subTest(preset=preset):
                old=new_caption(); old['caption_preset']=preset
                for key in DEFAULTS: old.pop(key)
                before=copy.deepcopy(old)
                restored=freeze_effect_parameters(copy.deepcopy(old))
                self.assertEqual(display_settings(restored)['caption_display_mode'],mode)
                self.assertEqual(effects(restored)['anim_in_type'],'none')
                self.assertEqual(old,before)

    def test_current_snapshots_reject_old_animation_alias_instead_of_silently_disabling(self):
        layer=self.configured()
        for name in ('typewriter','bullet_reveal'):
            invalid=copy.deepcopy(layer); invalid['caption_effects']['anim_in_type']=name
            before=copy.deepcopy(invalid)
            with self.assertRaisesRegex(ValueError,'显示方式'): effects(invalid)
            self.assertEqual(invalid,before)

    def test_ui_does_not_offer_reveal_as_an_independent_entrance_animation(self):
        editor=CaptionEffectEditor(self.configured()); self.addCleanup(editor.deleteLater)
        for key in ('anim_in_type','anim_out_type'):
            for value in ('typewriter','bullet_reveal'):
                self.assertEqual(editor.controls[key].findData(value),-1)

    def test_invalid_reset_flag_does_not_change_input(self):
        layer=self.configured(); before=copy.deepcopy(layer)
        with self.assertRaises(ValueError): apply_style(layer,'appear',reset_layout='yes')
        self.assertEqual(layer,before)


def typewriter_layer(preset,mode):
    value=apply_style(new_caption(),preset,reset_layout=True)
    value.update(font='Open Sans',font_min=50,font_max=50,x=50,y=50,width=90,height=70,
        caption_words=3,caption_per_line=3,caption_display_mode=mode,caption_accumulate=False,
        caption_first_line_emphasis=False,caption_cursor=False,caption_random_size=False,
        color='#ffffff',outline_width=0,shadow_enabled=False,background_enabled=False)
    value['caption_effects'].update(anim_in_type='none',anim_out_type='none',karaoke_highlight=False,
        dynamic_box=False,text_gradient_enabled=False,scrolling_mode=False,only_show_active_word=False,
        use_underline=False,stroke_expand_enabled=False,speed_trail_enabled=False,
        global_mask_enabled=False,ambient_glow_enabled=False,ambient_lighting_enabled=False)
    return normalize_layers([value])[0]


class TypewriterRevealAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_preview_glyphs_never_start_before_their_authoritative_word(self):
        for preset in (PREFIX+'打字机模式',PREFIX+'全屏打字机_金黄光标'):
            for mode in ('word','char'):
                with self.subTest(preset=preset,mode=mode):
                    layer=typewriter_layer(preset,mode); before=copy.deepcopy(layer)
                    primitives,events=make_primitives(WORDS,layer,360,640,3.)
                    for primitive in (value for value in primitives if value['z']==0):
                        event=next(value for value in events if abs(value['x']-primitive['x'])<1e-6 and abs(value['y']-primitive['y'])<1e-6)
                        self.assertGreaterEqual(primitive['start'],event['start'])
                    self.assertEqual(layer,before)

    @unittest.skipUnless(os.environ.get('BATCH_TEST_FFMPEG'),'needs FFmpeg')
    def test_actual_ass_render_hides_future_words_for_word_and_char_modes(self):
        ffmpeg=os.environ['BATCH_TEST_FFMPEG']
        def run(args):
            result=subprocess.run([ffmpeg,'-hide_banner','-loglevel','error',*args],capture_output=True,timeout=40,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
            return result.stdout
        with tempfile.TemporaryDirectory() as directory:
            for mode in ('word','char'):
                layer=typewriter_layer(PREFIX+'打字机模式',mode)
                path=Path(directory)/(mode+'.ass'); output=Path(directory)/(mode+'.mp4')
                events=write_caption_ass(path,WORDS,layer,360,640,3.)
                run(['-y','-f','lavfi','-i','color=c=black:s=360x640:r=20:d=3',
                    '-vf','ass=filename='+filter_path(path),'-c:v','libx264','-pix_fmt','yuv420p',str(output)])
                image=QtGui.QImage.fromData(run(['-ss','0.8','-i',str(output),'-frames:v','1','-f','image2pipe','-c:v','png','-']))
                self.assertFalse(image.isNull())
                def bright(event):
                    bounds=QtCore.QRectF(event['x'],event['y'],event['width'],event['height']).toAlignedRect().intersected(image.rect())
                    return sum(image.pixelColor(x,y).red()>180 and image.pixelColor(x,y).green()>180
                        for y in range(bounds.top(),bounds.bottom()+1) for x in range(bounds.left(),bounds.right()+1))
                self.assertGreater(bright(events[0]),20)
                self.assertEqual(bright(events[1]),0)


if __name__=='__main__': unittest.main()
