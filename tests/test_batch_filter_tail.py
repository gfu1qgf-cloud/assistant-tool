"""Generated media reproducing the real 7.2 s multi-layer / reverb export."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from app_plugins.builtin.batch_text_video.store import DEFAULTS
from app_plugins.builtin.batch_text_video.mixing import audio_graph
from app_plugins.builtin.batch_text_video.voice_reverb import write_ir
from app_plugins.builtin.batch_text_video.visual_media import add_plane
from qt_compat import QtGui, QtWidgets

FFMPEG = os.environ.get('NODE_FFMPEG', '')


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
class FilterTailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_multilayer_reverb_ducking_finishes_at_partial_audio_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def run(args, timeout=20):
                result = subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner',
                    '-loglevel', 'error', *args], capture_output=True, timeout=timeout)
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace')[-4000:])
                return result
            voice, music, clip = (root/name for name in ('voice.mp3', 'music.mp3', 'clip.mp4'))
            run(['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100:duration=7.17',
                 '-ac', '1', '-c:a', 'libmp3lame', str(voice)])
            run(['-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=44100:duration=10',
                 '-ac', '2', '-c:a', 'libmp3lame', str(music)])
            run(['-f', 'lavfi', '-i', 'testsrc2=s=108x192:r=30:d=0.7',
                 '-c:v', 'libx264', str(clip)])
            settings = {**DEFAULTS, 'voice_fx_preset': 'church', 'voice_duck': True}
            ir = write_ir(root/'ir.wav', settings)
            for threads in (4, 1):
                with self.subTest(filter_threads=threads):
                    args = ['-filter_complex_threads', str(threads),
                        '-f', 'lavfi', '-i', 'color=black:s=108x192:r=30',
                        '-stream_loop', '-1', '-i', str(clip), '-i', str(voice),
                        '-ss', '1.2', '-t', '7.3', '-i', str(music), '-i', str(ir)]
                    graph = [
                        '[0:v]format=rgba[base]',
                        '[1:v]setpts=PTS-STARTPTS,fps=30,format=rgba,scale=108:192,'
                        'colorchannelmixer=aa=0.8,trim=duration=7.2,setsar=1[source]',
                        'color=black@0:s=108x192:r=30:d=7.2,format=rgba[canvas]',
                        "[canvas][source]overlay=x=0:y=0:eval=frame:format=auto:eof_action=pass:repeatlast=0[plane]",
                        '[base]split=2[blend_base][original]',
                        '[plane]split=2[rgb][alpha_source]',
                        '[alpha_source]alphaextract[alpha]',
                        '[blend_base]format=gbrp[base_rgb]', '[rgb]format=gbrp[source_rgb]',
                        '[base_rgb][source_rgb]blend=all_mode=screen[blended]',
                        '[blended][alpha]alphamerge[masked]',
                        '[original][masked]overlay=0:0:format=auto,format=yuv420p[v]',
                    ]
                    audio, _ = audio_graph(settings, 7.2, 2, 3, {'start': 0}, voice_ir_index=4)
                    graph.extend(audio)
                    started = time.monotonic()
                    result = run([*args, '-filter_complex', ';'.join(graph), '-map', '[v]', '-map', '[a]',
                        '-t', '7.2', '-c:v', 'libx264', '-preset', 'ultrafast',
                        '-c:a', 'aac', '-f', 'null', '-'])
                    print(f'tail fixture threads={threads}: {time.monotonic()-started:.3f}s', flush=True)

    def test_full_graph_with_breathing_image_and_native_music_piece(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root/'picture.png'
            picture = QtGui.QImage(108, 192, QtGui.QImage.Format.Format_RGBA8888)
            picture.fill(QtGui.QColor('#804020')); self.assertTrue(picture.save(str(image)))
            def run(args, timeout=20):
                result = subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
                    capture_output=True, timeout=timeout)
                self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace')[-4000:])
            clip = root/'clip.mp4'
            run(['-f', 'lavfi', '-i', 'testsrc2=s=108x192:r=30:d=0.7', '-c:v', 'libx264', str(clip)])
            voice, music = root/'voice.mp3', root/'music.mp3'
            run(['-f', 'lavfi', '-i', 'sine=sample_rate=44100:duration=7.17', '-c:a', 'libmp3lame', str(voice)])
            run(['-f', 'lavfi', '-i', 'sine=sample_rate=44100:duration=10', '-ac', '2', '-c:a', 'libmp3lame', str(music)])
            settings = {**DEFAULTS, 'voice_fx_preset': 'church', 'voice_duck': True}
            ir = write_ir(root/'ir.wav', settings)
            for threads in (4, 1):
                with self.subTest(filter_threads=threads):
                    args = ['-filter_threads', str(threads), '-filter_complex_threads', str(threads),
                        '-f', 'lavfi', '-i', 'color=black:s=108x192:r=30', '-loop', '1', '-i', str(image)]
                    graph = ['[0:v]format=rgba[base]', '[base][1:v]overlay=0:0:shortest=1:format=auto[layer0]']
                    layer = dict(kind='image', path=str(image), x=50, y=50, width=100, height=100,
                        opacity=100, visual_animation='breathe', animation_strength=8, animation_period=5)
                    add_plane(args, graph, layer, 2, 'layer0', 'layer1', 108, 192, 7.2, 30)
                    video = dict(kind='image', path=str(clip), x=50, y=50, width=100, height=100,
                        opacity=80, blend_mode='screen')
                    add_plane(args, graph, video, 3, 'layer1', 'layer2', 108, 192, 7.2, 30)
                    args += ['-loop', '1', '-i', str(image), '-i', str(voice), '-ss', '1.2', '-t', '7.3',
                             '-i', str(music), '-i', str(ir)]
                    graph += ['[layer2][4:v]overlay=0:0:shortest=1:format=auto,format=yuv420p[v]',
                        '[6:a]atrim=duration=7.2,asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,'
                        'apad=whole_dur=7.2,atrim=duration=7.2[piece]', '[piece]concat=n=1:v=0:a=1[music_source]']
                    audio, _ = audio_graph(settings, 7.2, 5, 6, {'start': 0}, voice_ir_index=7,
                        music_input='[music_source]', music_start=0)
                    graph.extend(audio)
                    run([*args, '-filter_complex', ';'.join(graph), '-map', '[v]', '-map', '[a]',
                        '-t', '7.2', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-f', 'null', '-'])
