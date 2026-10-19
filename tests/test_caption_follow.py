import copy
import os
import re
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption,track_for,offset_at,DEFAULTS
from app_plugins.builtin.batch_text_video.caption_style_render import groups_and_events,make_primitives,motion_at,render_design
from app_plugins.builtin.batch_text_video.caption_styles import apply_style,PREFIX
from app_plugins.builtin.batch_text_video.captions import new_caption,write_caption_ass,design_layers,load_word_timings
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts,render_preview_overlay
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.caption_follow_preview import prepare

TEXT='Pane prosím ochraňuj moju rodinu a naplň naše srdcia pokojom každý deň'
WORDS=[dict(text=t,start=.3+i*.4,end=.6+i*.4) for i,t in enumerate(TEXT.split())]
DURATION=6.


class CaptionFollowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layer(self,**values):
        layer=new_follow_caption()
        layer.update(font='Arial',font_min=48,font_max=48,width=82,height=32,x=50,y=50,outline_width=0,
                     background_enabled=False,shadow_enabled=False,caption_per_line=3)
        layer.update(values)
        return normalize_layers([layer])[0]

    def test_legacy_components_default_to_disabled_follow_and_no_input_mutation(self):
        layer=new_caption()
        for key in DEFAULTS: layer.pop(key,None)
        before=copy.deepcopy(layer)
        self.assertEqual(normalize_layers([layer])[0]['caption_follow_mode'],'none')
        self.assertEqual(layer,before)

    def test_long_text_keeps_all_words_and_does_not_shrink_to_fit_full_height(self):
        words=[dict(text='Slovensko',start=i*.25,end=(i+1)*.25) for i in range(180)]
        layer=self.layer(caption_per_line=2)
        events,groups,_=groups_and_events(words,layer,540,960,46.)
        self.assertEqual(len(events),180)
        self.assertEqual({e['_font_pixel_size'] for e in events},{24})
        self.assertGreater(events[-1]['y'],960)
        self.assertEqual([(e['start'],e['spoken_end']) for e in events],[(w['start'],w['end']) for w in words])

    def test_word_focus_arrives_on_time_and_last_word_stays_centered(self):
        layer=self.layer(caption_focus_x=35,caption_focus_y=60)
        events,_,_=groups_and_events(WORDS,layer,540,960,DURATION)
        box=QtCore.QRectF(540*.09,960*.34,540*.82,960*.32)
        track=track_for(events,layer,box,DURATION)
        for event in events:
            dx,dy=offset_at(track,event['start'])
            self.assertAlmostEqual(event['x']+event['width']/2+dx,box.left()+box.width()*.35,places=6)
            self.assertAlmostEqual(event['_follow_line_center']+dy,box.top()+box.height()*.6,places=6)
        self.assertEqual(offset_at(track,DURATION),offset_at(track,events[-1]['start']))

    def test_line_mode_does_not_pan_horizontally_or_jitter_for_accents(self):
        layer=self.layer(caption_follow_mode='line')
        events,_,_=groups_and_events(WORDS,layer,540,960,DURATION)
        box=QtCore.QRectF(48,300,440,300)
        track=track_for(events,layer,box,DURATION)
        self.assertTrue(all(p[1]==0 for p in track))
        first=[e for e in events if e['line']==0]
        self.assertEqual(len({offset_at(track,e['start'])[1] for e in first}),1)

    def test_zero_smoothing_is_a_cut_in_both_preview_and_ass_not_a_full_word_pan(self):
        layer=self.layer(caption_follow_ms=0)
        events,_,_=groups_and_events(WORDS,layer,540,960,DURATION)
        box=QtCore.QRectF(48,300,440,300);track=track_for(events,layer,box,DURATION)
        self.assertEqual(offset_at(track,events[1]['start']-.0001),offset_at(track,events[0]['start']))
        self.assertNotEqual(offset_at(track,events[1]['start']),offset_at(track,events[0]['start']))
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cuts.ass';write_caption_ass(path,WORDS,layer,540,960,DURATION)
            moves=re.findall(r'\\move\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+),',path.read_text('utf8'))
            self.assertTrue(moves)
            self.assertTrue(all(a==c and b==d for a,b,c,d in moves))

    def test_long_follow_export_is_clipped_and_bounded_without_losing_words(self):
        words=[dict(text='Slovensko',start=i*.25,end=(i+1)*.25) for i in range(180)]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'long.ass'
            events=write_caption_ass(path,words,self.layer(caption_per_line=2),540,960,46.)
            self.assertEqual(len(events),180)
            self.assertLess(path.stat().st_size,64*1024*1024)

    def test_follow_parameters_are_editable_and_saved_in_caption_node(self):
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        changes=[];editor=CaptionNodeEditor({},changes.append);self.addCleanup(editor.close)
        editor.mode.setCurrentIndex(1)
        properties=editor.panel.caption_properties
        properties.controls['caption_follow_mode'].setCurrentIndex(
            properties.controls['caption_follow_mode'].findData('word'))
        properties.controls['caption_focus_y'].setValue(62)
        self.assertEqual(changes[-1]['caption']['caption_follow_mode'],'word')
        self.assertEqual(changes[-1]['caption']['caption_focus_y'],62)

    def test_sentence_timing_is_rejected_for_word_mode_but_accepted_for_line(self):
        words=[dict(w,start=.2,end=4.,cue=0) for w in WORDS]
        with self.assertRaisesRegex(ValueError,'逐词时间戳'): make_primitives(words,self.layer(),540,960,DURATION)
        values,_=make_primitives(words,self.layer(caption_follow_mode='line'),540,960,DURATION)
        self.assertTrue(values)

    def test_preset_switch_keeps_follow_settings_and_ui_roundtrip_does_not_lock_them(self):
        layer=self.layer(caption_focus_x=31,caption_focus_y=64,caption_follow_ms=320)
        result=apply_style(layer,PREFIX+'默认白字')
        self.assertEqual({k:result[k] for k in DEFAULTS},{k:layer[k] for k in DEFAULTS})
        panel=CaptionProperties();self.addCleanup(panel.close)
        panel.set_layer(result)
        panel.controls['caption_focus_x'].setValue(42)
        panel.set_layer(panel.layer)
        self.assertEqual(panel.controls['caption_focus_x'].value(),42)
        self.assertTrue(panel.controls['caption_follow_mode'].isEnabled())

    def test_ass_and_preview_share_track_and_clip_and_opacity(self):
        layer=self.layer(caption_context_opacity=25)
        primitives,events=make_primitives(WORDS,layer,540,960,DURATION)
        foreground=[p for p in primitives if p['z']==0 and p.get('follow_track')]
        self.assertTrue(foreground)
        for primitive in foreground:
            self.assertIn('screen_clip',primitive)
        context=next(p for p in foreground if p['start']==0)
        self.assertAlmostEqual(motion_at(context,0)[3],.25)
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'follow.ass'
            write_caption_ass(target,WORDS,layer,540,960,DURATION)
            data=target.read_text('utf8')
            self.assertIn('\\clip(',data);self.assertIn('\\move(',data)
            self.assertNotIn('Pane',data)
        design=design_layers([layer],'',TEXT)[0]
        self.assertEqual(len(design['text'].split()),len(WORDS))
        a,_=render_design(design,540,960,words=WORDS,duration=DURATION,time=.4)
        b,_=render_design(design,540,960,words=WORDS,duration=DURATION,time=4.7)
        self.assertNotEqual(a,b)

    def test_preview_reads_manual_word_srt_and_never_calls_a_model(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'words.srt'
            path.write_text('1\n00:00:00,100 --> 00:00:00,800\nPane\n\n2\n00:00:01,000 --> 00:00:01,800\nprosím\n',encoding='utf8')
            layer=self.layer(caption_timing='file',caption_file=str(path))
            settings={'layers':[layer],'_editor_timing':{'duration':2.,'time':1.3}}
            before=copy.deepcopy(settings)
            with patch('app_plugins.builtin.batch_text_video.captions.whisper_inference',side_effect=AssertionError('model')):
                preview,notices=prepare(settings,{'body':'Pane prosím'},folder)
            self.assertEqual(settings,before)
            self.assertEqual([w['text'] for w in preview['_caption_preview_words'][layer['id']]],['Pane','prosím'])
            self.assertIn('现有时间戳',notices[0])

    def test_cached_only_alignment_never_creates_cache_or_loads_model(self):
        from globalValue import globalValue
        with tempfile.TemporaryDirectory() as folder:
            voice=Path(folder)/'audio.wav';voice.write_bytes(b'synthetic')
            with patch.object(globalValue,'whisper_model_name',return_value='test-model'), \
                    patch('app_plugins.builtin.batch_text_video.captions.whisper_inference',side_effect=AssertionError('model')):
                self.assertIsNone(load_word_timings(voice,'Pane',2.,folder,cache_only=True))
            self.assertFalse((Path(folder)/'caption-cache').exists())

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'需要 FFmpeg 原生画面验证')
    def test_native_libass_frames_match_qt_focus_and_stay_inside_viewport(self):
        from qt_compat import QtGui
        from app_plugins.builtin.batch_text_video.captions import filter_path
        folder=Path(os.environ.get('LZX_FOLLOW_QA') or tempfile.mkdtemp())
        folder.mkdir(parents=True,exist_ok=True)
        layer=self.layer(caption_context_opacity=25)
        ass=folder/'follow.ass';write_caption_ass(ass,WORDS,layer,540,960,DURATION)
        design=design_layers([layer],'',TEXT)[0]
        def cyan_bounds(image):
            pixels=[]
            for y in range(image.height()):
                for x in range(image.width()):
                    c=image.pixelColor(x,y)
                    if c.alpha()>100 and c.green()>140 and c.blue()>160 and c.red()<130:
                        pixels.append((x,y))
            self.assertTrue(pixels,'当前词必须有清晰高亮')
            return min(x for x,y in pixels),min(y for x,y in pixels),max(x for x,y in pixels),max(y for x,y in pixels)
        for label,time in (('start',.4),('later',4.4)):
            qt,_=render_design(design,540,960,words=WORDS,duration=DURATION,time=time)
            qt.save(str(folder/(label+'-qt.png')))
            target=folder/(label+'-ass.png')
            subprocess.run([os.environ['NODE_FFMPEG'],'-y','-nostdin','-hide_banner','-loglevel','error',
                '-f','lavfi','-i','color=c=black:s=540x960:r=25:d=6',
                '-vf','ass='+filter_path(ass),'-ss',str(time),'-frames:v','1',str(target)],
                check=True,timeout=60,capture_output=True)
            native=QtGui.QImage(str(target));a=cyan_bounds(qt);b=cyan_bounds(native)
            self.assertLess(abs((b[0]+b[2])/2-270),4)
            self.assertLess(max(abs(x-y) for x,y in zip(a,b)),4)


if __name__=='__main__':unittest.main()
