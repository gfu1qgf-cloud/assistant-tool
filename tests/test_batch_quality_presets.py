"""Final quality presets keep layout/audio, fallback and project state intact."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.store import fresh_state,Store
from app_plugins.builtin.batch_text_video.prepared_export import PreparedExport
from app_plugins.builtin.batch_text_video.playback import preview_key
from model.MediaEncoding import normalize_export_settings,video_encoding_args,quality_label

FFMPEG=os.environ.get('NODE_FFMPEG','')
FFPROBE=str(Path(FFMPEG).with_name('ffprobe.exe')) if FFMPEG else ''


class QualityPresetTests(unittest.TestCase):
    def test_old_settings_keep_original_final_encoding_values(self):
        settings={};normalize_export_settings(settings)
        self.assertEqual(settings['export_quality'],'high')
        for encoder,flag,value in (('cpu','-crf','20'),('nvenc','-cq','19'),('qsv','-global_quality','19')):
            args=video_encoding_args(encoder,quality=settings['export_quality'])
            self.assertEqual(args[args.index(flag)+1],value)

    def test_all_final_backends_have_three_ordered_quality_levels(self):
        for encoder,flag in (('cpu','-crf'),('nvenc','-cq'),('qsv','-global_quality')):
            values=[]
            for quality in ('high','medium','low'):
                args=video_encoding_args(encoder,quality=quality)
                values.append(int(args[args.index(flag)+1]))
                self.assertFalse(any(flag in args for flag in ('-s','-r','-vf','-af','-b:a')))
            self.assertTrue(values[0]<values[1]<values[2],values)

    def test_preview_and_intermediate_quality_do_not_depend_on_export_preset(self):
        for encoder in ('cpu','nvenc','qsv'):
            for options in ({'preview':True},{'cache':True}):
                reference=video_encoding_args(encoder,quality='high',**options)
                for quality in ('medium','low'):
                    self.assertEqual(reference,video_encoding_args(encoder,quality=quality,**options))

    def test_invalid_quality_is_not_silently_ignored(self):
        for value in ('auto','',None,True,[],{}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError,'导出质量'):
                    normalize_export_settings({'export_quality':value})
                with self.assertRaisesRegex(ValueError,'导出质量'):
                    video_encoding_args('cpu',quality=value)
        self.assertIn('高',quality_label('high'))

    def test_quality_is_profile_local_and_save_as_keeps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory);state=fresh_state()
            state['settings']['export_quality']='low';state=store.save(state,0)
            state=store.create_profile(state,'B',True)
            self.assertEqual(state['settings']['export_quality'],'low')
            second=state['active_profile'];state['settings']['export_quality']='medium'
            state=store.save(state,state['revision'])
            state=store.select_profile(state,'default')
            self.assertEqual(state['settings']['export_quality'],'low')
            state=store.select_profile(state,second)
            self.assertEqual(state['settings']['export_quality'],'medium')

    def test_gpu_runtime_fallback_keeps_selected_quality_for_cpu(self):
        for encoder in ('nvenc','qsv'):
            for quality in ('medium','low'):
                with self.subTest(encoder=encoder,quality=quality),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);target=root/'temp.mp4'
                    args=['ffmpeg','-i','source',*video_encoding_args(encoder,quality=quality,workers=3),
                        '-pix_fmt','nv12' if encoder=='qsv' else 'yuv420p',str(target)]
                    calls=[]
                    def run(command,*_args,**_kwargs):
                        calls.append(command)
                        if len(calls)==1:raise RuntimeError('NVENC / MFX session unavailable')
                        target.write_bytes(b'synthetic output')
                    prepared=PreparedExport(args,target,root/'out.mp4','probe',1.,True,None,[],
                        threading.Event(),None,lambda _:None,False,encoder,True,False,3,quality=quality)
                    with patch('app_plugins.builtin.batch_text_video.engine._run',side_effect=run),\
                         patch('app_plugins.builtin.batch_text_video.engine.probe',return_value={'duration':1.,'audio':True}):
                        prepared.encode()
                    cpu=video_encoding_args('cpu',quality=quality,workers=3)
                    self.assertEqual(calls[1][3:3+len(cpu)],cpu)
                    self.assertEqual(calls[1][-3:],['-pix_fmt','yuv420p',str(target)])
                    self.assertEqual((prepared.encoder,prepared.quality),('cpu',quality))


@unittest.skipUnless(Path(FFMPEG).is_file() and Path(FFPROBE).is_file(),'requires explicit test FFmpeg')
class RealQualityExports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def ffmpeg(self,args):
        result=subprocess.run([FFMPEG,'-y','-nostdin','-hide_banner','-loglevel','error',*args],
            capture_output=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr.decode('utf8','replace'))
        return result.stdout

    def test_real_exports_change_video_quality_not_canvas_timing_or_audio(self):
        self.check_exports('cpu')

    @unittest.skipUnless(os.environ.get('TEST_INTEL_QSV')=='1','Intel QSV hardware test must be explicitly enabled')
    def test_real_intel_qsv_presets_keep_quality_and_frame_audio_contracts(self):
        self.check_exports('qsv')

    def check_exports(self,encoder):
        from app_plugins.builtin.batch_text_video.engine import prepare_static_text,probe
        from PIL import Image,ImageChops,ImageStat
        import io
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.mkv'
            self.ffmpeg(['-f','lavfi','-i','testsrc2=s=320x180:r=12:d=2',
                '-f','lavfi','-i','sine=frequency=660:duration=2','-c:v','ffv1',
                '-pix_fmt','yuv420p','-color_primaries','bt709','-color_trc','bt709',
                '-colorspace','bt709','-c:a','pcm_s16le','-shortest',str(source)])
            state=fresh_state();state['settings'].update(ffmpeg_path=FFMPEG,aspect='source',
                fit='cover',auto_crop_black_bars=False,darkness=0,keep_audio=True,music_enabled=False,
                use_voice=False,output_dir=str(root/'outputs'),video_encoder=encoder,encoder_fallback=False)
            for layer in state['settings']['layers']:layer['enabled']=False
            transparent=root/'transparent.png';Image.new('RGBA',(4,4),(0,0,0,0)).save(transparent)
            state['settings']['layers'].append(dict(id='transparent',kind='image',name='透明合成测试',
                source='file',source_file=str(transparent),path=str(transparent),enabled=True,
                opacity=100,x=50,y=50,width=10,height=10))
            job=dict(id='job',path=str(source),name='test',title='',body='')
            state['jobs']=[job]
            before=copy.deepcopy(state);sizes={};errors={};preview=None
            def frame(path):
                return Image.open(io.BytesIO(self.ffmpeg(['-ss','0.75','-i',str(path),
                    '-frames:v','1','-c:v','png','-f','image2pipe','-']))).convert('RGB')
            original=frame(source)
            for quality in ('high','medium','low'):
                state['settings']['export_quality']=quality;job['name']=quality
                key=preview_key(job,state)
                if preview is None:preview=key
                self.assertEqual(key,preview)
                reports=[]
                with prepare_static_text(job,state,root/'private',report=reports.append) as item:
                    self.assertEqual(item.quality,quality)
                    flag='-crf' if encoder=='cpu' else '-global_quality'
                    self.assertEqual(item.args[item.args.index(flag)+1],
                        {'high':'20' if encoder=='cpu' else '19','medium':'23','low':'27'}[quality])
                    self.assertIn('导出质量', '\n'.join(reports))
                    state['settings']['export_quality']='high'
                    self.assertEqual(item.quality,quality) # queued snapshot, not mutable UI state
                    item.encode();path=item.publish()
                info=probe(path,FFPROBE)
                self.assertEqual((info['video']['width'],info['video']['height']),(320,180))
                self.assertEqual(info['video']['avg_frame_rate'],'12/1')
                self.assertAlmostEqual(info['duration'],2.,delta=.05)
                self.assertTrue(info['audio'])
                self.assertEqual(info['video']['pix_fmt'],'yuv420p')
                sizes[quality]=path.stat().st_size
                errors[quality]=sum(ImageStat.Stat(ImageChops.difference(frame(path),original)).mean)
            self.assertGreater(sizes['high'],sizes['medium'])
            self.assertGreater(sizes['medium'],sizes['low'])
            self.assertLess(errors['high'],errors['low'])
            self.assertEqual(state['settings']['layers'],before['settings']['layers'])
            print('QUALITY_PRESET_REAL:',json.dumps({'encoder':encoder,'bytes':sizes,'frame_error':errors}))
