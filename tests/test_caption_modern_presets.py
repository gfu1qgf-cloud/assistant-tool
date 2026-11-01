import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets, QtTest
from app_plugins.builtin.batch_text_video.caption_modern_presets import PRESETS
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, effects, preset_choices, description
from app_plugins.builtin.batch_text_video.caption_keywords import matches
from app_plugins.builtin.batch_text_video.caption_parameter_values import VALUE_ROLE, describe, swatch
from app_plugins.builtin.batch_text_video.captions import new_caption, write_caption_ass
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives, render_design
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel


class ModernCaptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layer(self,key='modern:talk_keywords',**values):
        layer=apply_style(new_caption(),key,reset_layout=True)
        layer.update(values)
        return normalize_layers([layer])[0]

    def test_twenty_distinct_presets_register_and_retain_user_geometry_and_rhythm(self):
        self.assertEqual(len(PRESETS),20)
        self.assertEqual(len(preset_choices())+2,88)
        self.assertGreaterEqual(len({v['category'] for v in PRESETS.values()}),10)
        signatures=set()
        source=self.layer('appear',x=35,y=40,width=72,height=60,caption_words=7,caption_per_line=3)
        before=copy.deepcopy(source)
        for key in PRESETS:
            with self.subTest(key=key):
                layer=normalize_layers([apply_style(source,key)])[0]
                for field in ('x','y','width','height','caption_words','caption_per_line'):
                    self.assertEqual(layer[field],source[field])
                self.assertEqual(layer['outline_position'],'outside')
                self.assertNotIn('五段缩放',description(layer))
                signatures.add(repr((effects(layer),layer['caption_display_mode'])))
        self.assertEqual(len(signatures),20)
        self.assertEqual(source,before)

    def test_all_new_presets_have_native_preview_and_ass_without_media_or_models(self):
        from app_plugins.builtin.batch_text_video.caption_gallery import sample
        qa=os.environ.get('LZX_MODERN_CAPTION_QA')
        sheet=QtGui.QImage(1600,1100,QtGui.QImage.Format.Format_RGB32)
        sheet.fill(QtGui.QColor('#16212b'))
        painter=QtGui.QPainter(sheet)
        try:
            with tempfile.TemporaryDirectory() as folder:
                for i,key in enumerate(PRESETS):
                    with self.subTest(key=key):
                        layer=self.layer(key)
                        words=[dict(text=t,start=j*.6,end=j*.6+.5) for j,t in enumerate(('Božia','láska','dáva','nádej'))]
                        image=sample(key,320,180)
                        self.assertFalse(image.isNull())
                        ass=Path(folder)/(str(i)+'.ass')
                        events=write_caption_ass(ass,words,layer,540,960,3.)
                        self.assertEqual(len(events),4)
                        self.assertIn('Dialogue:',ass.read_text('utf8'))
                        x,y=(i%5)*320,(i//5)*275
                        painter.drawImage(x,y,image)
                        painter.setPen(QtGui.QColor('#ffffff'))
                        painter.drawText(QtCore.QRectF(x+8,y+190,304,70),QtCore.Qt.TextFlag.TextWordWrap,PRESETS[key]['label'])
        finally:painter.end()
        if qa:self.assertTrue(sheet.save(str(Path(qa)/'preset-contact-sheet.png')))

    def test_keyword_matching_is_unicode_casefolded_and_explicit(self):
        style=effects(self.layer())
        for value in ('NÁDEJ,','“Láska!”','HOPE','和平与希望'):
            expected=value!='和平与希望'
            self.assertEqual(matches(value,style),expected)
        self.assertFalse(matches('hopeless',style))
        self.assertTrue(matches('和平与希望',{**style,'keyword_match':'contains'}))
        self.assertFalse(matches('hope',{**style,'keyword_emphasis_enabled':False}))
        self.assertFalse(matches('hope',{**style,'keyword_terms':''}))
        for values in ({'keyword_match':'guess'},{'keyword_scale':2.1},{'keyword_terms':[]},
                       {'keyword_terms':','.join('term'+str(i) for i in range(81))}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                effects({**self.layer(),'caption_effects':{**style,**values}})

    def test_keyword_size_is_laid_out_before_painting_and_color_has_explicit_priority(self):
        words=[dict(text='hope',start=0,end=1),dict(text='ordinary',start=1,end=2)]
        layer=self.layer(font_min=60,font_max=60,caption_words=2,caption_per_line=2,
                         caption_group_colors=True,caption_group_palette=['#223344','#445566'])
        layer['caption_effects'].update(keyword_scale=1.5,keyword_color='#ff3860',karaoke_highlight=True)
        primitives,events=make_primitives(words,layer,1080,1000,3.)
        a,b=events
        self.assertTrue(a['_keyword_emphasis']);self.assertFalse(b['_keyword_emphasis'])
        self.assertAlmostEqual(a['_font_pixel_size']/b['_font_pixel_size'],1.5,delta=.03)
        foreground=[p for p in primitives if p['z']==0 and p['x']==a['x']]
        self.assertTrue(foreground)
        self.assertTrue(all(p['color']==['#ff3860'] for p in foreground))
        ordinary=[p for p in primitives if p['z']==0 and p['x']==b['x']]
        self.assertFalse(any('#ff3860' in p['color'] for p in ordinary))
        self.assertTrue(any(any(c in {'#223344','#445566'} for c in p['color']) for p in ordinary))
        self.assertLess(a['x']+a['width'],b['x']+1)
        self.assertEqual([(e['start'],e['spoken_end']) for e in events],[(0,1),(1,2)])

    def test_old_presets_and_disabled_keyword_parameters_do_not_change_pixels_or_ass(self):
        words=[dict(text='hope',start=0,end=1)]
        layer=self.layer('appear',font_min=70,font_max=70)
        altered=copy.deepcopy(layer)
        altered['caption_effects'].update(keyword_terms='hope',keyword_color='#ff0000',keyword_scale=2.,keyword_emphasis_enabled=False)
        with tempfile.TemporaryDirectory() as folder:
            a,b=Path(folder)/'a.ass',Path(folder)/'b.ass'
            write_caption_ass(a,words,layer,540,960,2.)
            write_caption_ass(b,words,altered,540,960,2.)
            self.assertEqual(a.read_bytes(),b.read_bytes())
            a,_=render_design(layer,540,960,words=words,duration=2,time=.5)
            b,_=render_design(altered,540,960,words=words,duration=2,time=.5)
            self.assertEqual(a,b)

    def test_keyword_settings_save_in_project_and_caption_node(self):
        from app_plugins.builtin.batch_text_video.store import Store
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        layer=self.layer()
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);state=store.load();state['settings']['layers']=normalize_layers([layer]);store.save(state,0)
            saved=next(v for v in store.load()['settings']['layers'] if v['id']==layer['id'])
            self.assertEqual(effects(saved)['keyword_terms'],effects(layer)['keyword_terms'])
        layer['caption_timing']='task'
        changes=[];editor=CaptionNodeEditor({'caption':layer},changes.append)
        self.addCleanup(editor.close)
        control=editor.panel.caption_properties.effects_editor.controls['keyword_scale']
        control.setValue(1.4)
        self.assertEqual(changes[-1]['caption']['caption_effects']['keyword_scale'],1.4)

    def test_custom_keyword_list_survives_style_changes_without_enabling_it_implicitly(self):
        layer=self.layer()
        layer['caption_effects'].update(keyword_terms='viera, nádej, my_word',keyword_match='contains')
        result=apply_style(layer,'modern:film_white')
        self.assertEqual(effects(result)['keyword_terms'],'viera, nádej, my_word')
        self.assertEqual(effects(result)['keyword_match'],'contains')
        self.assertFalse(effects(result)['keyword_emphasis_enabled'])

    def test_follow_recipe_keeps_horizontal_position_and_original_word_times(self):
        layer=self.layer('modern:lyric_follow',caption_per_line=1)
        words=[dict(text=t,start=i*.6,end=i*.6+.5) for i,t in enumerate(('Božia','láska','nádej'))]
        primitives,events=make_primitives(words,layer,540,960,3.)
        self.assertEqual(layer['caption_follow_mode'],'line')
        self.assertEqual(layer['caption_clip_feather'],8)
        self.assertEqual(layer['caption_read_opacity'],100)
        text=[p for p in primitives if p['z']==0]
        self.assertTrue(text)
        self.assertTrue(all(point[1]==0 for point in text[0]['follow_track']))
        self.assertEqual([(e['start'],e['spoken_end']) for e in events],[(w['start'],w['end']) for w in words])

    def test_gallery_filters_new_families_and_loads_only_visible_thumbnails(self):
        from app_plugins.builtin.batch_text_video.caption_gallery import CaptionGallery
        gallery=CaptionGallery();self.addCleanup(gallery.close)
        gallery.resize(850,650);gallery.show()
        with patch('app_plugins.builtin.batch_text_video.caption_gallery.sample',return_value=QtGui.QImage(440,248,QtGui.QImage.Format.Format_RGB32)) as render:
            for _ in range(30):self.app.processEvents()
            self.assertLess(render.call_count,20)
            self.assertGreater(render.call_count,0)
            self.assertGreater(len(gallery.pending),50)
        gallery.families.setCurrentIndex(gallery.families.findData('new'))
        visible=[gallery.list.item(i) for i in range(gallery.list.count()) if not gallery.list.item(i).isHidden()]
        self.assertEqual(len(visible),20)
        gallery.families.setCurrentIndex(gallery.families.findData('口播'))
        self.assertEqual(sum(not gallery.list.item(i).isHidden() for i in range(gallery.list.count())),5)
        gallery.search.setText('关键词')
        self.assertEqual(sum(not gallery.list.item(i).isHidden() for i in range(gallery.list.count())),1)

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Requires explicit real FFmpeg')
    def test_all_twenty_presets_really_encode_including_follow_feather_inputs(self):
        from app_plugins.builtin.batch_text_video.caption_clip import append_caption
        words=[dict(text=t,start=i*.6,end=i*.6+.5) for i,t in enumerate(('Božia','láska','dáva','nádej'))]
        qa=os.environ.get('LZX_MODERN_CAPTION_QA')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for i,key in enumerate(PRESETS):
                with self.subTest(preset=key):
                    layer=self.layer(key)
                    folder=root/str(i);folder.mkdir()
                    ass=folder/'caption.ass';write_caption_ass(ass,words,layer,360,640,3.)
                    args=[os.environ['NODE_FFMPEG'],'-nostdin','-hide_banner','-loglevel','error',
                        '-filter_complex_threads','1','-f','lavfi','-i','color=c=0x263748:s=360x640:r=5:d=3']
                    filters=[];append_caption(args,filters,'0:v','out',ass,layer,360,640,3.,5,1)
                    result=subprocess.run(args+['-filter_complex',';'.join(filters),'-map','[out]',
                        '-ss','1','-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],
                        cwd=folder,capture_output=True,timeout=30)
                    self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
                    image=QtGui.QImage.fromData(result.stdout,'PNG');self.assertFalse(image.isNull())
                    bright=sum(image.pixelColor(x,y).red()>170 or image.pixelColor(x,y).green()>170
                               for y in range(image.height()) for x in range(image.width()))
                    self.assertGreater(bright,50)
                    if qa:
                        target=Path(qa)/'native-preset-frames';target.mkdir(exist_ok=True)
                        image.save(str(target/(key.split(':')[-1]+'.png')))

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Requires explicit real FFmpeg')
    def test_real_native_keyword_color_and_size_match_qt_without_realigning(self):
        layer=self.layer(font_min=150,font_max=150,caption_words=2,caption_per_line=2,
                         width=96,height=90,y=50,outline_width=0,shadow_enabled=False)
        layer['caption_effects'].update(keyword_color='#ff0000',keyword_scale=1.4,keyword_terms='hope')
        words=[dict(text='hope',start=0,end=1),dict(text='ordinary',start=1,end=2)]
        qt,_=render_design(layer,540,400,words=words,duration=3,time=.5)
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);ass=folder/'caption.ass'
            write_caption_ass(ass,words,layer,540,400,3.)
            result=subprocess.run([os.environ['NODE_FFMPEG'],'-nostdin','-hide_banner','-loglevel','error',
                '-f','lavfi','-i','color=c=black:s=540x400:r=2:d=3','-vf','ass=caption.ass',
                '-frames:v','1','-f','image2pipe','-vcodec','png','pipe:1'],cwd=folder,capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
            native=QtGui.QImage.fromData(result.stdout,'PNG')
            def red_pixels(image):
                return sum(image.pixelColor(x,y).red()>180 and image.pixelColor(x,y).green()<40
                           for y in range(image.height()) for x in range(image.width()))
            self.assertGreater(red_pixels(qt),300)
            self.assertAlmostEqual(red_pixels(native)/red_pixels(qt),1,delta=.2)


class ParameterPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def panel(self):
        layer=normalize_layers([apply_style(new_caption(),'modern:talk_highlight',reset_layout=True)])[0]
        layer['caption_effects'].update(color_high=['#ff3b65','#6a5cff','#43dce3'],floating_amplitude=37)
        panel=LayerPanel([layer],'.');panel.select_layer_id(layer['id']);panel.hide_catalog()
        self.addCleanup(panel.close)
        owner=panel.caption_properties;owner.show_parameters();self.app.processEvents()
        window=owner.parameter_window;self.addCleanup(window.close)
        window.only_changed.setChecked(False)
        return panel,window

    def items(self,window):
        return {tuple(item.data(0,QtCore.Qt.ItemDataRole.UserRole)):item for item in
                [window.table.topLevelItem(i) for i in range(window.table.topLevelItemCount())]}

    def test_color_chips_boolean_states_chinese_choices_and_units_are_semantic(self):
        panel,window=self.panel();items=self.items(window)
        color=items[('effect','color_high')]
        self.assertIn('渐变',color.text(1));self.assertFalse(color.icon(1).isNull())
        self.assertEqual(color.data(1,VALUE_ROLE)['colors'],['#ffff3b65','#ff6a5cff','#ff43dce3'])
        numeric=items[('effect','floating_amplitude')]
        self.assertEqual(numeric.text(1),'37 px');self.assertEqual(numeric.data(1,VALUE_ROLE)['raw'],37)
        self.assertEqual(items[('effect','keyword_emphasis_enabled')].text(1),'关闭')
        self.assertFalse(items[('effect','keyword_emphasis_enabled')].icon(1).isNull())
        self.assertEqual(items[('layer','outline_position')].text(1),'向外（保留字面）')
        self.assertEqual(items[('layer','width')].text(1),'90 %')

    def test_alpha_checkerboard_and_palette_swatches_have_visible_distinct_colors(self):
        control=QtWidgets.QLineEdit()
        info=describe(control,['#80ff0000','#ff0000ff'],'caption_group_palette','配色')
        image=swatch(info,self.app.palette(),width=100,height=24).toImage()
        self.assertGreater(image.pixelColor(20,12).red(),image.pixelColor(20,12).blue())
        self.assertGreater(image.pixelColor(80,12).blue(),image.pixelColor(80,12).red())
        self.assertFalse(info['gradient'])

    def test_pixel_editor_never_mislabels_canonical_geometry_or_opacity_ranges(self):
        spin=QtWidgets.QDoubleSpinBox();spin.setSuffix(' px')
        self.assertEqual(describe(spin,90,'width','框宽度（%）')['text'],'90 %')
        self.assertEqual(describe(spin,50,'x','横向中心（%）')['text'],'50 %')
        spin.setSuffix('')
        self.assertEqual(describe(spin,128,'opacity_high_bg','高亮底色不透明度')['text'],'128 / 255')
        self.assertEqual(describe(spin,.3,'scrolling_opacity_context','上下文透明度')['text'],'0.3 / 1')

    def test_type_filter_hex_search_and_detail_selection_do_not_edit_any_parameter(self):
        panel,window=self.panel();before=copy.deepcopy(panel.current());changes=[]
        panel.changed.connect(lambda:changes.append(1))
        window.value_type.setCurrentIndex(window.value_type.findData('color'))
        visible=[v for v in self.items(window).values() if not v.isHidden()]
        self.assertTrue(visible);self.assertTrue(all(v.data(1,VALUE_ROLE)['kind']=='color' for v in visible))
        window.search.setText('#6a5cff')
        shown=[v for v in self.items(window).values() if not v.isHidden()]
        self.assertEqual(len(shown),1)
        window.table.setCurrentItem(shown[0]);self.app.processEvents()
        self.assertEqual(window.detail_title.text(),'读到的词颜色')
        self.assertFalse(window.detail_values[0][1].pixmap().isNull())
        self.assertTrue(window.locate_button.isEnabled())
        window.locate_button.click();QtTest.QTest.qWait(100)
        self.assertTrue(window.isVisible());self.assertIsNotNone(window.flash)
        self.assertEqual(panel.current(),before);self.assertFalse(changes)
        qa=os.environ.get('LZX_MODERN_CAPTION_QA')
        if qa:
            window.search.clear();window.resize(1220,720);self.app.processEvents()
            self.assertTrue(window.grab().save(str(Path(qa)/'parameter-overview.png')))


if __name__=='__main__':unittest.main()
