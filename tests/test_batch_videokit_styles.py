import copy
import gc
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.caption_styles import (
    apply_style, effects, preset_choices, PREFIX, description)
from app_plugins.builtin.batch_text_video.caption_style_catalog import PRESETS
from app_plugins.builtin.batch_text_video.caption_style_render import (
    make_primitives, motion_at, groups_and_events, render_design)
from app_plugins.builtin.batch_text_video.captions import new_caption, write_caption_ass, design_layers, filter_path
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.caption_effect_ui import CaptionEffectEditor
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel

WORDS = [{'text':'Božia','start':.25,'end':.8}, {'text':'láska','start':.95,'end':1.5},
         {'text':'je','start':1.65,'end':2.0}, {'text':'s','start':2.1,'end':2.5},
         {'text':'nami.','start':2.65,'end':3.2}]


def component(name,**values):
    layer = apply_style(new_caption(),PREFIX+name,reset_layout=True)
    layer.update(values)
    return normalize_layers([layer])[0]


class VideoKitStylesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def tearDown(self):
        gc.collect()
        QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_all_65_presets_validate_draw_and_write_ass(self):
        self.assertEqual(len(PRESETS),65)
        self.assertEqual(len(preset_choices()),66)
        with tempfile.TemporaryDirectory() as folder:
            for index,name in enumerate(PRESETS):
                with self.subTest(preset=name):
                    layer = component(name)
                    path = Path(folder)/f'{index}.ass'
                    events = write_caption_ass(path,WORDS,layer,360,640,3.5)
                    self.assertEqual([e['text'] for e in events if not e['context']],[w['text'] for w in WORDS])
                    self.assertGreater(path.stat().st_size,400)
                    data = path.read_text('utf8')
                    self.assertIn('Dialogue:',data)
                    self.assertNotIn('Božia',data)  # Only safe vector paths, no ASS text injection.
                    design = design_layers([layer],'','Božia láska je s nami.')[0]
                    image,size = render_design(design,360,640)
                    self.assertFalse(image.isNull())
                    self.assertGreater(size,0)

    def test_applying_or_resetting_styles_preserves_timing_and_text(self):
        layer = new_caption()
        layer.update(caption_timing='file',caption_file='task.srt',caption_language='sk',caption_source='full')
        layer['caption_effects']={'global_mask_enabled':True}
        before = copy.deepcopy(layer)
        result = apply_style(layer,PREFIX+'渐变蓝色框')
        self.assertEqual(layer,before)
        self.assertEqual(result['caption_effects_version'],1)
        self.assertFalse(result['caption_effects']['global_mask_enabled'])
        self.assertIn('anim_in_type',result['caption_effects'])
        for key in ('id','caption_timing','caption_file','caption_language','caption_source'):
            self.assertEqual(result[key],before[key])
        changed = apply_style(result,PREFIX+'默认白字')
        self.assertFalse(effects(changed)['use_box'])
        self.assertFalse(effects(changed)['bg_gradient_enabled'])

    def test_timing_events_are_not_reestimated_by_style(self):
        for name in PRESETS:
            with self.subTest(name=name):
                values,_groups,_style = groups_and_events(WORDS,component(name),360,640,3.5)
                self.assertEqual([(v['start'],v['spoken_end']) for v in values if not v['context']],
                    [(v['start'],v['end']) for v in WORDS])

    def test_multiword_srt_does_not_invent_word_timing(self):
        words = [{**w,'start':.2,'end':3.2,'cue':0} for w in WORDS]
        for name in ('卡拉OK高亮','逐词弹出(随机回弹)','全屏打字机_金黄光标'):
            values,_groups,_style = groups_and_events(words,component(name),360,640,3.5)
            self.assertEqual({v['start'] for v in values},{.2})
            self.assertEqual({v['spoken_end'] for v in values},{3.2})

    def test_distinct_effect_families_really_render_differently(self):
        samples = {}
        with tempfile.TemporaryDirectory() as folder:
            for name in ('默认白字','卡拉OK高亮','粉色气泡','上滑入场','悬浮漂移',
                         '渐变蓝色框','霓虹多层描边','逐词弹出(随机回弹)','全屏打字机_金黄光标'):
                path = Path(folder)/'style.ass'
                write_caption_ass(path,WORDS,component(name),360,640,3.5)
                samples[name] = path.read_text('utf8')
        self.assertEqual(len(set(samples.values())),len(samples))
        self.assertIn('\\blur',samples['霓虹多层描边'])
        self.assertIn('\\move',samples['上滑入场'])

    def test_current_word_box_stops_at_actual_word_end(self):
        values,_events = make_primitives(WORDS,component('撞色黄底框高亮'),360,640,3.5)
        high = [p for p in values if p['z']==-3]
        self.assertEqual([(p['start'],p['end']) for p in high],[(w['start'],w['end']) for w in WORDS])

    def test_scatter_and_font_sizes_are_deterministic(self):
        layer = component('随机分散气泡卡片')
        a,_groups,_style = groups_and_events(WORDS,layer,360,640,3.5)
        b,_groups,_style = groups_and_events(WORDS,layer,360,640,3.5)
        self.assertEqual([(e['x'],e['y'],e['path']) for e in a],[(e['x'],e['y'],e['path']) for e in b])
        c,_groups,_style = groups_and_events(WORDS,{**layer,'caption_seed':2},360,640,3.5)
        self.assertNotEqual([(e['x'],e['y']) for e in a],[(e['x'],e['y']) for e in c])

    def test_scrolling_lyrics_show_dim_context(self):
        layer = component('滚动歌词_粉高亮',caption_words=2,caption_per_line=2)
        primitives,events = make_primitives(WORDS,layer,360,640,3.5)
        self.assertTrue(any(e['context'] for e in events))
        self.assertTrue(any(0<p['opacity']<.5 for p in primitives if p['z']==0))

    def test_bad_effect_values_are_rejected(self):
        layer = component('默认白字')
        for value in ({'color_high':'{\\evil}'},{'anim_in_type':'eval'},
                      {'shadow_blur':float('nan')},{'floating_period':0},
                      {'dynamic_box':'yes'},{'arbitrary_script':'evil'}, {'metronome_bpm':0},
                      {'shadow_blur':'8'}, {'stroke_expand_layers':2.3}, {'global_mask_opacity':5}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                effects({**layer,'caption_effects':value})

    def test_ui_selects_style_and_preserves_regular_controls(self):
        widget = CaptionProperties()
        try:
            widget.set_layer(normalize_layers([new_caption()])[0])
            saved=[]; widget.changed.connect(saved.append)
            widget.controls['caption_preset'].setCurrentIndex(widget.controls['caption_preset'].findData(PREFIX+'渐变蓝色框'))
            self.assertTrue(saved[-1]['_caption_style_apply'])
            self.assertEqual(saved[-1]['caption_words'],2)
            widget.controls['caption_timing'].setCurrentIndex(widget.controls['caption_timing'].findData('uniform'))
            self.assertEqual(saved[-1]['caption_timing'],'uniform')
            self.assertNotIn('_caption_style_apply',saved[-1])
            dialog=CaptionEffectEditor(saved[-1])
            dialog.deleteLater()
            self.assertIn('播放预览',widget.style_note.text())
        finally: widget.deleteLater()

    def test_effect_change_does_not_overwrite_new_font_edit(self):
        from app_plugins.builtin.batch_text_video.store import fresh_state
        temporary = tempfile.TemporaryDirectory()
        state = fresh_state()
        panel = LayerPanel(state['settings']['layers'],temporary.name,settings=state['settings']); panel.add_caption()
        try:
            panel.caption_properties.controls['caption_preset'].setCurrentIndex(
                panel.caption_properties.controls['caption_preset'].findData(PREFIX+'默认白字'))
            panel.current()['font_weight']=888
            panel.caption_properties.controls['caption_hold_ms'].setValue(220)
            self.assertEqual(panel.current()['font_weight'],888)
        finally: panel.deleteLater(); temporary.cleanup()

    def test_independent_effect_dicts_no_shared_configuration(self):
        a,b = normalize_layers([new_caption()])[0],normalize_layers([new_caption()])[0]
        original = copy.deepcopy(b['caption_effects'])
        a['caption_effects']['test']=True
        self.assertEqual(b['caption_effects'],original)
        self.assertNotIn('test',b['caption_effects'])


class VideoKitNativeRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()
        cls.ffmpeg=Path(r'C:\Users\RTV\pythonPro\辅助小工具\runtime\ffmpeg\ffmpeg.exe')

    def test_all_effect_categories_are_accepted_by_real_ffmpeg(self):
        if not self.ffmpeg.is_file(): self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory() as folder:
            for name in ('卡拉OK高亮','随机分散气泡卡片','霓虹蓝粉渐变框','全屏打字机_金黄光标',
                         '逐词弹出(随机回弹)','上滑入场','悬浮漂移','粉色气泡','圣光降临',
                         '风驰幻影_白斜体','金色霓虹环境光','霓虹多层描边','阴影沉浸式'):
                with self.subTest(name=name):
                    path=Path(folder)/'style.ass'
                    write_caption_ass(path,WORDS,component(name),360,640,3.5)
                    result=subprocess.run([str(self.ffmpeg),'-hide_banner','-loglevel','error',
                        '-f','lavfi','-i','color=c=gray:s=360x640:r=10:d=3.5',
                        '-vf','format=rgba,ass=filename='+filter_path(path),'-f','null','-'],capture_output=True,timeout=30)
                    self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))

    def test_real_application_export_and_preview_keep_smooth_gradient_and_audio(self):
        if not self.ffmpeg.is_file():self.skipTest('FFmpeg unavailable')
        from app_plugins.builtin.batch_text_video.engine import render_static_text,probe
        from app_plugins.builtin.batch_text_video.store import fresh_state
        with tempfile.TemporaryDirectory(prefix='字幕样式测试 ') as folder:
            root=Path(folder);source=root/'background.mp4';voice=root/'task_audio.wav'
            def ffmpeg(args):
                result=subprocess.run([str(self.ffmpeg),'-hide_banner','-loglevel','error',*args],capture_output=True,timeout=35)
                self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
                return result.stdout
            ffmpeg(['-y','-f','lavfi','-i','color=c=gray:s=360x640:r=20:d=3.5','-c:v','libx264','-pix_fmt','yuv420p',str(source)])
            ffmpeg(['-y','-f','lavfi','-i','sine=frequency=330:duration=3.5',str(voice)])
            state=fresh_state();layer=component('霓虹蓝粉渐变框')
            state['settings'].update(aspect='source',use_voice=True,music_enabled=False,overwrite=True,
                ffmpeg_path=str(self.ffmpeg),layers=[layer]+[dict(item,enabled=False) for item in state['settings']['layers']])
            job={'id':'one','path':str(source),'voice_path':str(voice),'voice_override':True,'title':'',
                 'body':'Božia láska je s nami.','name':'成品','task_dir':str(root)}
            state['jobs']=[job];before=copy.deepcopy(state)
            with patch('app_plugins.builtin.batch_text_video.captions.load_word_timings',return_value=WORDS):
                output,_music=render_static_text(job,state,root)
                preview,_music=render_static_text(dict(job,name='预览'),state,root,preview=True)
            self.assertEqual(state,before)
            self.assertTrue(probe(output,str(self.ffmpeg.with_name('ffprobe.exe')))['audio'])
            self.assertTrue(probe(preview,str(self.ffmpeg.with_name('ffprobe.exe')))['audio'])
            image=QtGui.QImage.fromData(ffmpeg(['-ss','1.15','-i',str(output),'-frames:v','1','-f','image2pipe','-vcodec','png','-']))
            # The lower strip is inside the box but below every glyph/shadow;
            # measuring a text edge would incorrectly classify it as a seam.
            _events,groups,style=groups_and_events(WORDS,layer,360,640,3.5)
            active=next(group for group in groups if group['start']<=1.15<group['end'])
            bounds=active['bounds']; scale=360/1080
            sample_y=round(bounds.bottom()+style['box_padding_y']*scale/2)
            # Derive the strip from the layout: a fixed screen row can cross
            # text after a legitimate baseline/font-layout correction.
            self.assertGreater(sample_y,max(e['y']+e['_native_path'].boundingRect().bottom()
                                           for e in active['events']))
            left=round(bounds.left()+style['box_radius']*scale+2)
            right=round(bounds.right()-style['box_radius']*scale-2)
            self.assertGreater(right-left,100)
            pixels=[image.pixelColor(x,sample_y) for x in range(left,right)]
            differences=[max(abs(getattr(a,key)()-getattr(b,key)()) for key in ('red','green','blue'))
                for a,b in zip(pixels,pixels[1:])]
            self.assertLess(max(differences),18,'Gradient must not have alternating chroma seams')


if __name__=='__main__': unittest.main()
