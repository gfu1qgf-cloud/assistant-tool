import copy
from pathlib import Path
import tempfile
import unittest

from qt_compat import QtCore,QtWidgets
from app_plugins.builtin.batch_text_video.captions import new_caption,caption_events,write_caption_ass
from app_plugins.builtin.batch_text_video.caption_ui import CaptionProperties
from app_plugins.builtin.batch_text_video.caption_styles import apply_style,PREFIX
from app_plugins.builtin.batch_text_video.caption_style_catalog import PRESETS
from app_plugins.builtin.batch_text_video.caption_style_render import make_primitives
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.store import Store,fresh_state
from app_plugins.builtin.batch_text_video.component_nodes import component_graph
from app_plugins.builtin.batch_text_video.node_effects import fallback_registry

WORDS=[dict(text='Ámen.',start=.1+i*.4,end=.4+i*.4) for i in range(6)]


class CaptionRandomToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_unspecified_defaults_off_explicit_saved_switch_is_preserved_and_boolean(self):
        old=new_caption(); old.pop('caption_random_size')
        value=normalize_layers([old])[0]
        self.assertFalse(value['caption_random_size'])
        self.assertTrue(normalize_layers([{**old,'caption_random_size':True}])[0]['caption_random_size'])
        old=copy.deepcopy(value); old.pop('caption_random_size')
        self.assertEqual(caption_events(WORDS,old,1080,1920,3),caption_events(WORDS,value,1080,1920,3))
        for invalid in (0,1,'false',None):
            with self.subTest(value=invalid),self.assertRaises(ValueError):
                normalize_layers([{**new_caption(),'caption_random_size':invalid}])

    def test_ui_exposes_switch_and_restores_saved_contrast_when_reenabled(self):
        layer=new_caption(); layer.update(caption_random_size=True,caption_size_contrast=2.75,caption_seed=42)
        editor=CaptionProperties(); editor.set_layer(layer)
        changes=[]; editor.changed.connect(changes.append)
        try:
            switch=editor.controls['caption_random_size']; contrast=editor.controls['caption_size_contrast']
            self.assertEqual(switch.accessibleName(),'启用随机字号')
            switch.setChecked(False)
            self.assertFalse(changes[-1]['caption_random_size'])
            self.assertTrue(contrast.isEnabled())
            self.assertEqual(changes[-1]['caption_size_contrast'],2.75)
            editor.set_layer(changes[-1])
            switch.setChecked(True)
            self.assertTrue(contrast.isEnabled())
            self.assertEqual(changes[-1]['caption_size_contrast'],2.75)
            self.assertEqual(changes[-1]['caption_seed'],42)
        finally:
            editor.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)

    def test_builtin_off_is_fixed_midrange_but_pop_is_preserved(self):
        layer=new_caption(); layer.update(caption_random_size=False,font_min=25,font_max=50,
            width=98,height=80,caption_per_line=3,caption_words=3)
        layer=normalize_layers([layer])[0]
        events=caption_events(WORDS,layer,1080,1920,3)
        self.assertTrue(all(e['path']==events[0]['path'] for e in events))
        self.assertEqual(events,caption_events(WORDS,{**layer,'caption_size_contrast':4},1080,1920,3))
        self.assertEqual(layer['caption_preset'],'pop')
        self.assertGreater(layer['caption_pop_ms'],0)

    def test_catalog_and_random_pulse_use_the_same_switch(self):
        names=['逐词弹出(随机大小)']
        names.append(next(name for name,value in PRESETS.items() if value.get('anim_in_type')=='word_pop_random_pulse'))
        for name in names:
            with self.subTest(preset=name):
                layer=apply_style(new_caption(),PREFIX+name)
                layer.update(caption_random_size=False,caption_size_contrast=3,width=96,height=80)
                layer=normalize_layers([layer])[0]
                fixed=caption_events(WORDS,layer,1080,1920,3)
                equivalent=caption_events(WORDS,{**layer,'caption_random_size':True,'caption_size_contrast':0},1080,1920,3)
                self.assertEqual(fixed,equivalent)
                primitives,_=make_primitives(WORDS,layer,1080,1920,3)
                pulses={p.get('pulse') for p in primitives if p['motion']=='word_pop_random_pulse'}
                self.assertLessEqual(len(pulses),1)

    def test_profile_and_converted_node_preserve_disabled_switch(self):
        layer=new_caption(); layer.update(caption_random_size=False,caption_size_contrast=3)
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory); state=fresh_state(); state['settings']['layers'].append(layer)
            store.save(state,0)
            saved=next(v for v in store.load()['settings']['layers'] if v['kind']=='caption')
            self.assertFalse(saved['caption_random_size'])
            graph,_=component_graph(saved,state['settings'],fallback_registry())
            style=next(n for n in graph['nodes'] if n['kind']=='batch_text_video.caption')['params']['caption']
            self.assertFalse(style['caption_random_size'])
            self.assertEqual(style['caption_size_contrast'],3)

    def test_export_reuses_switch_without_touching_words_or_timestamps(self):
        layer=new_caption(); layer.update(caption_random_size=False,caption_size_contrast=4)
        layer=normalize_layers([layer])[0]
        before=copy.deepcopy(WORDS)
        with tempfile.TemporaryDirectory() as directory:
            a=Path(directory)/'off.ass'; b=Path(directory)/'zero.ass'
            write_caption_ass(a,WORDS,layer,1080,1920,3)
            write_caption_ass(b,WORDS,{**layer,'caption_random_size':True,'caption_size_contrast':0},1080,1920,3)
            self.assertEqual(a.read_bytes(),b.read_bytes())
        self.assertEqual(WORDS,before)


if __name__=='__main__': unittest.main()
