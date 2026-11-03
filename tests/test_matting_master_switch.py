"""The unchecked master must preserve original alpha and never invoke AI."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import matting_assets as assets, matting_live as live
from app_plugins.builtin.batch_text_video import matting_switch as switch, matting_render, engine
from app_plugins.builtin.batch_text_video.visual_media import VisualProperties, normalize_visual, keyed_image, add_plane
from app_plugins.builtin.batch_text_video.layers import normalize_layers, compose_layers
from app_plugins.builtin.batch_text_video.components import resolve_sources

FF=os.environ.get('NODE_FFMPEG','')


class MasterSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.path=self.root/'original.png'
        self.image=QtGui.QImage(64,48,QtGui.QImage.Format.Format_RGBA8888);self.image.fill(QtGui.QColor(0,255,0,128))
        self.image.setPixelColor(32,24,QtGui.QColor(200,100,80,0));self.assertTrue(self.image.save(str(self.path)))

    def layer(self,**updates):
        layer=dict(id='person',kind='image',path=str(self.path),width=100,height=100,x=50,y=50,opacity=100,
            matting_enabled=False,key_method='ai',key_mode='green',key_despill=True,key_cleanup=True,
            key_matte_grow=-1.,key_alpha_low=20.,key_alpha_high=75.,use_saved_matte=True)
        layer.update(updates);return normalize_layers([layer])[0]

    def test_default_legacy_enabled_and_boolean_validation(self):
        layer={};normalize_visual(layer);self.assertIs(layer['matting_enabled'],True)
        for value in (0,1,'false',None):
            with self.subTest(value=value),self.assertRaises(ValueError):normalize_visual(dict(matting_enabled=value))

    def test_checkbox_disables_all_matte_controls_preserves_options_and_recovers(self):
        layer=self.layer(matting_enabled=True);panel=VisualProperties();self.addCleanup(panel.deleteLater);panel.set_layer(layer)
        changes=[];panel.changed.connect(lambda v:changes.append(copy.deepcopy(v)))
        master=panel.controls['matting_enabled'];self.assertTrue(master.isChecked())
        master.click();self.assertEqual(len(changes),1);self.assertFalse(changes[-1]['matting_enabled'])
        for key in layer:
            if key.startswith('key_') or key=='use_saved_matte':self.assertEqual(changes[-1][key],layer[key],key)
        self.assertTrue(master.isEnabled());self.assertFalse(panel.controls['use_saved_matte'].isEnabled())
        self.assertFalse(panel.matting_button.isEnabled());self.assertFalse(panel.matte_levels.isEnabled())
        for key,control in panel.controls.items():
            if key.startswith('key_'):self.assertFalse(control.isEnabled(),key)
        self.assertTrue(panel.controls['media_loop'].isEnabled());self.assertTrue(panel.controls['blend_mode'].isEnabled())
        self.assertIn('直接使用原素材',panel.matting_status.text())
        self.assertIn('不关闭AI',panel.controls['key_mode'].itemText(0))
        master.click();self.assertTrue(panel.controls['use_saved_matte'].isEnabled());self.assertTrue(panel.matting_button.isEnabled())
        panel.controls['use_saved_matte'].setChecked(False);self.assertTrue(panel.controls['key_method'].isEnabled())
        self.assertEqual(panel.controls['key_matte_grow'].value(),-1.)
        shot=os.environ.get('LZX_MASTER_SWITCH_QA')
        if shot:
            master.setChecked(False);panel.sections.sections['visual'].set_expanded(True)
            panel.resize(650,2200);panel.show();self.app.processEvents();panel.grab().save(shot);panel.close()

    def test_disabled_saved_and_inline_ai_skip_catalogue_hash_and_model(self):
        catalogue=assets.Catalogue(self.root/'catalogue');layer=self.layer();before=copy.deepcopy(layer)
        for saved in (True,False):
            layer['use_saved_matte']=saved
            with (patch.object(catalogue,'lookup',side_effect=AssertionError('no saved matte')),
                  patch.object(assets,'fingerprint',side_effect=AssertionError('no AI hash'))):
                result=assets.apply_saved(layer,catalogue);snapshot=assets.snapshot_layers([layer],catalogue)
            self.assertEqual(result['path'],str(self.path));self.assertEqual(result['key_mode'],'none')
            self.assertEqual(result['key_method'],'normal');self.assertFalse(result['key_despill'])
            self.assertNotIn('_matting_binding',result);self.assertNotIn('_matting_snapshot',snapshot[0])
            self.assertEqual(snapshot[0]['key_method'],'ai');self.assertFalse(snapshot[0]['matting_enabled'])
        layer['use_saved_matte']=before['use_saved_matte'];self.assertEqual(layer,before)

    def test_stale_saved_binding_restores_original_but_does_not_retarget_new_pool_member(self):
        layer=self.layer(path=str(self.root/'saved.png'))
        layer['_matting_binding']=dict(source={'path':str(self.path)},result={'path':layer['path']},mode='dynamic')
        layer['_matting_snapshot']={'bad':{'error':'corrupt'}};layer['_matting_post_options']={'key_matte_grow':-2}
        self.assertFalse(live.dynamic(layer));result=switch.original(layer)
        self.assertEqual(result['path'],str(self.path));self.assertNotIn('_matting_binding',result)
        self.assertNotIn('_matting_snapshot',result);self.assertIn('_matting_binding',layer)
        other=str(self.root/'new-pool.png');layer['path']=other
        self.assertEqual(switch.original(layer)['path'],other)

    def test_design_original_alpha_and_green_are_preserved_and_mask_not_opened(self):
        layer=self.layer(key_method='enhanced',key_mask_path=str(self.root/'missing-mask.png'))
        from app_plugins.builtin.batch_text_video.chroma_key import active_mask
        self.assertEqual(active_mask(layer),'');self.assertEqual(keyed_image(self.image,layer),self.image)
        image=compose_layers({},[layer],64,48,0)
        self.assertGreater(image.pixelColor(10,10).green(),240);self.assertAlmostEqual(image.pixelColor(10,10).alpha(),128,delta=1)
        self.assertEqual(image.pixelColor(32,24).alpha(),0)

    def test_pool_snapshot_and_resolve_both_members_skip_stale_matte_errors(self):
        second=self.root/'second.png';self.assertTrue(self.image.save(str(second)))
        layer=self.layer(source='pool',sequence_items=[dict(id='a',path=str(self.path)),dict(id='b',path=str(second))])
        layer['_matting_snapshot']={'corrupt':{'error':'must not read'}}
        with patch.object(assets.Catalogue,'lookup',side_effect=AssertionError('disabled pool lookup')):
            frozen=assets.snapshot_layers([layer])
            for index,path in enumerate((self.path,second)):
                frozen[0]['sequence_cursor']=index;result=resolve_sources(frozen)[0][0]
                self.assertEqual(result['path'],str(path));self.assertNotIn('_matting_binding',result)

    @unittest.skipUnless(FF,'Explicit FFmpeg required')
    def test_transparent_mov_direct_native_path_and_loop_dissolve_never_run_ai(self):
        path=self.root/'transparent.mov'
        pixels=np.zeros((10,48,64,4),np.uint8);pixels[:,:,:,:3]=[0,255,0];pixels[:,8:40,12:52,3]=128
        result=subprocess.run([FF,'-v','error','-y','-f','rawvideo','-pix_fmt','rgba','-s','64x48','-r','10','-i','pipe:0',
            '-c:v','qtrle','-pix_fmt','argb',str(path)],input=pixels.tobytes(),capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        for mode in ('none','fade'):
            layer=self.layer(path=str(path),media_loop_transition=mode,media_loop_fade=.2)
            args=[FF,'-y','-v','error','-filter_complex_threads','1','-f','lavfi','-i','color=black:s=64x48:r=10:d=2']
            filters=[];target=self.root/(mode+'.mkv')
            with patch.object(matting_render,'Predictor',side_effect=AssertionError('Disabled AI invoked')):
                add_plane(args,filters,layer,1,'0:v','person',64,48,2,10,ffmpeg=FF)
                self.assertNotIn('colorkey',';'.join(filters));self.assertNotIn('geq=',';'.join(filters))
                args+=['-filter_complex',';'.join(filters),'-map','[person]','-c:v','ffv1',str(target)]
                engine._run(args,timeout=20)
            data=subprocess.run([FF,'-v','error','-i',str(target),'-f','rawvideo','-pix_fmt','rgba','pipe:1'],capture_output=True,check=True).stdout
            frames=np.frombuffer(data,np.uint8).reshape(-1,48,64,4)
            self.assertEqual(len(frames),20);self.assertGreater(frames[:,24,32,1].min(),110)
            self.assertLess(frames[:,24,32,1].max(),145);self.assertLess(frames[:,0,0,:3].max(),3)

    def test_master_persists_in_node_style_and_normal_inspector_patch(self):
        from app_plugins.builtin.batch_text_video.visual_media import visual_property_panel
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry
        from app_plugins.builtin.batch_text_video.store import DEFAULTS,Store,fresh_state
        layer=self.layer(matting_enabled=True);panel=visual_property_panel();self.addCleanup(panel.deleteLater)
        panel.bind(layer,QtCore.QSize(1080,1920),fresh_state(),'percent');patches=[];panel.edited.connect(lambda i,v:patches.append((i,v)))
        panel.visual_properties.controls['matting_enabled'].click()
        self.assertEqual(patches[-1][0],layer['id']);self.assertFalse(patches[-1][1]['matting_enabled'])
        layer.update(patches[-1][1]);graph,_=component_graph(layer,DEFAULTS,fallback_registry())
        self.assertIn('"matting_enabled": false',__import__('json').dumps(graph))
        state=fresh_state();state['settings']['layers']=[layer];store=Store(self.root/'store');store.save(state,0)
        self.assertFalse(next(v for v in store.load()['settings']['layers'] if v['id']=='person')['matting_enabled'])

    @unittest.skipUnless(FF,'Explicit FFmpeg required')
    def test_actual_export_playback_and_converted_node_keep_native_alpha_without_ai(self):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry,new_effect,prepare_bundle,EffectRuntime
        from app_plugins.builtin.batch_text_video.store import DEFAULTS,fresh_state
        from model.node_workflow.library import WorkflowLibrary
        background=self.root/'background.mp4'
        subprocess.run([FF,'-y','-v','error','-f','lavfi','-i','color=blue:s=240x160:r=10:d=0.6',
            '-c:v','libx264',str(background)],capture_output=True,check=True,timeout=15)
        layer=self.layer();registry=fallback_registry();graph,_=component_graph(layer,DEFAULTS,registry)
        library=WorkflowLibrary(self.root/'workflows');record=library.save(graph,'disabled matte',registry)
        effect=new_effect();effect['workflow_id']=record['workflow_id']
        services=SimpleNamespace(available_nodes=lambda:registry,workflow_library=library)
        outputs=[]
        with (patch.object(matting_render,'Predictor',side_effect=AssertionError('Disabled AI invoked')),
              patch.object(assets.Catalogue,'lookup',side_effect=AssertionError('Disabled workbench invoked'))):
            for mode in ('native','playback','node'):
                state=fresh_state();state['settings'].update(aspect='source',darkness=0,music_enabled=False,use_voice=False,
                    ffmpeg_path=FF,output_dir=str(self.root/'out'),layers=[effect if mode=='node' else layer])
                job=dict(id='job',path=str(background),name=mode,title='',body='',task_dir='');state['jobs']=[job]
                runtime=None
                if mode=='node':
                    bundle,registry,_=prepare_bundle(state,['job'],services);runtime=EffectRuntime(bundle,registry)
                output,_=engine.render_static_text(job,state,self.root/'render-data',preview=mode=='playback',node_effects=runtime)
                data=subprocess.run([FF,'-v','error','-i',str(output),'-f','rawvideo','-pix_fmt','rgba','pipe:1'],capture_output=True,check=True).stdout
                outputs.append(np.frombuffer(data,np.uint8).reshape(-1,160,240,4))
        for frames in outputs:
            self.assertEqual(len(frames),6);self.assertGreater(frames[0,20,50,1],100);self.assertGreater(frames[0,20,50,2],100)
        for frames in outputs[1:]:self.assertLess(np.abs(frames.astype(float)-outputs[0]).mean(),3)

    def test_main_window_master_change_saves_and_undo_restores_all_parameters(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        layer=self.layer(matting_enabled=True,use_saved_matte=False);state=fresh_state();state['settings']['layers']=[layer]
        store=Store(self.root/'undo-store');store.save(state,0)
        with patch.object(BatchTextVideoDialog,'request_background'):
            dialog=BatchTextVideoDialog(store=store)
            try:
                dialog.layer_panel.select_layer_id('person');dialog.layer_panel.visual_properties.controls['matting_enabled'].click()
                self.assertFalse(dialog.layer_panel.current()['matting_enabled']);self.assertTrue(dialog.persist())
                self.assertFalse(next(v for v in store.load()['settings']['layers'] if v['id']=='person')['matting_enabled'])
                self.assertTrue(dialog.undo_edit());self.assertTrue(dialog.layer_panel.current()['matting_enabled'])
                self.assertTrue(dialog.redo_edit());self.assertFalse(dialog.layer_panel.current()['matting_enabled'])
                for key,value in layer.items():
                    if key.startswith('key_'):self.assertEqual(dialog.layer_panel.current()[key],value,key)
            finally:dialog.edit_timer.stop();dialog.close();dialog.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()
