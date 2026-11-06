import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
from app_plugins.builtin.batch_text_video import palette_rules as rules
from app_plugins.builtin.batch_text_video.palette_library import PaletteLibrary,defaults,new_palette,canonical
from app_plugins.builtin.batch_text_video.palette_ui import PaletteManager,PaletteChoice
from app_plugins.builtin.batch_text_video.color_ui import EffectColorEditor
from app_plugins.builtin.batch_text_video.captions import new_caption
from app_plugins.builtin.batch_text_video.text_colors import resolve_colors,job_color_scope
from app_plugins.builtin.batch_text_video.layers import normalize_layers

FF=os.environ.get('NODE_FFMPEG','')

def choice(colors=None,**values):
    return rules.normalize({'color':dict(mode='palette',colors=colors or ['#ff2244','#22cc88','#4488ff'],
        name='我的喜欢',palette_id='mine',seed=0,**values)})['color']

class ColorPaletteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.library=PaletteLibrary(self.root/'palettes.json')

    def test_defaults_save_custom_rename_delete_and_revision_conflict(self):
        start=self.library.load();self.assertEqual(len(start['palettes']),8);self.assertFalse(self.library.path.exists())
        custom=new_palette('我喜欢的三色',['red','#22cc88','#4488ff','red'])
        self.assertEqual(len(custom['colors']),3)
        first=self.library.save([*start['palettes'],custom],0)
        self.assertEqual(self.library.load(),first)
        with self.assertRaisesRegex(ValueError,'其他窗口'):self.library.save([],0)
        items=copy.deepcopy(first['palettes']);items[-1]['name']='新名字';self.library.save(items,1)
        self.library.save(items[:-1],2);self.assertEqual(len(self.library.load()['palettes']),8)
        self.assertIn(custom['id'],json.dumps(first))

    def test_invalid_library_never_silently_resets_or_overwrites(self):
        self.library.path.write_text('bad json',encoding='utf8')
        with self.assertRaises(ValueError):self.library.load()
        self.assertEqual(self.library.path.read_text(),'bad json')
        for colors in ([],['bad'],['red']*33):
            with self.assertRaises(ValueError):new_palette('mine',colors)

    def test_every_video_is_stable_each_field_and_seed_are_independent(self):
        rule=choice();value='#ffffff'
        outputs=[rules.choose(value,rule,'video-'+str(i),'layer','color') for i in range(40)]
        self.assertEqual(outputs,[rules.choose(value,rule,'video-'+str(i),'layer','color') for i in range(40)])
        self.assertEqual(set(outputs),set(rule['colors']))
        other=[rules.choose(value,rule,'video-'+str(i),'layer','outline_color') for i in range(40)]
        self.assertNotEqual(outputs,other)
        next_seed=[rules.choose(value,{**rule,'seed':1},'video-'+str(i),'layer','color') for i in range(40)]
        self.assertNotEqual(outputs,next_seed)

    def test_gradient_retains_slots_cycles_without_repeats_and_alpha_policy(self):
        rule=choice(['#80ff0000','#400000ff','#ffcc00'],alpha=True)
        result=rules.choose(['white']*3,rule,'video','caption','caption_effects/color_text')
        self.assertEqual(set(result),set(rule['colors']));self.assertTrue(any(len(c)==9 for c in result))
        rgb=rules.choose('#ffffff',{**rule,'alpha':False},'video','layer','color')
        self.assertEqual(len(rgb),7)

    def test_family_gray_does_not_invent_hue_and_preserves_alpha(self):
        rule=rules.normalize({'color':dict(mode='family',alpha=True)})['color']
        for i in range(20):
            color=QtGui.QColor(rules.choose('#80ffffff',rule,str(i),'a','color'))
            self.assertEqual(color.red(),color.green());self.assertEqual(color.green(),color.blue());self.assertEqual(color.alpha(),128)
        self.assertEqual(QtGui.QColor(rules.choose('#80ffffff',{**rule,'alpha':False},'x','a','color')).alpha(),128)

    def test_bad_paths_ranges_flags_and_group_single_colour_rejected(self):
        for path in ('path','caption_effects/anim_in_type','__class__/color','caption_effects/color_text/0'):
            with self.assertRaises(ValueError):rules.normalize({path:choice()})
        for data in ({'mode':'bad'},{'mode':'palette','colors':[]},{'mode':'family','hue':float('nan')},
                     {'mode':'family','seed':True},{'mode':'family','alpha':'false'}):
            with self.assertRaises(ValueError):rules.normalize({'color':data})
        with self.assertRaises(ValueError):rules.normalize({'caption_group_palette':choice(['red'])})

    def test_resolve_scalar_effect_gradient_group_and_tint_without_mutating_stored_values(self):
        layer=new_caption();layer.update(id='cap',color_mode='family',color_rules={
            'color':choice(['#22cc88']),'outline_color':choice(['#ff2244']),
            'caption_effects/color_text':choice(),'caption_effects/keyword_color':choice(['#4488ff']),
            'caption_group_palette':choice()})
        before=copy.deepcopy(layer);result=rules.resolve_object(layer,'video')
        self.assertEqual(result['color'],'#22cc88');self.assertEqual(result['color_mode'],'fixed')
        self.assertEqual(result['outline_color'],'#ff2244');self.assertNotIn('color_rules',result)
        self.assertTrue(result['caption_effects']['color_text']);self.assertEqual(result['caption_effects']['keyword_color'],'#4488ff')
        self.assertEqual(layer,before);self.assertEqual(resolve_colors([layer],'video')[0]['color'],'#22cc88')
        settings=dict(layers=[layer],color_rules={'background_tint_color':choice(['#ff2244'])},background_tint_color='#000000')
        runtime=rules.resolve_settings(settings,'video');self.assertEqual(runtime['background_tint_color'],'#ff2244')
        self.assertEqual(runtime['layers'][0]['color'],'#22cc88');self.assertIn('color_rules',settings)
        self.assertEqual(rules.resolve_settings(runtime,'another scope'),runtime)
        import threading
        signal=threading.Event();settings['_runtime_cancel']=signal
        self.assertIs(rules.resolve_settings(settings,'video')['_runtime_cancel'],signal)

    def test_delete_library_does_not_change_selected_snapshot(self):
        rule=choice();layer=dict(id='a',color='#ffffff',color_rules={'color':rule})
        before=rules.resolve_object(layer,'video')
        self.library.save([],0)
        self.assertEqual(rules.resolve_object(layer,'video'),before)

    def test_colour_editor_accepts_snapshot_and_manual_change_clears_it(self):
        control=EffectColorEditor('#ffffff,#000000');self.addCleanup(control.deleteLater)
        control.bind_choice({'color_rules':{'caption_effects/color_text':choice()}},'caption_effects/color_text')
        self.assertIsNotNone(control.choice_rule);self.assertIn('套餐',control.palette_button.text())
        control.field.setText('#112233,#445566');self.assertIsNone(control.choice_rule)
        self.assertEqual(control.text(),'#112233,#445566')

    def test_manager_cancel_and_save_and_choice_controls(self):
        manager=PaletteManager(library=self.library);self.addCleanup(manager.deleteLater)
        manager.add();manager.name.setText('我的三色');manager.colors.setText('#ff2244,#22cc88,#4488ff');manager.reject()
        self.assertFalse(self.library.path.exists())
        manager=PaletteManager(library=self.library);self.addCleanup(manager.deleteLater);manager.add();manager.name.setText('我的三色');manager.save()
        self.assertEqual(self.library.load()['palettes'][-1]['name'],'我的三色')
        picker=PaletteChoice('#ffffff',parent=None,library=self.library);self.addCleanup(picker.deleteLater)
        picker.palette.setCurrentIndex(1);picker.confirm();self.assertEqual(picker.rule['colors'],defaults()[0]['colors'])
        picker.mode.setCurrentIndex(picker.mode.findData('fixed'));picker.confirm();self.assertIsNone(picker.rule)

    def test_text_and_caption_fields_commit_rules_through_original_signals(self):
        from app_plugins.builtin.batch_text_video.component_ui import TextBoxProperties
        from app_plugins.builtin.batch_text_video.caption_appearance_ui import CaptionAppearanceEditor
        from app_plugins.builtin.batch_text_video.caption_effect_ui import CaptionEffectEditor
        layer=normalize_layers([new_caption()])[0];panel=TextBoxProperties();self.addCleanup(panel.deleteLater);panel.set_layer(layer);changes=[];panel.changed.connect(changes.append)
        rules.set_rule(panel.layer,'outline_color',choice());panel._palette_changed('outline_color')
        self.assertIn('outline_color',changes[-1]['color_rules'])
        rules.set_rule(panel.layer,'color',choice());panel._palette_changed('color')
        self.assertTrue(panel.controls['color_mode'].isHidden());self.assertTrue(panel.random_color_section.isHidden())
        for editor_type,key,path in ((CaptionAppearanceEditor,'color_high','caption_effects/color_high'),
                                     (CaptionEffectEditor,'holy_glow_color','caption_effects/holy_glow_color')):
            editor=editor_type();self.addCleanup(editor.deleteLater);editor.set_layer(layer);values=[];editor.changed.connect(values.append)
            editor.controls[key].choice_rule=choice();editor.controls[key]._setting=True
            try:editor.controls[key].field.textChanged.emit(editor.controls[key].text())
            finally:editor.controls[key]._setting=False
            self.assertIn(path,values[-1]['color_rules'])

    def test_invalid_group_palette_stays_as_draft_and_library_error_is_shown(self):
        from app_plugins.builtin.batch_text_video.caption_appearance_ui import CaptionAppearanceEditor
        editor=CaptionAppearanceEditor();self.addCleanup(editor.deleteLater)
        layer=normalize_layers([new_caption()])[0];layer['caption_group_colors']=True;editor.set_layer(layer)
        changes=[];editor.changed.connect(changes.append)
        editor.controls['caption_group_palette'].choice_rule=choice(['red']);editor.edit()
        self.assertEqual(changes,[]);self.assertIn('至少',editor.error.text())
        picker=PaletteChoice('#ffffff',library=self.library);self.addCleanup(picker.deleteLater)
        with patch.object(self.library,'load',side_effect=ValueError('invalid library')):picker.manage()
        self.assertIn('invalid library',picker.error.text())

    def test_node_connected_colour_has_priority_over_palette_and_keeps_other_rules(self):
        from app_plugins.builtin.batch_text_video.workflow_colors import apply_color_inputs
        from model.node_workflow.contracts import ColorValue
        layer=dict(color='#ffffff',color_rules={'color':choice(),'outline_color':choice()})
        value=apply_color_inputs(layer,{'color':ColorValue(1,0,0,1)})
        self.assertNotIn('color',value['color_rules']);self.assertIn('outline_color',value['color_rules'])
        self.assertEqual(rules.resolve_object(value,'video')['color'],'#ffff0000')

    def test_job_scope_unchanged_by_export_uuid_status_and_cache(self):
        job=dict(path=str(self.root/'source.mp4'),title='title',body='body',task_dir=str(self.root/'task'))
        self.assertEqual(job_color_scope(job),job_color_scope({**job,'id':'new','status':'failed','output':'other.mp4'}))

    def test_empty_background_rows_differ_but_quick_export_keeps_visible_colour(self):
        from app_plugins.builtin.batch_text_video.quick_export import prepare_current_export
        from app_plugins.builtin.batch_text_video.store import fresh_state
        from app_plugins.builtin.batch_text_video.playback import preview_key
        a=dict(id='a',path='',title='',body='',task_dir='');b={**a,'id':'b'}
        self.assertNotEqual(rules.palette_scope(a),rules.palette_scope(b))
        state=fresh_state();state['settings']['background_pool_id']='none'
        state['settings']['layers']=normalize_layers([dict(id='t',kind='text',source='fixed',text='test',color_rules={'color':choice()})])
        state['jobs']=[a,b]
        self.assertNotEqual(preview_key(a,state),preview_key(b,state))
        preview=dict(id='copy',path='',title='Title',body='Body')
        changed,identifier=prepare_current_export(state,preview,copy_id='copy')
        queued=next(j for j in changed['jobs'] if j['id']==identifier)
        self.assertEqual(rules.palette_scope(preview),rules.palette_scope(queued))

    def test_main_menu_and_palette_change_save_undo_and_preserve_another_colour_rule(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        state=fresh_state();layer=normalize_layers([dict(id='text',kind='text',source='fixed',text='Grace')])[0]
        state['settings']['layers']=[layer];store=Store(self.root/'state');store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'):
            owner=BatchTextVideoDialog(store=store)
            try:
                self.assertTrue(any(a.text()=='我的颜色套餐…' for a in owner.settings_menu.actions()))
                owner.layer_panel.select_layer_id('text');editor=owner.layer_panel.text_properties
                rules.set_rule(editor.layer,'outline_color',choice());editor._palette_changed('outline_color')
                owner.persist();self.assertIn('outline_color',owner.layer_panel.current()['color_rules'])
                self.assertTrue(owner.undo_edit());self.assertNotIn('outline_color',owner.layer_panel.current().get('color_rules',{}))
                self.assertTrue(owner.redo_edit());self.assertIn('outline_color',owner.layer_panel.current()['color_rules'])
                rules.set_rule(owner._global_color_choices,'background_tint_color',choice());owner.schedule_edit();owner.persist()
                self.assertIn('background_tint_color',store.load()['settings']['color_rules'])
                self.assertTrue(owner.undo_edit());self.assertNotIn('background_tint_color',owner.read_settings().get('color_rules',{}))
                self.assertTrue(owner.redo_edit());self.assertIn('background_tint_color',owner.read_settings()['color_rules'])
                shot=os.environ.get('LZX_PALETTE_QA')
                if shot:
                    palette=PaletteChoice('#ffffff',choice(),library=self.library);palette.show();self.app.processEvents()
                    palette.grab().save(str(Path(shot)/'palette-picker.png'));palette.close();palette.deleteLater()
                    manager=PaletteManager(library=self.library);manager.show();self.app.processEvents()
                    manager.grab().save(str(Path(shot)/'palette-manager.png'));manager.close();manager.deleteLater()
            finally:
                owner.edit_timer.stop();owner.close();owner.deleteLater();self.app.processEvents()

    def test_key_colour_and_basic_caption_node_expose_palette_and_pass_validation(self):
        from app_plugins.builtin.batch_text_video.chroma_key_ui import KeyDetails
        from app_plugins.builtin.batch_text_video.visual_media import visual_property_panel
        from app_plugins.builtin.batch_text_video.workflow_caption_ui import CaptionNodeEditor
        from app_plugins.builtin.batch_text_video.workflow_nodes import caption_node
        node=CaptionNodeEditor({},lambda v:None);self.addCleanup(node.deleteLater)
        self.assertTrue(hasattr(node.basic_controls['color'],'palette_choice'))
        self.assertIn('color_rules',{p.key for p in caption_node().parameters})
        details=KeyDetails();self.addCleanup(details.deleteLater)
        layer=normalize_layers([dict(id='pic',kind='image',path=str(self.root/'source.png'),key_mode='custom',key_color='#00ff00')])[0]
        details.set_layer(layer);changes=[];details.changed.connect(changes.append)
        rules.set_rule(details.layer,'key_color',choice());details.palette_changed()
        self.assertIn('key_color',changes[-1]['color_rules'])
        panel=visual_property_panel();self.addCleanup(panel.deleteLater)
        from app_plugins.builtin.batch_text_video.store import fresh_state
        panel.bind(layer,QtCore.QSize(1080,1920),fresh_state(),'percent');patches=[];panel.edited.connect(lambda _,v:patches.append(v))
        panel.visual_properties._key_details_changed(changes[-1])
        self.assertIn('key_color',patches[-1]['color_rules'])

    def test_parameter_overview_palette_is_visible_and_applies_rule_without_changing_hex(self):
        from app_plugins.builtin.batch_text_video.caption_ui import caption_property_panel
        from app_plugins.builtin.batch_text_video.caption_parameter_edit import token_for,apply_value
        from app_plugins.builtin.batch_text_video.caption_parameter_window import CaptionParameterWindow
        from app_plugins.builtin.batch_text_video.store import fresh_state
        layer=normalize_layers([new_caption()])[0];panel=caption_property_panel();self.addCleanup(panel.deleteLater)
        panel.bind(layer,QtCore.QSize(1080,1920),fresh_state(),'percent')
        owner=panel.caption_properties;window=CaptionParameterWindow(owner);self.addCleanup(window.deleteLater)
        # Isolated panel binding normally receives its model refresh from LayerPanel.
        panel.edited.connect(lambda _,patch:panel.bind({**owner.layer,**patch},QtCore.QSize(1080,1920),fresh_state(),'percent'))
        token=token_for(window,('layer','outline_color'));original=owner.layer['outline_color']
        self.assertTrue(apply_value(window,token,original,choice()))
        window.refresh();items=[window.table.topLevelItem(i) for i in range(window.table.topLevelItemCount())]
        selected=next(i for i in items if i.data(0,QtCore.Qt.ItemDataRole.UserRole)==('layer','outline_color'))
        self.assertIn('套餐随机',selected.text(1));self.assertEqual(owner.layer['outline_color'],original)
        apply_value(window,token_for(window,('layer','color')),owner.layer['color'],choice())
        apply_value(window,token_for(window,('layer','color')),owner.layer['color'],None)
        apply_value(window,token_for(window,('layer','color_mode')),'family')
        self.assertNotIn('color',owner.layer['color_rules']);self.assertEqual(owner.layer['color_mode'],'family')

    @unittest.skipUnless(FF,'Explicit FFmpeg required')
    def test_actual_native_preview_and_converted_node_have_same_colour_and_retries(self):
        import numpy as np
        from app_plugins.builtin.batch_text_video import engine
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry,new_effect,prepare_bundle,EffectRuntime
        from app_plugins.builtin.batch_text_video.store import DEFAULTS,fresh_state
        from model.node_workflow.library import WorkflowLibrary
        background=self.root/'background.mp4'
        subprocess.run([FF,'-v','error','-y','-f','lavfi','-i','color=black:s=240x160:r=10:d=0.6','-c:v','libx264',str(background)],check=True,capture_output=True)
        layer=normalize_layers([dict(id='one',kind='text',source='fixed',text='Grace',font_min=50,font_max=50,
            color='#ffffff',outline_width=0,shadow_enabled=False,width=95,height=90,color_rules={'color':choice()})])[0]
        registry=fallback_registry();graph,_=component_graph(layer,DEFAULTS,registry);library=WorkflowLibrary(self.root/'workflows')
        stored=library.save(graph,'colour match',registry);effect=new_effect();effect['workflow_id']=stored['workflow_id']
        services=SimpleNamespace(available_nodes=lambda:registry,workflow_library=library);outputs=[]
        for mode in ('native','playback','node','retry'):
            state=fresh_state();state['settings'].update(aspect='source',darkness=0,use_voice=False,music_enabled=False,
                ffmpeg_path=FF,output_dir=str(self.root/'out'),layers=[effect if mode=='node' else layer])
            job=dict(id='job',path=str(background),name=mode,title='',body='',task_dir='');state['jobs']=[job];runtime=None
            if mode=='node':
                bundle,registry,_=prepare_bundle(state,['job'],services);runtime=EffectRuntime(bundle,registry)
            output,_=engine.render_static_text(job,state,self.root/'render-data',preview=mode=='playback',node_effects=runtime)
            data=subprocess.run([FF,'-v','error','-i',str(output),'-f','rawvideo','-pix_fmt','rgb24','pipe:1'],check=True,capture_output=True).stdout
            outputs.append(np.frombuffer(data,np.uint8).reshape(-1,160,240,3))
        for frames in outputs[1:]:self.assertLess(np.abs(frames.astype(float)-outputs[0]).mean(),2.)

if __name__=='__main__':unittest.main()
