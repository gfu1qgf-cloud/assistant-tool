"""Pure still backgrounds: real finite exports, zoom clocks and explicit pool routing."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import image_background as image_bg
from app_plugins.builtin.batch_text_video.engine import probe, render_static_text, prepare_static_text
from app_plugins.builtin.batch_text_video.store import Store, fresh_state
from app_plugins.builtin.batch_text_video.pools import add_videos, apply_pairs, pairing_plan
from app_plugins.builtin.batch_text_video.pool_ui import PairingDialog
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog, PreviewWorker
from app_plugins.builtin.batch_text_video.preview import Preview
from app_plugins.builtin.batch_text_video.layout import ensure_fonts

FFMPEG = os.environ.get('NODE_FFMPEG', '')


def variant(settings, **changes):
    mapping = {'image_background_motion': 'visual_animation', 'image_background_strength': 'animation_strength',
               'image_background_seconds': 'animation_period', 'image_background_direction': 'animation_direction'}
    result = copy.deepcopy(settings)
    control = result.setdefault('background_visual', {})
    for key, value in changes.items():
        control[mapping.get(key, key)] = value
        if key == 'image_background_seconds': control['animation_zoom_seconds'] = value
    return result


class ImageBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def source(self, root, name='background.png'):
        image = QtGui.QImage(320, 180, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('#305080'))
        painter = QtGui.QPainter(image)
        painter.fillRect(100, 50, 120, 80, QtGui.QColor('#f0c030'))
        painter.fillRect(0, 0, 20, 180, QtGui.QColor('red'))
        painter.end()
        path = Path(root)/name
        self.assertTrue(image.save(str(path)))
        return path

    def state(self, root, **settings):
        state = fresh_state()
        state['settings'].update(aspect='source', fit='cover', darkness=0, background_blur=0,
            music_enabled=False, ffmpeg_path=FFMPEG, output_dir=str(root), overwrite=True,
            unvoiced_duration_seconds=1.2, image_background_motion='zoom_in',
            image_background_strength=50, image_background_seconds=.8)
        state['settings'].update(settings)
        legacy = {key: state['settings'].pop(key) for key in list(state['settings']) if key.startswith('image_background_')}
        state['settings'] = variant(state['settings'], **legacy)
        for layer in state['settings']['layers']: layer['enabled'] = False
        return state

    def run_ffmpeg(self, *args):
        result = subprocess.run([FFMPEG, '-v', 'error', '-nostdin', '-y', *map(str, args)],
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def frames(self, path):
        return np.frombuffer(self.run_ffmpeg('-i', path, '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'),
                             np.uint8).reshape(-1, 180, 320, 3)

    def test_zoom_strength_and_speed_have_independent_bounded_clocks(self):
        base = variant({}, image_background_motion='breathe', image_background_strength=20, image_background_seconds=4)
        self.assertEqual(image_bg.scale_at(base, 0), 1)
        self.assertAlmostEqual(image_bg.scale_at(base, 2), 1.2)
        self.assertAlmostEqual(image_bg.scale_at(base, 4), 1)
        inward = variant(base, image_background_direction='inward')
        self.assertAlmostEqual(image_bg.scale_at(inward, 0), 1.2)
        self.assertAlmostEqual(image_bg.scale_at(inward, 2), 1)
        self.assertAlmostEqual(image_bg.scale_at(inward, 4), 1.2)
        self.assertTrue(all(image_bg.scale_at(inward, t/30) >= 1 for t in range(241)))
        fast = variant(base, image_background_seconds=2)
        self.assertAlmostEqual(image_bg.scale_at(fast, 1), image_bg.scale_at(base, 2))
        for mode in ('zoom_in', 'zoom_out'):
            values = variant(base, image_background_motion=mode)
            values_at = [image_bg.scale_at(values, t) for t in (0, 2, 4, 40)]
            self.assertTrue(all(1 <= value <= 1.2 for value in values_at))
            self.assertEqual(values_at[-1], values_at[-2])
        self.assertEqual(image_bg.scale_at(variant(base, image_background_strength=0), 2), 1)
        self.assertEqual(image_bg.scale_at(variant(base, image_background_motion='none'), 2), 1)
        for invalid in (float('nan'), float('inf'), -1, True):
            with self.assertRaises(ValueError): image_bg.normalized(variant(base, image_background_strength=invalid))

    def test_images_are_backgrounds_only_when_explicitly_imported_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.source(directory)
            state = self.state(directory)
            self.assertEqual(add_videos(state, [str(path), str(path)]), 1)
            apply_pairs(state, pairing_plan([], state['backgrounds'], 'videos'))
            self.assertEqual(state['jobs'][0]['path'], str(path.resolve()))
            store = Store(Path(directory)/'state')
            store.save(state, 0)
            saved = store.load()
            self.assertEqual(saved['settings']['background_visual']['animation_strength'], 50)
            self.assertEqual(saved['backgrounds'], [str(path.resolve())])
            with patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, 'update_preview'):
                dialog = BatchTextVideoDialog(store=Store(Path(directory)/'ui'))
                try:
                    with patch.object(dialog, 'import_pool_images') as overlay:
                        dialog.add_files([str(path)])
                        overlay.assert_called_once()
                    self.assertEqual(dialog.state['backgrounds'], [])
                    dialog.add_files([str(path)], background=True)
                    self.assertEqual(dialog.state['backgrounds'], [str(path.resolve())])
                    self.assertEqual(dialog.state['image_pool'], [])
                    self.assertTrue(dialog.controls['unvoiced_duration_seconds'].isEnabled())
                    self.assertIn('background_pool_id', dialog.controls)
                    self.assertFalse(any(key.startswith('image_background_') for key in dialog.controls))
                    dialog.layer_panel.select_background()
                    self.assertEqual(dialog.layer_panel.current()['kind'], 'background')
                    visual = dialog.layer_panel.visual_properties
                    visual.controls['visual_animation'].setCurrentIndex(0)
                    self.assertFalse(visual.controls['animation_strength'].isEnabled())
                    self.assertEqual(dialog.read_settings()['background_visual']['visual_animation'], 'none')
                finally:
                    dialog.edit_timer.stop(); dialog.close(); dialog.deleteLater()

    def test_custom_image_pool_is_available_in_explicit_background_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.source(directory)
            state = self.state(directory)
            state['resource_pools'] = [dict(id='photos', name='图片背景', kind='image', next_id='',
                                          items=[dict(id='first', path=str(path))])]
            dialog = PairingDialog([], [], state=state, selected_pool='photos')
            try:
                self.assertEqual(dialog.video_source.currentData(), 'photos')
                self.assertEqual(dialog.pairs[0]['path'], str(path))
                self.assertEqual(state['jobs'], [])
            finally: dialog.deleteLater()

    def test_project_pool_is_global_while_each_visual_keeps_its_animation(self):
        from app_plugins.builtin.batch_text_video.background_visual import background_job
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        with tempfile.TemporaryDirectory() as directory:
            paths = [str(self.source(directory, name=f'photo-{i}.png')) for i in range(2)]
            state = self.state(directory)
            state['resource_pools'] = [dict(id='global-bg', name='底层图片', kind='image', next_id='',
                items=[dict(id=str(i), path=path) for i, path in enumerate(paths)])]
            state['settings']['background_pool_id'] = 'global-bg'
            state['jobs'] = [dict(id=f'job-{i}', path='old.mp4', name=f'job-{i}', status='待生成', title='', body='') for i in range(3)]
            self.assertEqual([background_job(job, state)['path'] for job in state['jobs']], [paths[0], paths[1], paths[0]])
            original = copy.deepcopy(state)
            background_job(state['jobs'][0], state)
            self.assertEqual(state, original)
            from app_plugins.builtin.batch_text_video.quick_export import prepare_current_export
            visible = background_job(dict(id='copy-preview', name='quick', title='', body=''), state)
            queued, identifier = prepare_current_export(state, visible, copy_id='copy-preview')
            added_job = next(item for item in queued['jobs'] if item['id'] == identifier)
            self.assertEqual(background_job(added_job, queued)['path'], visible['path'])
            state['settings']['layers'] = normalize_layers([dict(id='photo', kind='image', path=paths[0],
                visual_animation='breathe', animation_strength=7, animation_period=3)])
            store = Store(Path(directory)/'ui'); store.save(state, 0)
            with patch.object(BatchTextVideoDialog, 'request_background'), patch.object(BatchTextVideoDialog, 'update_preview'):
                dialog = BatchTextVideoDialog(store=store)
                try:
                    self.assertEqual(dialog.controls['background_pool_id'].currentData(), 'global-bg')
                    dialog.layer_panel.select_background()
                    visual = dialog.layer_panel.visual_properties
                    visual.controls['visual_animation'].setCurrentIndex(visual.controls['visual_animation'].findData('breathe'))
                    visual.controls['animation_strength'].setValue(30)
                    visual.controls['animation_direction'].setCurrentIndex(visual.controls['animation_direction'].findData('inward'))
                    values = dialog.read_settings()
                    self.assertEqual(values['background_visual']['animation_strength'], 30)
                    self.assertEqual(values['background_visual']['animation_direction'], 'inward')
                    self.assertEqual(next(layer for layer in values['layers'] if layer['id']=='photo')['animation_strength'], 7)
                    dialog.edit_timer.stop(); dialog.persist()
                    self.assertTrue(dialog.undo_edit())
                    self.assertNotEqual(dialog.read_settings()['background_visual']['animation_strength'], 30)
                finally:
                    dialog.edit_timer.stop(); dialog.close(); dialog.deleteLater()

    def test_shared_visual_direction_and_push_duration_are_not_project_settings(self):
        from app_plugins.builtin.batch_text_video.visual_animation import motion_at, expressions
        layer = dict(visual_animation='breathe', animation_strength=20, animation_period=4, animation_direction='inward')
        self.assertAlmostEqual(motion_at(layer, 10, 0)[0], 1.2)
        self.assertAlmostEqual(motion_at(layer, 10, 2)[0], 1)
        self.assertIn('1-(', expressions(layer, 10)[0])
        layer.update(visual_animation='zoom_in', animation_zoom_seconds=2)
        self.assertAlmostEqual(motion_at(layer, 10, 1)[0], 1.1)
        self.assertAlmostEqual(motion_at(layer, 10, 8)[0], 1.2)

    @unittest.skipUnless(FFMPEG, 'real FFmpeg required')
    def test_global_background_pool_is_used_by_export_even_when_row_has_another_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.source(directory)
            state = self.state(directory)
            state['settings']['background_pool_id'] = 'photos'
            state['resource_pools'] = [dict(id='photos', name='图片背景', kind='image', next_id='',
                items=[dict(id='first', path=str(path))])]
            job = dict(id='first-job', path=str(Path(directory)/'missing-video.mp4'), name='global-pool', title='', body='')
            state['jobs'] = [job]
            output, _ = render_static_text(job, state, Path(directory)/'private')
            self.assertTrue(Path(output).is_file())
            self.assertEqual(job['path'], str(Path(directory)/'missing-video.mp4'))

    @unittest.skipUnless(FFMPEG, 'real FFmpeg required')
    def test_bottom_visual_animation_also_handles_video_background_at_its_own_fps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.source(directory)
            video = Path(directory)/'source.mp4'
            self.run_ffmpeg('-loop', '1', '-i', path, '-t', '1.2', '-r', '10', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', video)
            state = self.state(directory)
            for layer in state['settings']['layers']:
                if layer['kind'] == 'title': layer['enabled'] = True
            output, _ = render_static_text(dict(id='video', path=str(video), name='moving-video', title='x', body=''), state, Path(directory)/'private')
            data = self.frames(output)
            self.assertEqual(len(data), 12)
            self.assertGreater(np.abs(data[0].astype(int)-data[8]).mean(), 10)

    def test_metadata_orientation_alpha_and_image_duration_do_not_use_video_probe(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rotated.jpg'
            source = Image.new('RGB', (120, 80), '#f0c030')
            exif = source.getexif(); exif[274] = 6
            source.save(path, exif=exif)
            with patch('app_plugins.builtin.batch_text_video.engine.probe', side_effect=AssertionError('video probe')):
                info = image_bg.probe_background(path, '', {'unvoiced_duration_seconds': 7})
            self.assertEqual((info['video']['width'], info['video']['height']), (80, 120))
            self.assertEqual(info['duration'], 7)
            self.assertFalse(info['audio'])
            decoded = QtGui.QImage(image_bg.materialize(path, Path(directory)/'oriented.png'))
            self.assertEqual((decoded.width(), decoded.height()), (80, 120))
            transparent = QtGui.QImage(12, 10, QtGui.QImage.Format.Format_RGBA8888)
            transparent.fill(QtGui.QColor(0, 255, 0, 0)); transparent.save(str(Path(directory)/'alpha.png'))
            flat = QtGui.QImage(image_bg.materialize(Path(directory)/'alpha.png', Path(directory)/'flat.png'))
            self.assertEqual(flat.pixelColor(0, 0).getRgb(), (0, 0, 0, 255))

    @unittest.skipUnless(FFMPEG, 'real FFmpeg required')
    def test_pure_picture_export_is_finite_zooming_30fps_without_intermediate_video(self):
        ffprobe = str(Path(FFMPEG).with_name('ffprobe.exe'))
        with tempfile.TemporaryDirectory() as directory:
            path = self.source(directory)
            before = path.read_bytes()
            state = self.state(directory)
            job = dict(id='image', path=str(path), name='pure-picture', title='', body='')
            with prepare_static_text(job, state, Path(directory)/'private') as prepared:
                self.assertIn('-framerate', prepared.args)
                self.assertNotIn('-stream_loop', prepared.args)
                self.assertFalse(any('cycle' in str(arg) or str(arg).endswith('.mov') for arg in prepared.args))
                prepared.encode(); output = prepared.publish()
            info = probe(output, ffprobe)
            self.assertAlmostEqual(info['duration'], 1.2, delta=.05)
            self.assertEqual(info['video']['avg_frame_rate'], '30/1')
            self.assertFalse(info['audio'])
            frames = self.frames(output)
            self.assertEqual(len(frames), 36)
            # A centered yellow rectangle grows horizontally, then holds at the requested time.
            widths = [int(((frame[90, :, 0] > 150) & (frame[90, :, 1] > 110)).sum()) for frame in frames]
            self.assertGreater(widths[24], widths[0] * 1.4)
            self.assertLessEqual(abs(widths[24]-widths[-1]), 2)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(Path(directory).rglob('background.png'))[1:])
            self.assertFalse(list(Path(directory).glob('.text-video-*')))

    @unittest.skipUnless(FFMPEG, 'real FFmpeg required')
    def test_breathe_and_zoom_out_speed_and_zero_strength_affect_actual_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.source(directory)
            for mode, strength, direction in [('breathe', 50, 'outward'), ('breathe', 50, 'inward'),
                                              ('zoom_out', 50, 'outward'), ('zoom_in', 0, 'outward')]:
                state = self.state(directory, image_background_motion=mode, image_background_strength=strength,
                                   image_background_direction=direction)
                output, _ = render_static_text(dict(path=str(path), name=mode, title='', body=''), state, Path(directory)/'private')
                frames = self.frames(output)
                self.assertGreater(frames.max(axis=3).min(), 30, 'Shrinking must not reveal new black borders')
                if mode == 'breathe':
                    self.assertGreater(np.abs(frames[0].astype(int)-frames[12]).mean(), 10)
                    self.assertLess(np.abs(frames[0].astype(int)-frames[24]).mean(), 1)
                    yellow_widths = [int(((frame[90, :, 0] > 150) & (frame[90, :, 1] > 110)).sum()) for frame in frames]
                    if direction == 'inward': self.assertGreater(yellow_widths[0], yellow_widths[12]*1.4)
                    else: self.assertGreater(yellow_widths[12], yellow_widths[0]*1.4)
                elif mode == 'zoom_out':
                    self.assertGreater(np.abs(frames[0].astype(int)-frames[24]).mean(), 10)
                    self.assertLess(np.abs(frames[24].astype(int)-frames[-1]).mean(), 1)
                else:
                    self.assertLess(np.abs(frames[0].astype(int)-frames[-1]).mean(), 1)

    @unittest.skipUnless(FFMPEG, 'real FFmpeg required')
    def test_voice_sets_picture_length_and_preview_limit_keeps_full_animation_clock(self):
        ffprobe = str(Path(FFMPEG).with_name('ffprobe.exe'))
        with tempfile.TemporaryDirectory() as directory:
            path = self.source(directory)
            voice = Path(directory)/'voice.wav'
            self.run_ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.6', voice)
            state = self.state(directory, use_voice=True, image_background_seconds=4)
            job = dict(path=str(path), name='voice-picture', title='', body='', voice_path=str(voice), voice_override=True)
            output, _ = render_static_text(job, state, Path(directory)/'private')
            info = probe(output, ffprobe)
            self.assertAlmostEqual(info['duration'], 1.6, delta=.05); self.assertTrue(info['audio'])
            with prepare_static_text(dict(job, name='preview'), state, Path(directory)/'private', preview=True, preview_limit=.6) as prepared:
                self.assertEqual(prepared.duration, .6)
                script = Path(prepared.args[prepared.args.index('-/filter_complex')+1]).read_text('utf-8')
                self.assertIn('/4.000000000', script)
                prepared.encode(); preview = prepared.publish()
            self.assertAlmostEqual(probe(preview, ffprobe)['duration'], .6, delta=.05)

    @unittest.skipUnless(FFMPEG, 'real FFmpeg required')
    def test_picture_preview_reads_without_seek_and_does_not_run_cropdetect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.source(directory)
            results = []
            worker = PreviewWorker(str(path), FFMPEG,
                frame_settings={'aspect': 'source', 'fit': 'cover', 'auto_crop_black_bars': True})
            worker.ready.connect(results.append)
            with patch('app_plugins.builtin.batch_text_video.background_framing.detect_background_crop', side_effect=AssertionError('video crop')):
                worker.run()
            self.assertNotIn('error', results[0])
            self.assertTrue(results[0]['media']['still_image'])
            decoded = QtGui.QImage.fromData(results[0]['image'])
            self.assertFalse(decoded.isNull())
            viewer = Preview(); viewer.resize(320, 180); viewer.setMinimumSize(0, 0)
            viewer.background = decoded; viewer.canvas_size = QtCore.QSize(320, 180); viewer.show_bounds = False
            viewer.background_zoom = 1; first = viewer.grab().toImage()
            viewer.background_zoom = 1.5; last = viewer.grab().toImage()
            self.assertNotEqual(first, last)
            viewer.deleteLater()


if __name__ == '__main__': unittest.main()
