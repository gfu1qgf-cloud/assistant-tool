"""Every caption gets independent basic appearance and the same native renderer."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video.captions import new_caption,write_caption_ass,filter_path
from app_plugins.builtin.batch_text_video.caption_styles import apply_style,effects,preset_choices,PREFIX
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives,motion_at,render_design
from app_plugins.builtin.batch_text_video.caption_basic_render import make_primitives as make_basic
from app_plugins.builtin.batch_text_video.caption_appearance import group_colors,COMMON_EFFECT_KEYS
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.caption_effect_ui import CaptionEffectEditor
from app_plugins.builtin.batch_text_video.layout import ensure_fonts

WORDS=[dict(text=text,start=.2+i*.5,end=.55+i*.5) for i,text in enumerate(('Božia','nádej','pokoj','láska','nový','deň'))]


def caption(preset='pop',**values):
    layer=apply_style(new_caption(),preset,reset_layout=True)
    layer.update(width=96,height=85,x=50,y=50,font_min=30,font_max=44,caption_words=2,caption_per_line=2)
    layer.update(values)
    return normalize_layers([layer])[0]


def primitives(layer):
    return (make_basic if layer['caption_preset'] in {'pop','appear'} else make_primitives)(WORDS,layer,360,640,4.)[0]


class CaptionCommonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_caption_defaults_and_palette_are_not_shared_between_components(self):
        first,second=new_caption(),new_caption()
        first['caption_effects']['opacity_high_bg']=51
        first['caption_group_palette'].append('#112233')
        self.assertEqual(second['caption_effects'],{})
        self.assertNotIn('#112233',second['caption_group_palette'])
        self.assertNotIn('#112233',new_caption()['caption_group_palette'])

    def test_minimal_caption_configuration_receives_defaults_before_appearance_migration(self):
        from app_plugins.builtin.batch_text_video.captions import DEFAULTS
        layer=new_caption()
        for key in DEFAULTS: layer.pop(key,None)
        result=normalize_layers([layer])[0]
        self.assertEqual(result['caption_preset'],'pop')
        self.assertEqual(result['caption_appearance_version'],2)
        self.assertFalse(result['caption_random_size'])

    def test_random_size_is_off_by_default_but_available_to_every_preset(self):
        self.assertFalse(new_caption()['caption_random_size'])
        from app_plugins.builtin.batch_text_video.caption_random import size_contrast,supports_random_size
        for name in ['pop','appear']+[key for _,key in preset_choices()]:
            layer=caption(name)
            self.assertFalse(layer['caption_random_size'],name)
            self.assertTrue(supports_random_size(layer),name)
            layer['caption_random_size']=True
            self.assertEqual(size_contrast(layer),layer['caption_size_contrast'],name)

    def test_basic_ui_and_advanced_have_one_owner_and_save_on_reselection(self):
        layer=caption('appear'); panel=LayerPanel([layer],'.'); panel.select_layer_id(layer['id'])
        self.addCleanup(panel.deleteLater)
        editor=panel.editor; common=editor.caption_appearance.controls
        self.assertTrue(editor.sections['font'].content.isAncestorOf(editor.caption_appearance))
        self.assertTrue(editor.sections['background'].content.isAncestorOf(editor.caption_background))
        self.assertNotIn('random_size',editor.sections)
        self.assertTrue(editor.sections['font'].content.isAncestorOf(editor.caption_properties.random_widget))
        self.assertFalse(editor.caption_properties.random_widget.isHidden())
        common['karaoke_highlight'].setChecked(True)
        common['color_high'].setText('#ff75bd')
        common['dynamic_box'].setChecked(True)
        common['opacity_high_bg'].setValue(255)
        editor.caption_appearance.controls['caption_group_colors'].setChecked(True)
        editor.caption_appearance.controls['caption_color_seed'].setValue(127)
        editor.text_properties.controls['background_opacity'].setValue(0)
        panel.select_layer_id('title'); panel.select_layer_id(layer['id'])
        style=effects(panel.current())
        self.assertTrue(style['karaoke_highlight']); self.assertTrue(style['dynamic_box'])
        self.assertEqual(style['color_high'],'#ff75bd'); self.assertEqual(style['opacity_high_bg'],255)
        self.assertTrue(panel.current()['caption_group_colors'])
        self.assertEqual(panel.current()['caption_color_seed'],127)
        self.assertEqual(panel.current()['background_opacity'],0)
        if os.environ.get('LZX_CAPTION_COMMON_SCREENSHOT'):
            panel.catalog.hide(); panel.header.hide()
            scroll=QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(panel)
            self.addCleanup(scroll.deleteLater)
            scroll.resize(470,850); scroll.show()
            editor.sections['word_highlight_box'].set_expanded(True)
            self.app.processEvents()
            scroll.ensureWidgetVisible(common['opacity_high_bg'])
            self.app.processEvents()
            self.assertTrue(scroll.grab().save(os.environ['LZX_CAPTION_COMMON_SCREENSHOT']))
        dialog=CaptionEffectEditor(caption(PREFIX+'暗夜青_青高亮'))
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(set(dialog.controls)&COMMON_EFFECT_KEYS)

    def test_highlight_box_is_opaque_at_start_in_every_preset_and_independent_of_group_background(self):
        for preset in ['pop','appear']+[key for _,key in preset_choices()]:
            with self.subTest(preset=preset):
                layer=caption(preset,background_enabled=True,background_opacity=0)
                layer['caption_effects'].update(dynamic_box=True,color_high_bg='#00ff00',opacity_high_bg=255,
                    dyn_box_anim=False)
                boxes=[p for p in primitives(layer) if p['z']==-3]
                self.assertTrue(boxes)
                for box in boxes:
                    self.assertEqual(box['opacity'],1)
                    self.assertEqual(motion_at(box,box['start']+.001)[3],1)
                    self.assertEqual(box['motion'],'none')
                layer['background_enabled']=False
                self.assertEqual([p['opacity'] for p in primitives(layer) if p['z']==-3],[1]*len(boxes))

    def test_highlight_alpha_one_is_one_of_255_not_full_opacity(self):
        layer=caption()
        for alpha in (0,1,64,255):
            layer['caption_effects'].update(dynamic_box=True,opacity_high_bg=alpha)
            boxes=[p for p in primitives(layer) if p['z']==-3]
            if not alpha: self.assertFalse(boxes)
            else: self.assertTrue(all(abs(p['opacity']-alpha/255)<1e-8 for p in boxes))

    def test_legacy_appearance_is_transferred_once_without_reverting_basic_changes(self):
        layer=caption(PREFIX+'暗夜青_青高亮')
        layer.pop('caption_appearance_version')
        layer.update(background_enabled=False,background_opacity=5)
        layer['caption_effects'].update(use_box=True,opacity_bg=64,opacity_high_bg=.6)
        migrated=normalize_layers([layer])[0]
        self.assertTrue(migrated['background_enabled'])
        self.assertAlmostEqual(migrated['background_opacity'],64/255*100)
        self.assertEqual(effects(migrated)['opacity_high_bg'],153)
        migrated.update(background_enabled=False,background_opacity=0)
        saved=normalize_layers([migrated])[0]
        self.assertFalse(effects(saved)['use_box'])
        self.assertEqual(effects(saved)['opacity_bg'],0)

    def test_node_editor_reuses_and_persists_common_settings_on_recreation(self):
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        updates=[]
        layer=caption(caption_timing='task')
        editor=CaptionNodeEditor({'caption':layer},updates.append)
        self.addCleanup(editor.deleteLater)
        common=editor.panel.editor.caption_appearance.controls
        common['dynamic_box'].setChecked(True)
        common['opacity_high_bg'].setValue(255)
        common['caption_group_colors'].setChecked(True)
        common['caption_color_seed'].setValue(371)
        editor.panel.text_properties.controls['outline_blur'].setValue(4)
        params=updates[-1]
        restored=CaptionNodeEditor(params,lambda _values:None)
        self.addCleanup(restored.deleteLater)
        saved=restored.panel.current()
        self.assertEqual(saved['outline_blur'],4)
        self.assertTrue(saved['caption_group_colors'])
        self.assertEqual(saved['caption_color_seed'],371)
        self.assertTrue(effects(saved)['dynamic_box'])
        self.assertEqual(effects(saved)['opacity_high_bg'],255)

    def test_group_background_switch_opacity_and_word_highlight_are_independent_for_all_modes(self):
        for preset in ('pop','appear',PREFIX+'暗夜青_青高亮','davinci:lzx_word_pop'):
            layer=caption(preset,background_enabled=True,background_opacity=20)
            layer['caption_effects'].update(dynamic_box=False,karaoke_highlight=True,color_high='#ff75bd')
            result=primitives(layer)
            self.assertTrue(any(p['z']==-5 and abs(p['opacity']-.2)<1e-8 for p in result))
            self.assertFalse(any(p['z']==-3 for p in result))
            self.assertTrue(any('#ff75bd' in p['color'] for p in result if p['z']==0))
            layer['background_enabled']=False
            self.assertFalse(any(p['z']==-5 for p in primitives(layer)))

    def test_group_colors_cycle_deterministically_without_adjacent_repeats(self):
        layer=caption(caption_group_colors=True,caption_group_palette=['#5de6ff','#ff83b5','#b6a5ff'])
        before=copy.deepcopy(layer); a=group_colors(layer,9,WORDS); b=group_colors(layer,9,WORDS)
        self.assertEqual(a,b); self.assertTrue(all(x!=y for x,y in zip(a,a[1:])))
        self.assertEqual(a[:1],group_colors(layer,1,WORDS[:2]))
        self.assertEqual(layer,before)
        for preset in ('pop','appear','davinci:lzx_word_pop'):
            layer=caption(preset,caption_group_colors=True,caption_group_palette=['#5de6ff','#ff83b5','#b6a5ff'])
            selected={color for p in primitives(layer) if p['z']==0 for color in p['color']}
            self.assertEqual(selected,set(layer['caption_group_palette']))

    def test_outline_blur_is_a_separate_primitive_and_leaves_word_fill_sharp(self):
        for preset in ('pop','appear','davinci:lzx_word_pop'):
            layer=caption(preset,outline_width=4,outline_blur=12,shadow_enabled=False)
            result=primitives(layer)
            self.assertTrue(any(p['z']==-1 and p['blur']==4 for p in result))
            self.assertTrue(all(p['border']==0 and p['blur']==0 for p in result if p['z']==0))
            with tempfile.TemporaryDirectory() as root:
                file=Path(root)/'captions.ass'; write_caption_ass(file,WORDS,layer,360,640,4.)
                self.assertIn('\\blur4.000',file.read_text('utf8'))

    def test_group_palette_never_conflicts_with_decorative_outline_palette(self):
        from app_plugins.builtin.batch_text_video.caption_style_render import groups_and_events
        for preset in [key for _,key in preset_choices()]:
            with self.subTest(preset=preset):
                layer=caption(preset,caption_group_colors=True,
                    caption_group_palette=['#5de6ff','#ff83b5','#b6a5ff'])
                layer['caption_effects'].update(karaoke_highlight=False,stroke_expand_enabled=True,
                    stroke_expand_colors=['#112233'])
                _,groups,_=groups_and_events(WORDS,layer,360,640,4.)
                result=primitives(layer)
                selected={color for p in result if p['z']==0 for color in p['color']}
                self.assertTrue(set(group_colors(layer,len(groups),WORDS)) <= selected)

    def test_static_text_and_watermark_outline_can_be_soft_without_softening_text(self):
        from app_plugins.builtin.batch_text_video.components import new_text_component
        from app_plugins.builtin.batch_text_video.text_components import render_text_box
        layer=new_text_component(); layer.update(text='Božia nádej',font='Poppins',font_min=64,font_max=64,
            color='#ffffff',outline_color='#0000ff',outline_width=6,shadow_enabled=False,
            width=96,height=80,align='center',vertical='center')
        layer=normalize_layers([layer])[0]
        hard,_=render_text_box(layer,360,640)
        soft,_=render_text_box({**layer,'outline_blur':15},360,640)
        def count(image,predicate):
            return sum(predicate(image.pixelColor(x,y)) for y in range(image.height()) for x in range(image.width()))
        def foreground_channel(image):
            image=image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888_Premultiplied)
            return image.constBits().asstring(image.sizeInBytes())[1::4]
        hard_green,soft_green=foreground_channel(hard),foreground_channel(soft)
        self.assertLessEqual(max(abs(a-b) for a,b in zip(hard_green,soft_green)),2)
        blue=lambda p:p.alpha()>0 and p.red()<20 and p.green()<20 and p.blue()>100
        self.assertGreater(count(soft,blue),count(hard,blue))

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Set NODE_FFMPEG for native regression')
    def test_native_highlight_box_reaches_full_opacity_before_text_fade_finishes(self):
        for preset in ('pop',PREFIX+'暗夜青_青高亮'):
            layer=caption(preset,background_enabled=True,background_opacity=0,opacity=100)
            layer['caption_effects'].update(dynamic_box=True,color_high_bg='#00ff00',opacity_high_bg=255,
                dyn_box_anim=False,high_padding=18)
            result=primitives(layer); box=next(p for p in result if p['z']==-3)
            with tempfile.TemporaryDirectory() as root:
                file=Path(root)/'captions.ass'; write_caption_ass(file,WORDS,layer,360,640,4.)
                samples=[]
                for background in ('black','white'):
                    command=[os.environ['NODE_FFMPEG'],'-hide_banner','-loglevel','error','-f','lavfi','-i',
                        f'color=c={background}:s=360x640:r=30:d=4','-vf','format=rgba,ass=filename='+filter_path(file),
                        '-ss',str(box['start']+.04),'-frames:v','1','-f','image2pipe','-c:v','png','-']
                    run=subprocess.run(command,capture_output=True,timeout=20)
                    self.assertEqual(run.returncode,0,run.stderr.decode('utf8','replace'))
                    image=QtGui.QImage.fromData(run.stdout); self.assertFalse(image.isNull())
                    # Avoid glyphs and the rounded outside edge. Opaque pixels
                    # must not change when the underlying video changes.
                    samples.append(image.pixelColor(round(box['x']+box['width']/2),round(box['y']+4)))
                self.assertGreater(samples[0].green(),220)
                self.assertLess(max(abs(getattr(samples[0],c)()-getattr(samples[1],c)()) for c in ('red','green','blue')),3)
