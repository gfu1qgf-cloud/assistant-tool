import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.batch_text_video import dnx_alpha as dnx
from app_plugins.builtin.batch_text_video.matting_live import Inputs


class AlphaCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root/'source.mov'; self.source.write_bytes(b'source')
        self.info = dict(stamp=dnx.signature(self.source), header_sha256='abc', width=2, height=2, rate='30')
        self.root_patch = patch.object(dnx, 'cache_root', return_value=self.root/'cache'); self.root_patch.start(); self.addCleanup(self.root_patch.stop)

    def build(self, path, info, cancel, report):
        path.write_bytes(b'mask'); path.with_suffix('.json').write_text(json.dumps(dict(key=dnx.cache_key(info), bytes=4, frames=3)))

    def test_non_mov_has_no_probe_or_file_access(self):
        with patch.object(dnx, 'command', side_effect=AssertionError('unexpected probe')):
            self.assertIsNone(dnx.describe('missing.mp4'))
            args, filters = [], []
            self.assertEqual(dnx.add_source(args, filters, 'missing.png', 3, 'x'), ('3:v', 1))
            self.assertEqual(args, []); self.assertEqual(filters, [])

    def test_one_build_for_concurrent_readers_and_warm_reuse(self):
        def build(*args): time.sleep(.05); self.build(*args)
        def read(_):
            with dnx.mask(self.info) as path: return Path(path).read_bytes()
        with patch.object(dnx, '_build', side_effect=build) as builder:
            with ThreadPoolExecutor(6) as pool: result = list(pool.map(read, range(12)))
            self.assertEqual(result, [b'mask']*12); self.assertEqual(builder.call_count, 1)

    def test_mask_lease_prevents_pruning_then_releases(self):
        with patch.object(dnx, '_build', side_effect=self.build), patch.object(dnx, 'DISK_LIMIT', 1):
            with dnx.mask(self.info) as path:
                dnx.prune(Path(path).parent); self.assertTrue(Path(path).is_file())
                with self.assertRaises(PermissionError): Path(path).unlink()
            self.assertFalse(Path(path).exists())

    def test_deferred_input_is_snapshot_safe_and_builds_only_at_use(self):
        value = dnx.MaskInput(self.info); self.assertIs(copy.deepcopy(value), value)
        with patch.object(dnx, '_build', side_effect=self.build) as builder:
            self.assertEqual(builder.call_count, 0)
            with Inputs(['ffmpeg', '-i', value, '-f', 'null', '-']) as inputs:
                self.assertTrue(inputs.args[2].endswith('.mkv')); self.assertEqual(builder.call_count, 1)
                self.assertEqual(value, str(self.source))

    def test_cancel_and_source_change_never_return_stale_mask(self):
        event = threading.Event(); event.set()
        from app_plugins.builtin.batch_text_video.engine import Cancelled
        with self.assertRaises(Cancelled):
            with dnx.mask(self.info, event): pass
        self.source.write_bytes(b'changed!')
        with self.assertRaisesRegex(ValueError, '改变'):
            with dnx.mask(self.info): pass

    def test_failed_build_not_published_and_retry_works(self):
        with patch.object(dnx, '_build', side_effect=ValueError('broken')):
            with self.assertRaisesRegex(ValueError, 'broken'):
                with dnx.mask(self.info): pass
        with patch.object(dnx, '_build', side_effect=self.build):
            with dnx.mask(self.info) as path: self.assertEqual(Path(path).read_bytes(), b'mask')

    def test_extra_alpha_and_trimap_input_indices_are_distinct(self):
        from app_plugins.builtin.batch_text_video.visual_media import add_plane
        args, filters = [], []
        trimap = self.root/'mask.png'; trimap.write_bytes(b'not decoded during graph preparation')
        layer = dict(kind='image',path=str(self.source),width=100,height=100,x=50,y=50,opacity=100,
                     key_mode='green',key_method='enhanced',key_mask_path=str(trimap),media_loop=True)
        with patch.object(dnx, 'describe', return_value=self.info):
            self.assertEqual(add_plane(args, filters, layer, 2, 'base', 'rose', 160, 90, 2, 30), 3)
        graph = ';'.join(filters)
        self.assertIn('[3:v]', graph); self.assertIn('[4:v]', graph)
        self.assertEqual(sum(isinstance(value, dnx.MaskInput) for value in args), 1)
        self.assertIn('setpts=N/(30.000000000*TB)', graph)

    def test_native_binary_rejects_truncated_and_oversized_headers(self):
        packet, output = self.root/'bad.dnx', self.root/'bad.pgm'; packet.write_bytes(bytes(640))
        result = subprocess.run([dnx.decoder(),'--frame',str(packet),str(output)],capture_output=True,timeout=5)
        self.assertNotEqual(result.returncode, 0); self.assertFalse(output.exists())
        result = subprocess.run([dnx.decoder()],capture_output=True,timeout=5)
        self.assertNotEqual(result.returncode, 0)

    def test_node_compiler_preserves_deferred_alpha_input_and_cancellation(self):
        from types import SimpleNamespace
        from app_plugins.builtin.batch_text_video.workflow_layers import compile_layer
        from model.node_workflow.contracts import FilterOperation
        values=dict(id='r',kind='image',path=str(self.source),source='fixed',use_saved_matte=False)
        operation=FilterOperation('rose',None,'batch_text_video.visual_layer',(('layer',json.dumps(values)),))
        canceled=threading.Event()
        def check():
            if canceled.is_set(): raise InterruptedError('canceled')
        context=SimpleNamespace(ffmpeg='unused',check_canceled=check,log=lambda text:None)
        info=dict(duration=1,video=dict(width=160,height=90,avg_frame_rate='30/1'))
        with patch.object(dnx,'describe',return_value=self.info):
            fragment=compile_layer(operation,str(self.root),context,info)[0]
        command=[part for group in fragment.inputs for part in group]
        self.assertEqual(sum(isinstance(value,dnx.MaskInput) for value in command),1)
        canceled.set()
        with self.assertRaises(InterruptedError):
            with context.deferred_input_factory(command):pass


@unittest.skipUnless(os.environ.get('LZX_DNX_ALPHA_FIXTURE') and os.environ.get('NODE_FFMPEG'), 'Explicit read-only DNxHR Alpha fixture required')
class RealAlphaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.temp=tempfile.TemporaryDirectory(); cls.root=Path(cls.temp.name)
        cls.ffmpeg=os.environ['NODE_FFMPEG']; cls.source=cls.root/'alpha.mov'
        result=subprocess.run([cls.ffmpeg,'-nostdin','-y','-v','error','-i',os.environ['LZX_DNX_ALPHA_FIXTURE'],
            '-map','0:v:0','-c:v','copy','-frames:v','12',str(cls.source)],capture_output=True,timeout=30)
        if result.returncode: raise RuntimeError(result.stderr.decode('utf8','replace'))
        cls.patcher=patch.object(dnx,'cache_root',return_value=cls.root/'cache');cls.patcher.start()
        cls.info=dnx.describe(cls.source,cls.ffmpeg)
        if cls.info is None: raise AssertionError('Fixture has no encoded DNxHR Alpha')

    @classmethod
    def tearDownClass(cls): cls.patcher.stop();cls.temp.cleanup()

    def test_poster_and_stream_first_alpha_are_byte_identical(self):
        import numpy as np
        from app_plugins.builtin.batch_text_video.matting_render import image_array
        source=dnx.poster(self.source,ffmpeg=self.ffmpeg)
        rgba=image_array(source);self.assertEqual(rgba[:,:,3].min(),0);self.assertEqual(rgba[:,:,3].max(),255)
        self.assertTrue(np.any((rgba[:,:,3]>0)&(rgba[:,:,3]<255)))
        args=dnx.raw_args(self.source,self.ffmpeg)
        args[args.index('-f',args.index('-map')):]=['-frames:v','1','-f','rawvideo','-pix_fmt','rgba','pipe:1']
        with Inputs(args) as inputs:
            result=subprocess.run(inputs.args,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        self.assertEqual(result.stdout,rgba.tobytes())
        self.assertEqual(len(list((self.root/'cache').glob(dnx.cache_key(self.info)+'.mkv'))),1)

    def test_all_frames_preserve_alpha_and_seek_starts_at_correct_frame(self):
        import numpy as np
        from app_plugins.builtin.batch_text_video.matting_render import image_array
        args=dnx.raw_args(self.source,self.ffmpeg)
        with Inputs(args) as inputs: result=subprocess.run(inputs.args,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        size=self.info['width']*self.info['height']*4
        self.assertEqual(len(result.stdout),12*size)
        frames=np.frombuffer(result.stdout,np.uint8).reshape(12,self.info['height'],self.info['width'],4)
        for index in (0,1,5,11):
            expected=image_array(dnx.poster(self.source,ffmpeg=self.ffmpeg,position=index/30))
            self.assertEqual(frames[index].tobytes(),expected.tobytes())
        args=dnx.raw_args(self.source,self.ffmpeg,first=5)
        with Inputs(args) as inputs: seek=subprocess.run(inputs.args,capture_output=True,timeout=60)
        self.assertEqual(seek.returncode,0,seek.stderr.decode('utf8','replace'))
        self.assertEqual(seek.stdout,result.stdout[5*size:])

    def test_add_plane_export_and_loop_dissolve_keep_native_alpha(self):
        from app_plugins.builtin.batch_text_video.visual_media import add_plane
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.engine import _run
        from app_plugins.builtin.batch_text_video.visual_loop import LoopInput, cycle
        from app_plugins.builtin.batch_text_video.matting_live import Feed
        layer=normalize_layers([dict(id='r',kind='image',path=str(self.source),width=100,height=100,x=50,y=50,
            use_saved_matte=False,media_loop=True)])[0]
        args=[self.ffmpeg,'-nostdin','-y','-v','error','-filter_complex_threads','1']
        filters=['color=c=blue:s=320x180:r=30:d=0.8,format=rgba[base]']
        self.assertEqual(add_plane(args,filters,layer,0,'base','final',320,180,.8,30,ffmpeg=self.ffmpeg),2)
        output=self.root/'export.mp4'
        _run(args+['-filter_complex',';'.join(filters),'-map','[final]','-t','0.8','-c:v','libx264',str(output)],timeout=60)
        self.assertTrue(output.is_file())
        layer.update(media_loop_transition='fade',media_loop_fade=.1)
        spec=LoopInput(layer,1,30,self.ffmpeg,(320,180));feed=Feed(spec)
        try:
            frames=list(cycle(feed));self.assertEqual(len(frames),12)
            self.assertEqual(len(frames[0]),320*180*4)
            self.assertEqual(frames[0][3],0)
        finally:feed.close()

    def test_source_file_unchanged(self):
        self.assertEqual(dnx.signature(self.source),tuple(self.info['stamp']))

    def test_cancel_build_reaps_children_and_leaves_no_partial_mask(self):
        from app_plugins.builtin.batch_text_video.engine import Cancelled
        root=self.root/'canceled-cache';cancel=threading.Event();processes=[]
        original=subprocess.Popen
        def launch(*args,**kwargs):
            process=original(*args,**kwargs);processes.append(process);return process
        with patch.object(dnx,'cache_root',return_value=root),patch.object(dnx.subprocess,'Popen',side_effect=launch):
            with self.assertRaises(Cancelled):
                with dnx.mask(self.info,cancel,lambda text:cancel.set()):pass
        self.assertTrue(processes);self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertEqual(list(root.glob('*.mkv')),[])
        self.assertEqual(list(root.glob('*.index')),[]);self.assertEqual(list(root.glob('*.log')),[])

    def test_workbench_original_video_preview_uses_same_alpha_at_seek_position(self):
        from app_plugins.builtin.batch_text_video.matting_video import MattingVideoWorker, describe
        from app_plugins.builtin.batch_text_video.engine import probe, resolve_tools
        from app_plugins.builtin.batch_text_video.matting_render import image_array
        from app_plugins.builtin.batch_text_video.matting_settings import normalize
        _, ffprobe=resolve_tools(self.ffmpeg)
        request=dict(generation=7,path=str(self.source),info=describe(probe(self.source,ffprobe)),
                     values=normalize({}),original_only=True,position=5/30,play=False,ffmpeg=self.ffmpeg)
        worker=MattingVideoWorker(request); results=[];errors=[]
        worker.frameReady.connect(lambda value:(results.append(value),worker.acknowledge()))
        worker.failed.connect(errors.append);worker.run()
        self.assertEqual(errors,[]);self.assertEqual(len(results),1)
        expected=image_array(dnx.poster(self.source,ffmpeg=self.ffmpeg,position=5/30))
        self.assertEqual(image_array(results[0]['original']).tobytes(),expected.tobytes())

    @unittest.skipUnless(os.environ.get('LZX_DNX_ALPHA_FULL_PROOF'),'Explicit full-length read-only validation required')
    def test_full_length_original_through_real_export_pipeline(self):
        from app_plugins.builtin.batch_text_video.visual_media import add_plane
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.engine import _run, resolve_tools
        source=os.environ['LZX_DNX_ALPHA_FIXTURE'];stamp=dnx.signature(source)
        info=dnx.describe(source,self.ffmpeg)
        target=Path(os.environ['LZX_DNX_ALPHA_FULL_PROOF'])
        layer=normalize_layers([dict(id='r',kind='image',path=source,width=100,height=100,x=50,y=50,
            use_saved_matte=False,media_loop=False)])[0]
        filters=['color=c=blue:s=960x540:r=30:d=10.033333333,format=rgba[base]']
        args=[self.ffmpeg,'-nostdin','-y','-v','error','-filter_complex_threads','1']
        self.assertEqual(add_plane(args,filters,layer,0,'base','final',960,540,301/30,30,ffmpeg=self.ffmpeg),2)
        started=time.monotonic()
        _run(args+['-filter_complex',';'.join(filters),'-map','[final]','-frames:v','301','-an',
            '-c:v','libx264','-crf','18','-preset','fast',str(target)],timeout=120)
        _, ffprobe=resolve_tools(self.ffmpeg)
        output=json.loads(dnx.command([ffprobe,'-v','error','-count_frames','-show_streams','-of','json',str(target)]))['streams'][0]
        self.assertEqual(int(output['nb_read_frames']),301)
        self.assertEqual(dnx.signature(source),stamp)
        with dnx.mask(info) as mask_path:
            cached=Path(mask_path);cache_stamp=cached.stat().st_mtime_ns
            with patch.object(dnx,'_build',side_effect=AssertionError('Warm cache must not rebuild')):
                with dnx.mask(info) as again:self.assertEqual(again,mask_path)
            evidence=dict(frames=301,export_seconds=time.monotonic()-started,alpha_cache_bytes=cached.stat().st_size,
                          preview_bytes=target.stat().st_size,source_unchanged=True,warm_cache_reused=True,
                          native_alpha=True,davinci_used=False)
        target.with_suffix('.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')
