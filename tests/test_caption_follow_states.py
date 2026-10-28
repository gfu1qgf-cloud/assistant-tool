"""Unread/active/read phases share exact spoken boundaries in Qt and ASS."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.caption_follow import new_follow_caption
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives, motion_at, render_design
from app_plugins.builtin.batch_text_video.caption_styles import apply_style, PREFIX
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.captions import write_caption_ass, filter_path
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.layout import ensure_fonts
from app_plugins.builtin.batch_text_video.store import Store

WORDS = [dict(text='Alpha', start=1., end=2.)]
KEYS = ('caption_context_opacity', 'caption_active_opacity', 'caption_read_opacity')


class FollowStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layer(self, **values):
        layer = new_follow_caption()
        layer.update(font='Arial', font_min=72, font_max=72, width=90, height=60,
                     x=50, y=50, outline_width=0, background_enabled=False,
                     shadow_enabled=False, color='#FFFFFF', caption_follow_mode='line',
                     caption_per_line=4)
        layer['caption_effects'].update(anim_in_type='none', anim_out_type='none',
            karaoke_highlight=False, color_text='#FFFFFF')
        layer.update(values)
        return normalize_layers([layer])[0]

    def primitive_at(self, layer, time):
        primitives, _ = make_primitives(WORDS, layer, 320, 240, 3.)
        return next(p for p in primitives if p['z']==0 and p.get('follow_spoken')==(1.,2.)
                    and p['start'] <= time < p['end'])

    def test_old_context_value_becomes_unread_and_read_defaults_to_full_opacity(self):
        layer = self.layer(caption_context_opacity=25)
        for key in KEYS[1:]: layer.pop(key)
        before = copy.deepcopy(layer)
        normalized = normalize_layers([layer])[0]
        self.assertEqual([normalized[key] for key in KEYS], [25,100,100])
        self.assertEqual(layer, before)
        for time, expected in ((.5,.25),(1.,1.),(1.999,1.),(2.,1.),(2.8,1.)):
            primitive = self.primitive_at(normalized,time)
            self.assertAlmostEqual(motion_at(primitive,time)[3], expected)

    def test_each_state_is_independent_and_zero_really_hides_only_that_phase(self):
        for key in KEYS:
            for amount in (0,37,100):
                with self.subTest(key=key, amount=amount):
                    values = dict(zip(KEYS,(20,60,90))); values[key]=amount
                    layer = self.layer(**values)
                    for phase,time in zip(KEYS,(.5,1.5,2.5)):
                        primitive = self.primitive_at(layer,time)
                        self.assertAlmostEqual(motion_at(primitive,time)[3],values[phase]/100)

    def test_transition_uses_spoken_end_and_stays_read_through_a_silent_gap(self):
        words = WORDS+[dict(text='Beta',start=2.7,end=2.9)]
        layer = self.layer(caption_context_opacity=10,caption_active_opacity=50,caption_read_opacity=80)
        primitives, _ = make_primitives(words,layer,320,240,3.)
        for time, expected in ((.999,.1),(1.,.5),(1.999999,.5),(2.,.8),(2.6,.8)):
            p=next(p for p in primitives if p['z']==0 and p.get('follow_spoken')==(1.,2.)
                   and p['start']<=time<p['end'])
            self.assertAlmostEqual(motion_at(p,time)[3],expected)

    def test_values_are_validated_and_follow_off_does_not_apply_state_opacity(self):
        for key in KEYS:
            for value in (True,-1,101,float('nan'),'50'):
                with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                    self.layer(**{key:value})
        layer=self.layer(caption_follow_mode='none',caption_context_opacity=0,
                         caption_active_opacity=0,caption_read_opacity=0)
        primitives,_=make_primitives(WORDS,layer,320,240,3.)
        visible=[p for p in primitives if p['z']==0 and p['start']<=1.5<p['end']]
        self.assertTrue(visible)
        self.assertTrue(all(motion_at(p,1.5)[3]==1 for p in visible))

    def test_properties_and_parameter_overview_expose_all_three_values(self):
        from app_plugins.builtin.batch_text_video.caption_parameter_window import parameter_targets
        panel=CaptionProperties(); self.addCleanup(panel.close)
        panel.set_layer(self.layer())
        changes=[]; panel.changed.connect(changes.append)
        for key,value in zip(KEYS,(15,55,95)):
            panel.controls[key].setValue(value)
            self.assertTrue(panel.follow_section.isAncestorOf(panel.controls[key]))
        self.assertEqual([changes[-1][key] for key in KEYS],[15,55,95])
        targets=parameter_targets(panel)
        self.assertEqual([targets[('layer',key)][1] for key in KEYS],
                         ['未读不透明度','正在读不透明度','已读不透明度'])
        # Preserve the user's existing horizontal controls and tracking choice.
        self.assertIn('caption_focus_x',panel.controls)

    def test_preset_switch_project_and_node_roundtrip_preserve_three_states(self):
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        layer=self.layer(caption_context_opacity=12,caption_active_opacity=58,caption_read_opacity=94)
        styled=apply_style(layer,PREFIX+'默认白字')
        self.assertEqual([styled[key] for key in KEYS],[12,58,94])
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);state=store.load();state['settings']['layers']=normalize_layers([layer])
            store.save(state,0)
            saved=next(item for item in store.load()['settings']['layers'] if item['id']==layer['id'])
            self.assertEqual([saved[key] for key in KEYS],[12,58,94])
        changes=[];editor=CaptionNodeEditor({},changes.append);self.addCleanup(editor.close)
        editor.mode.setCurrentIndex(1)
        panel=editor.panel.caption_properties
        for key,value in zip(KEYS,(21,61,91)):panel.controls[key].setValue(value)
        caption=changes[-1]['caption']
        self.assertEqual([caption[key] for key in KEYS],[21,61,91])

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'),'Requires explicit real FFmpeg')
    def test_libass_frames_match_qt_for_all_three_states_and_hidden_read_state(self):
        ffmpeg=os.environ['NODE_FFMPEG']
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            for read in (100,0):
                layer=self.layer(caption_context_opacity=20,caption_active_opacity=60,caption_read_opacity=read)
                ass=folder/('states-'+str(read)+'.ass')
                write_caption_ass(ass,WORDS,layer,320,240,3.)
                for label,time,amount in (('unread',.5,20),('active',1.5,60),('read',2.5,read)):
                    qt,_=render_design(layer,320,240,words=WORDS,duration=3.,time=time)
                    peak=max(qt.pixelColor(x,y).alpha() for y in range(qt.height()) for x in range(qt.width()))
                    self.assertAlmostEqual(peak,round(255*amount/100),delta=1)
                    target=folder/(str(read)+'-'+label+'.png')
                    subprocess.run([ffmpeg,'-y','-nostdin','-hide_banner','-loglevel','error',
                        '-f','lavfi','-i','color=c=black:s=320x240:r=25:d=3',
                        '-vf','ass='+filter_path(ass),'-ss',str(time),'-frames:v','1',str(target)],
                        check=True,capture_output=True,timeout=30)
                    native=QtGui.QImage(str(target))
                    brightness=max(native.pixelColor(x,y).red() for y in range(native.height()) for x in range(native.width()))
                    self.assertAlmostEqual(brightness,peak,delta=5)
                    qa=os.environ.get('LZX_CAPTION_STATES_QA')
                    if qa and read==100:
                        Path(qa).mkdir(parents=True,exist_ok=True)
                        qt.save(str(Path(qa)/(label+'-qt.png')))
                        native.save(str(Path(qa)/(label+'-ffmpeg.png')))


if __name__=='__main__':unittest.main()
