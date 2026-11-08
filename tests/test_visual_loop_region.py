"""Real long-GOP selection, native alpha/key paths, bounded preview ownership."""
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
from app_plugins.builtin.batch_text_video import engine, visual_loop_region as regions
from app_plugins.builtin.batch_text_video.visual_media import add_plane, normalize_visual, VisualProperties
from app_plugins.builtin.batch_text_video.layers import normalize_layers
from app_plugins.builtin.batch_text_video.matting_live import Inputs
from app_plugins.builtin.batch_text_video import visual_loop as loops
from app_plugins.builtin.batch_text_video.visual_loop_region_ui import LoopRegionDialog

FF = os.environ.get('NODE_FFMPEG', '')


class LoopRegionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_ff(self, args, data=None):
        result = subprocess.run([FF, '-v', 'error', *args], input=data, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8','replace'))
        return result.stdout

    def media(self, rate='10', alpha=False, keyed=False):
        path = self.root/("人物's 测试.mov" if alpha else "人物's 测试.mp4")
        pixels = np.zeros((30,24,32,4), np.uint8); pixels[:,:,:,3]=255
        for i in range(30): pixels[i,:,:,:3] = [i*7, i*3, 220-i*4]
        if alpha: pixels[:,:,:,3]=np.arange(30)[:,None,None]*8
        if keyed: pixels[:,0:5,:, :3] = [0,255,0]
        codec = ['-c:v','qtrle','-pix_fmt','argb'] if alpha else ['-c:v','libx264','-crf','8','-g','100','-bf','3','-pix_fmt','yuv420p']
        self.run_ff(['-y','-f','rawvideo','-pix_fmt','rgba','-s','32x24','-r',rate,'-i','pipe:0',*codec,str(path)],pixels.tobytes())
        return path

    def rule(self,path,start=1.,end=2.):
        _,fp=engine.resolve_tools(FF); media=engine.probe(path,fp)
        return dict(path=str(path),start=start,end=end,duration=media['duration'],**regions.stamp(path))

    def layer(self,path,rule=None,**values):
        value=dict(id='person',kind='image',source='fixed',path=str(path),width=100,height=100,
                   media_loop_regions=[rule] if rule else [])
        value.update(values); return normalize_layers([value])[0]

    def pixels(self,path):
        return np.frombuffer(self.run_ff(['-i',str(path),'-f','rawvideo','-pix_fmt','rgba','pipe:1']),np.uint8).reshape(-1,24,32,4)

    def render(self,layer,duration=5.,fps=10):
        args=[FF,'-y','-v','error','-filter_complex_threads','1','-f','lavfi','-i',f'color=black:s=32x24:r={fps}:d={duration}']
        filters=[]
        add_plane(args,filters,layer,1,'0:v','person',32,24,duration,fps,ffmpeg=FF)
        args+=['-filter_complex',';'.join(filters),'-map','[person]','-an','-c:v','ffv1',str(self.root/'output.mkv')]
        engine._run(args,timeout=60)
        return self.pixels(self.root/'output.mkv'),args,filters

    def wait_until(self, test, timeout=10):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            self.app.processEvents()
            if test(): return
            time.sleep(.01)
        self.fail('Qt preview timeout')

    def close_dialog(self,dialog):
        dialog.reject(); self.wait_until(lambda: dialog.worker is None and dialog.loader is None)
        dialog.deleteLater(); self.app.processEvents()

    def test_normalization_is_pure_and_defaults_not_shared(self):
        a,b={},{}
        with patch.object(Path,'stat',side_effect=AssertionError('must not stat')):
            normalize_visual(a); normalize_visual(b)
            rule=dict(path='X:/missing.mov',start=1.,end=2.,duration=3.,size=0,mtime_ns=0)
            normalize_visual({regions.KEY:[rule]})
        a[regions.KEY].append({}); self.assertEqual(b[regions.KEY],[])

    def test_invalid_rules_rejected(self):
        good=dict(path='X:/source.mov',start=1.,end=2.,duration=3.,size=20,mtime_ns=5)
        for update in ({'start':2.},{'start':True},{'end':4.},{'start':float('nan')},{'size':-1},{'path':'x\n.mov'}):
            with self.subTest(update=update),self.assertRaises(ValueError): regions.normalize([{**good,**update}])
        with self.assertRaises(ValueError): regions.normalize([good,good])
        with self.assertRaises(ValueError): regions.normalize({})

    def test_pool_rules_select_original_binding_and_do_not_leak_to_other_sources(self):
        r=dict(path='C:/person.mov',start=1.,end=2.,duration=3.,size=0,mtime_ns=0)
        layer={regions.KEY:[r],'path':'C:/other.mov'}
        self.assertIsNone(regions.selected(layer))
        layer['_matting_binding']={'source':{'path':'c:/PERSON.mov'}}
        self.assertEqual(regions.selected(layer),r)
        self.assertIsNone(regions.selected({**layer,'media_loop':False}))
        self.assertEqual(regions.sources(dict(source='pool',sequence_items=[{'path':'C:/person.mov'},{'path':'c:/PERSON.mov'},{'path':'C:/still.png'}])),['C:/person.mov'])

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_native_long_gop_first_zero_to_b_then_a_to_b_exact_and_no_python_feed(self):
        path=self.media(); rule=self.rule(path); expected=self.pixels(path)
        output,args,_=self.render(self.layer(path,rule))
        self.assertEqual(len(output),50)
        wanted=np.concatenate([expected[:20],expected[10:20],expected[10:20],expected[10:20]])
        np.testing.assert_allclose(output[:,:,:,:3],wanted[:,:,:,:3],atol=3)
        spec=next(a for a in args if isinstance(a,regions.PlaylistInput))
        with Inputs(args) as expanded:
            script=next(Path(a) for a in expanded.args if str(a).endswith('.ffconcat'))
            self.assertTrue(script.is_file()); self.assertEqual(expanded.feeds,[])
        self.assertFalse(script.exists()); self.assertEqual(str(spec),str(path))

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_fractional_rate_exact_frames(self):
        rate=30000/1001; path=self.media('30000/1001'); rule=self.rule(path,10/rate,20/rate)
        spec=regions.PlaylistInput(str(path),rule,50/rate)
        args=[FF,'-v','error','-i',spec,'-vf','select=concatdec_select,setpts=PTS-STARTPTS',
              '-fps_mode','passthrough','-frames:v','50','-f','rawvideo','-pix_fmt','rgba','pipe:1']
        with Inputs(args) as inputs: data=self.run_ff(inputs.args[3:])
        actual=np.frombuffer(data,np.uint8).reshape(-1,24,32,4)
        base=self.pixels(path); expected=np.concatenate([base[:20],base[10:20],base[10:20],base[10:20]])
        np.testing.assert_array_equal(actual,expected)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_native_alpha_is_selected_with_rgb_and_no_giant_cache(self):
        path=self.media(alpha=True); rule=self.rule(path)
        spec=regions.PlaylistInput(str(path),rule,5)
        args=[FF,'-v','error','-i',spec,'-vf','select=concatdec_select,setpts=PTS-STARTPTS',
              '-frames:v','50','-f','rawvideo','-pix_fmt','rgba','pipe:1']
        with Inputs(args) as inputs: data=self.run_ff(inputs.args[3:])
        actual=np.frombuffer(data,np.uint8).reshape(-1,24,32,4)
        base=self.pixels(path); expected=np.concatenate([base[:20],base[10:20],base[10:20],base[10:20]])
        np.testing.assert_array_equal(actual,expected)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_native_green_key_respects_interval_and_intro_once(self):
        path=self.media(keyed=True); rule=self.rule(path)
        output,args,filters=self.render(self.layer(path,rule,key_mode='green',key_similarity=.2,key_softness=.1))
        self.assertTrue(any(isinstance(x,regions.PlaylistInput) for x in args))
        self.assertLess(output[:,2,16,:3].max(),4)
        np.testing.assert_allclose(output[20:30,12,16,:3],output[10:20,12,16,:3],atol=2)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_disabled_or_other_pool_member_keeps_legacy_whole_loop(self):
        path=self.media(); rule=self.rule(path)
        layer=self.layer(path,rule,media_loop=False)
        output,args,_=self.render(layer)
        self.assertFalse(any(isinstance(x,regions.PlaylistInput) for x in args))
        self.assertLess(output[35:,12,16,:3].max(),3)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_changed_source_is_rejected_and_short_interval_bounded(self):
        path=self.media(); rule=self.rule(path); rule['size']+=1
        with self.assertRaisesRegex(ValueError,'改变'): self.render(self.layer(path,rule))
        rule=self.rule(path,1.,1.001)
        with self.assertRaisesRegex(ValueError,'一帧'): self.render(self.layer(path,rule))
        with self.assertRaisesRegex(ValueError,'过短'): regions.PlaylistInput(str(path),self.rule(path,1.,1.1),86400)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_dissolve_head_is_a_never_preparation_zero(self):
        path=self.media(); layer=self.layer(path,self.rule(path),media_loop_transition='fade',media_loop_fade=.2)
        spec=loops.LoopInput(layer,4.,10,FF,(32,24))
        self.assertEqual(spec.overlap,2)
        from app_plugins.builtin.batch_text_video.matting_live import Feed as RealFeed
        feed=RealFeed(spec)
        try: actual=np.frombuffer(b''.join(loops.frames(feed)),np.uint8).reshape(-1,24,32,4)
        finally: feed.close()
        base=self.pixels(path)
        self.assertEqual(len(actual),40)
        for j in range(2):
            expected=np.frombuffer(loops.blend(base[18+j].tobytes(),base[10+j].tobytes(),(j+1)/3,32,24),np.uint8).reshape(24,32,4)
            np.testing.assert_array_equal(actual[18+j],expected)
        np.testing.assert_array_equal(actual[20],base[12])

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_fractional_single_frame_loop_and_short_output(self):
        rate=30000/1001; path=self.media('30000/1001'); base=self.pixels(path)
        for a,b,count in ((1/rate,2/rate,15),(10/rate,20/rate,5)):
            spec=regions.PlaylistInput(str(path),self.rule(path,a,b),count/rate)
            args=[FF,'-v','error','-i',spec,'-vf','select=concatdec_select,setpts=PTS-STARTPTS',
                  '-fps_mode','passthrough','-frames:v',str(count),'-f','rawvideo','-pix_fmt','rgba','pipe:1']
            with Inputs(args) as inputs: data=self.run_ff(inputs.args[3:])
            actual=np.frombuffer(data,np.uint8).reshape(-1,24,32,4)
            indices=list(range(round(b*rate)))+[round(a*rate)]*count if count==15 else list(range(count))
            np.testing.assert_array_equal(actual,base[indices[:count]])

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_node_and_native_render_match_using_same_region(self):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        from app_plugins.builtin.batch_text_video.node_effects import fallback_registry,new_effect,prepare_bundle,EffectRuntime
        from app_plugins.builtin.batch_text_video.store import DEFAULTS,fresh_state
        from model.node_workflow.library import WorkflowLibrary
        path=self.media(); layer=self.layer(path,self.rule(path),use_saved_matte=False)
        background=self.root/'background.mp4'
        self.run_ff(['-y','-f','lavfi','-i','color=black:s=240x160:r=10:d=5','-c:v','libx264',str(background)])
        registry=fallback_registry(); graph,_=component_graph(layer,DEFAULTS,registry)
        library=WorkflowLibrary(self.root/'workflows'); saved=library.save(graph,'loop',registry)
        effect=new_effect(); effect['workflow_id']=saved['workflow_id']
        services=SimpleNamespace(available_nodes=lambda:registry,workflow_library=library)
        outputs=[]
        for mode in ('native','playback','node'):
            state=fresh_state(); state['settings'].update(aspect='source',darkness=0,music_enabled=False,use_voice=False,
                ffmpeg_path=FF,output_dir=str(self.root/'out'),layers=[effect if mode=='node' else layer])
            job=dict(id='job',path=str(background),name=mode,title='',body='',task_dir='');state['jobs']=[job]
            runtime=None
            if mode=='node':
                bundle,registry,_=prepare_bundle(state,['job'],services);runtime=EffectRuntime(bundle,registry)
            output,_=engine.render_static_text(job,state,self.root/'render-data',preview=mode=='playback',node_effects=runtime)
            data=self.run_ff(['-i',str(output),'-f','rawvideo','-pix_fmt','rgba','pipe:1'])
            outputs.append(np.frombuffer(data,np.uint8).reshape(-1,160,240,4))
        for result in outputs[1:]:
            self.assertEqual(len(result),50)
            self.assertLess(np.abs(result.astype(float)-outputs[0]).mean(),2.)
            np.testing.assert_allclose(result[:,80,120],outputs[0][:,80,120],atol=4)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_dynamic_ai_region_without_dissolve_repeats_selected_frames(self):
        from app_plugins.builtin.batch_text_video import matting_render,matting_assets
        from app_plugins.builtin.batch_text_video.matting_settings import normalize
        from app_plugins.builtin.batch_text_video.matting_live import Feed
        from test_matting_video_preview import VideoPredictor
        path=self.media();layer=self.layer(path,self.rule(path),key_mode='none')
        layer['_matting_binding']=dict(mode='dynamic',source=matting_assets.fingerprint(path),parameters=normalize())
        with patch.object(matting_render,'Predictor',VideoPredictor):
            spec=loops.LoopInput(layer,4.,10,FF,(32,24)); self.assertEqual(spec.overlap,0)
            feed=Feed(spec)
            try: actual=np.frombuffer(b''.join(loops.frames(feed)),np.uint8).reshape(-1,24,32,4)
            finally: feed.close()
        np.testing.assert_array_equal(actual[20:30],actual[10:20])
        np.testing.assert_array_equal(actual[30:40],actual[10:20])

    @unittest.skipUnless(FF and os.environ.get('LZX_DNX_ALPHA_FIXTURE'),'Explicit read-only DNxHR Alpha fixture required')
    def test_native_dnx_alpha_region_uses_matching_rgb_and_alpha_times(self):
        from app_plugins.builtin.batch_text_video import dnx_alpha
        from app_plugins.builtin.batch_text_video.matting_live import Feed
        from app_plugins.builtin.batch_text_video.matting_video import describe
        source=self.root/'native-alpha.mov'
        self.run_ff(['-y','-i',os.environ['LZX_DNX_ALPHA_FIXTURE'],'-map','0:v:0','-c:v','copy','-frames:v','12',str(source)])
        _,fp=engine.resolve_tools(FF); info=describe(engine.probe(source,fp)); rate=info['fps']
        layer=self.layer(source,self.rule(source,3/rate,8/rate),media_loop_transition='none')
        with patch.object(dnx_alpha,'cache_root',return_value=self.root/'native-cache'):
            spec=loops.LoopInput(layer,18/rate,rate,FF,(32,24));feed=Feed(spec)
            try: actual=np.frombuffer(b''.join(loops.frames(feed)),np.uint8).reshape(-1,spec.height,spec.width,4)
            finally:feed.close()
        np.testing.assert_array_equal(actual[8:13],actual[3:8])
        np.testing.assert_array_equal(actual[13:18],actual[3:8])
        self.assertGreater(actual[4,:,:,3].max(),200);self.assertLess(actual[4,:,:,3].min(),10)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_native_nonzero_stream_origin_and_frame_times(self):
        original=self.media();path=self.root/'offset.mp4'
        self.run_ff(['-y','-i',str(original),'-c:v','copy','-output_ts_offset','5',str(path)])
        actual,_,_=self.render(self.layer(path,self.rule(path)))
        base=self.pixels(original)
        expected=np.concatenate([base[:20],base[10:20],base[10:20],base[10:20]])
        np.testing.assert_allclose(actual[:,:,:,:3],expected[:,:,:,:3],atol=3)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_dialog_boundary_previews_loop_and_save_while_worker_running(self):
        path=self.media(); layer=self.layer(path)
        dialog=LoopRegionDialog(layer); dialog.show(); self.addCleanup(lambda:self.close_dialog(dialog))
        self.wait_until(lambda: dialog.info is not None and not dialog.start_view.image.isNull() and dialog.worker is None)
        self.assertFalse(dialog.enabled.isChecked())
        dialog.enabled.setChecked(True); dialog.start.setValue(1); dialog.end.setValue(2)
        self.wait_until(lambda: dialog.worker is None and not dialog.timer.isActive() and not dialog.pending)
        start=dialog.start_view.image.pixelColor(16,12)
        self.assertGreater(start.red(),50)
        dialog.play_segment(True)
        self.wait_until(lambda: dialog.position>=1.8)
        self.wait_until(lambda: 1. <= dialog.position <1.4)
        screenshot=os.environ.get('LZX_LOOP_REGION_QA')
        if screenshot: dialog.grab().save(screenshot)
        dialog.save()
        self.wait_until(lambda: dialog.worker is None and dialog.loader is None and not dialog.isVisible())
        self.assertEqual(dialog.result(),QtWidgets.QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.rules[0]['start'],1); self.assertEqual(dialog.rules[0]['end'],2)
        self.assertEqual(layer[regions.KEY],[])

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_dialog_pool_switch_saves_distinct_drafts_cancel_no_mutation(self):
        path=self.media(); other=self.root/'other.mp4'; other.write_bytes(path.read_bytes())
        layer=self.layer(path,source='pool',sequence_items=[{'id':'a','path':str(path)},{'id':'b','path':str(other)}])
        before=copy.deepcopy(layer); dialog=LoopRegionDialog(layer); self.addCleanup(lambda:self.close_dialog(dialog))
        self.wait_until(lambda: dialog.info is not None)
        dialog.enabled.setChecked(True); dialog.start.setValue(1); dialog.end.setValue(2)
        dialog.source.setCurrentIndex(1); self.wait_until(lambda: dialog.info is not None and dialog.path==str(other))
        self.assertFalse(dialog.enabled.isChecked()); self.assertEqual(dialog.start.value(),0)
        dialog.enabled.setChecked(True); dialog.start.setValue(.5); dialog.end.setValue(1.5)
        self.assertTrue(dialog.capture()); self.assertEqual(len(dialog.rules),2)
        self.assertEqual(layer,before)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_properties_roundtrip_and_loop_switch(self):
        path=self.media(); layer=self.layer(path,self.rule(path))
        panel=VisualProperties(); self.addCleanup(panel.deleteLater); panel.set_layer(layer)
        self.assertTrue(panel.loop_region_button.isEnabled()); self.assertIn('1个',panel.loop_region_note.text())
        panel.controls['media_loop'].setChecked(False); self.assertFalse(panel.loop_region_button.isEnabled())
        self.assertEqual(panel.layer[regions.KEY],layer[regions.KEY])
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        state=fresh_state(); state['settings']['layers']=[layer]
        store=Store(self.root/'store'); store.save(state,0)
        self.assertEqual(store.load()['settings']['layers'][0][regions.KEY],layer[regions.KEY])

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_original_preview_bounds_memory_without_hashing_whole_video(self):
        from app_plugins.builtin.batch_text_video.matting_video import MattingVideoWorker,describe
        from app_plugins.builtin.batch_text_video import matting_assets
        path=self.root/'portrait.mp4'
        self.run_ff(['-y','-f','lavfi','-i','color=red:s=1080x1920:r=5:d=0.2','-c:v','libx264',str(path)])
        _,fp=engine.resolve_tools(FF);info=describe(engine.probe(path,fp))
        worker=MattingVideoWorker(dict(info=info,path=str(path),values={},position=0.,play=False,generation=1,
            original_only=True,source_stamp=regions.stamp(path),ffmpeg=FF))
        result=[];errors=[]
        worker.frameReady.connect(lambda value:(result.append(value),worker.acknowledge()),QtCore.Qt.ConnectionType.DirectConnection)
        worker.failed.connect(errors.append,QtCore.Qt.ConnectionType.DirectConnection)
        with patch.object(matting_assets,'fingerprint',side_effect=AssertionError('must not hash preview')):worker.run()
        self.assertEqual(errors,[]);self.assertEqual(len(result),1)
        self.assertLessEqual(result[0]['original'].width(),960);self.assertLessEqual(result[0]['original'].height(),540)

    @unittest.skipUnless(FF,'FFmpeg required')
    def test_real_inspector_save_undo_redo_and_cancel_do_not_change_sources(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        from app_plugins.builtin.batch_text_video.store import Store,fresh_state
        path=self.media();layer=self.layer(path);rule=self.rule(path)
        state=fresh_state();state['settings']['layers']=[layer];store=Store(self.root/'edit-store');store.save(state,0)
        class Draft(QtWidgets.QDialog):
            def __init__(self,*args):super().__init__();self.rules=[rule]
            def exec(self):return QtWidgets.QDialog.DialogCode.Accepted
        with patch.object(BatchTextVideoDialog,'request_background'):
            dialog=BatchTextVideoDialog(store=store)
            try:
                dialog.layer_panel.select_layer_id('person')
                with patch('app_plugins.builtin.batch_text_video.visual_loop_region_ui.LoopRegionDialog',Draft):
                    dialog.layer_panel.visual_properties._edit_loop_region()
                self.assertEqual(dialog.layer_panel.current()[regions.KEY],[rule])
                self.assertTrue(dialog.persist())
                self.assertEqual(next(v for v in store.load()['settings']['layers'] if v['id']=='person')[regions.KEY],[rule])
                self.assertTrue(dialog.undo_edit());self.assertEqual(dialog.layer_panel.current()[regions.KEY],[])
                self.assertTrue(dialog.redo_edit());self.assertEqual(dialog.layer_panel.current()[regions.KEY],[rule])
                self.assertEqual(dialog.layer_panel.current()['path'],str(path))
                Draft.exec=lambda self:QtWidgets.QDialog.DialogCode.Rejected
                before=copy.deepcopy(dialog.layer_panel.current())
                with patch('app_plugins.builtin.batch_text_video.visual_loop_region_ui.LoopRegionDialog',Draft):
                    dialog.layer_panel.visual_properties._edit_loop_region()
                self.assertEqual(dialog.layer_panel.current(),before)
            finally:
                dialog.edit_timer.stop();dialog.close()
                # Closing is asynchronous. Drain decoders before tempfile
                # cleanup so Windows never deletes a source still being read.
                self.wait_until(lambda: not dialog.is_busy(), timeout=15)
                dialog.deleteLater();self.app.processEvents()


if __name__=='__main__': unittest.main()
