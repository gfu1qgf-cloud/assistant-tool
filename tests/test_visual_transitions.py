"""Native visual entrance/exit UI, persistence, conversion and real frame parity."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from qt_compat import QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.layers import normalize_layers, compose_layers
from app_plugins.builtin.batch_text_video.store import Store, DEFAULTS, fresh_state
from app_plugins.builtin.batch_text_video.visual_media import VisualProperties, add_plane, needs_plane
from app_plugins.builtin.batch_text_video.visual_animation import show_window, preview_transform
from app_plugins.builtin.batch_text_video.overlay_timing import resolve_window, transform_at
from app_plugins.builtin.batch_text_video.component_nodes import component_graph
from app_plugins.builtin.batch_text_video.node_effects import fallback_registry, new_effect
from app_plugins.builtin.batch_text_video.design_preview import prepare_design_bundle, design_layers
from model.node_workflow.library import WorkflowLibrary

FFMPEG = os.environ.get('NODE_FFMPEG', '')
MODES = ('fade', 'left', 'right', 'up', 'down', 'small', 'large')
TIMES = (0., .5, .7, 1.5, 2.3, 2.5)


def rgba(image):
    image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits().asstring(image.sizeInBytes()), np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(image.height(), image.width(), 4).copy()


def metrics(pixels):
    alpha = pixels[:, :, 3]
    ys, xs = np.where(alpha > 3)
    if not len(xs):
        return None
    return np.array([(xs.min()+xs.max())/2, (ys.min()+ys.max())/2,
                     xs.max()-xs.min()+1, ys.max()-ys.min()+1, alpha.max()], float)


class VisualTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.picture = self.root / 'visual.png'
        image = QtGui.QImage(80, 60, QtGui.QImage.Format.Format_RGBA8888)
        image.fill(QtGui.QColor(230, 50, 30))
        self.assertTrue(image.save(str(self.picture)))

    def layer(self, mode='left', **extra):
        layer = dict(id='picture', name='测试图片', kind='image', path=str(self.picture), source='fixed',
                     x=50, y=50, width=25, height=25, visual_timing_enabled=True,
                     visual_timing=dict(start=.5, end=2.5, enter=mode, exit=mode,
                         enter_duration=.8, exit_duration=.8, fade_in=.8, fade_out=.8, distance=40, clock='restart'))
        layer.update(extra)
        return normalize_layers([layer])[0]

    def test_old_project_keeps_animation_disabled_and_no_shared_nested_defaults(self):
        layers = normalize_layers([dict(id='old', kind='image', path=str(self.picture))])
        self.assertFalse(layers[0]['visual_timing_enabled'])
        self.assertIsNone(show_window(layers[0], 3))
        other = normalize_layers([dict(id='another', kind='image', path=str(self.picture))])[0]
        layers[0]['visual_timing']['enter'] = 'left'
        self.assertEqual(other['visual_timing']['enter'], 'cut')

    def test_widget_independent_entry_exit_and_loading_does_not_emit(self):
        panel = VisualProperties()
        self.addCleanup(panel.deleteLater)
        changed = []
        panel.changed.connect(changed.append)
        layer = self.layer()
        before = copy.deepcopy(layer)
        panel.set_layer(layer)
        self.assertFalse(changed)
        editor = panel.appearance_editor
        for field in ('enter', 'exit'):
            self.assertEqual({editor.controls[field].itemData(i) for i in range(editor.controls[field].count())},
                             set(MODES) - {'fade'} | {'cut'})
        editor.controls['exit'].setCurrentIndex(editor.controls['exit'].findData('down'))
        editor.controls['enter_duration'].setValue(.6)
        self.assertEqual(changed[-1]['visual_timing']['enter'], 'left')
        self.assertEqual(changed[-1]['visual_timing']['exit'], 'down')
        self.assertEqual(changed[-1]['visual_timing']['enter_duration'], .6)
        panel.appearance_toggle.setChecked(False)
        self.assertFalse(changed[-1]['visual_timing_enabled'])
        panel.appearance_toggle.setChecked(True)
        self.assertEqual(changed[-1]['visual_timing']['exit'], 'down')
        self.assertEqual(layer, before)
        panel.set_layer(self.layer('up'))
        self.assertEqual(editor.controls['enter'].currentData(), 'up')

    def test_settings_survive_save_reload_and_component_conversion(self):
        state = fresh_state()
        layer = self.layer('right', visual_animation='breathe')
        state['settings']['layers'] = [layer]
        saved = Store(self.root / 'private').save(state, 0)
        loaded = Store(self.root / 'private').load()
        for key in ('visual_timing_enabled', 'visual_timing', 'visual_animation'):
            self.assertEqual(loaded['settings']['layers'][0][key], layer[key])
        graph, _ = component_graph(layer, DEFAULTS, fallback_registry())
        visual = next(n for n in graph['nodes'] if n['kind'] == 'batch_text_video.visual_layer')
        self.assertEqual(visual['params']['layer']['visual_timing'], layer['visual_timing'])
        self.assertTrue(visual['params']['layer']['visual_timing_enabled'])
        self.assertEqual(saved['settings']['layers'][0]['path'], str(self.picture))

    def test_short_window_keeps_hold_and_invalid_values_are_rejected(self):
        layer = self.layer()
        layer['visual_timing'].update(start=0, end=0, enter_duration=2, exit_duration=2)
        window = show_window(layer, .5)
        self.assertLess(window.enter_duration+window.exit_duration, .5)
        self.assertEqual(transform_at(window, .25), (1., 1., 0., 0.))
        for field, value in (('visual_timing_enabled', 1), ('visual_timing', {'enter': 'movie=remote'}),
                             ('visual_timing', {'start': 2, 'end': 1}),
                             ('visual_timing', {'enter_duration': float('nan')})):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                normalize_layers([{**self.layer(), field: value}])

    def test_display_window_node_accepts_converted_visual_layer(self):
        from app_plugins.builtin.batch_text_video.workflow_layers import prepare_layer, build_layer
        from app_plugins.builtin.batch_text_video.workflow_timing import build_timing, read_operation
        params = {'layer': self.layer()}
        context = SimpleNamespace(node_prepared=prepare_layer(None, params, None))
        operation = build_layer({}, params, context, media='visual')['operations'][0]
        wrapped = build_timing({'operations': (operation,)}, {'start': .5, 'end': 2.5}, context)['operations'][0]
        source, timing = read_operation(wrapped)
        self.assertEqual(source.name, 'batch_text_video.visual_layer')
        self.assertEqual(timing['start'], .5)

    def test_quick_preview_is_hidden_before_after_and_respects_direction(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                layer = self.layer(mode)
                self.assertTrue(needs_plane(layer))
                before = copy.deepcopy(layer)
                frames = [rgba(compose_layers({}, [layer], 320, 240, 0,
                    editor_timing={'duration': 3, 'time': time})) for time in TIMES]
                self.assertIsNone(metrics(frames[0]))
                self.assertIsNone(metrics(frames[1]))
                self.assertIsNone(metrics(frames[-1]))
                entering, hold, exiting = (metrics(frames[index]) for index in (2, 3, 4))
                self.assertAlmostEqual(hold[4], 255, delta=1)
                self.assertTrue(0 < entering[4] < 255)
                self.assertTrue(0 < exiting[4] < 255)
                axis = 0 if mode in {'left', 'right'} else 1
                if mode in {'left', 'right', 'up', 'down'}:
                    sign = -1 if mode in {'left', 'up'} else 1
                    self.assertGreater(sign*(entering[axis]-hold[axis]), 15)
                    self.assertGreater(sign*(exiting[axis]-hold[axis]), 15)
                if mode in {'small', 'large'}:
                    sign = -1 if mode == 'small' else 1
                    self.assertGreater(sign*(entering[2]-hold[2]), 5)
                self.assertEqual(layer, before)

    def test_converted_visual_uses_same_fast_preview_without_encoding_or_pool_progress(self):
        layer = self.layer('left')
        registry = fallback_registry()
        graph, _ = component_graph(layer, DEFAULTS, registry)
        library = WorkflowLibrary(self.root / 'workflows')
        stored = library.save(graph, '图片入场', registry)
        effect = new_effect(); effect['workflow_id'] = stored['workflow_id']
        source = self.root / 'identity.mp4'; source.write_bytes(b'not decoded by design preview')
        job = dict(id='job', name='Test', path=str(source), title='', body='', task_dir=str(self.root))
        state = fresh_state(); state['jobs'] = [job]; state['settings']['layers'] = [effect]
        services = SimpleNamespace(available_nodes=lambda: registry, workflow_library=library)
        bundle, registry = prepare_design_bundle(state, job, services)
        before = copy.deepcopy(state)
        from app_plugins.builtin.batch_text_video.layout import render_preview_overlay
        with patch('subprocess.Popen', side_effect=AssertionError('quick preview must not encode')):
            for time in (.7, 1.5, 2.3):
                settings = {**state['settings'], 'darkness': 0, '_editor_timing': {'duration': 3, 'time': time}}
                expanded, notices = design_layers(settings, job, bundle, registry, 320, 240, self.root, threading.Event())
                self.assertFalse(notices)
                converted = render_preview_overlay('', '', 320, 240, expanded)[0]
                expected = compose_layers({}, [layer], 320, 240, 0, editor_timing=settings['_editor_timing'])
                self.assertTrue(np.array_equal(rgba(converted), rgba(expected)))
        self.assertEqual(state, before)


@unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), 'requires explicit test FFmpeg')
class NativeVisualTransitionTests(VisualTransitionTests):
    def ffmpeg(self, args):
        result = subprocess.run([FFMPEG, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
            capture_output=True, timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def frames(self, layer, *, timing=None):
        args, filters = [], ['[0:v]format=rgba[base]']
        add_plane(args, filters, layer, 1, 'base', 'output', 320, 240, 3, 20, timing=timing)
        select = '+'.join(f'eq(n,{round(time*20)})' for time in TIMES)
        filters.append(f"[output]select='{select}',format=rgba[selected]")
        result = subprocess.run([FFMPEG, '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
            '-i', 'color=c=black@0:s=320x240:r=20:d=3,format=rgba', *args, '-filter_complex', ';'.join(filters),
            '-map', '[selected]', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'],
            capture_output=True, timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        return np.frombuffer(result.stdout, np.uint8).reshape(len(TIMES), 240, 320, 4)

    def test_native_frames_match_design_motion_and_alpha_for_every_preset(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                layer = self.layer(mode)
                actual = self.frames(layer)
                for index, time in enumerate(TIMES):
                    expected = rgba(compose_layers({}, [layer], 320, 240, 0,
                        editor_timing={'duration': 3, 'time': time}))
                    a, b = metrics(actual[index]), metrics(expected)
                    self.assertEqual(a is None, b is None, (mode, time, a, b))
                    if a is not None:
                        self.assertLessEqual(float(abs(a[:4]-b[:4]).max()), 3, (mode, time, a, b))
                        self.assertAlmostEqual(a[4], b[4], delta=3)

    def test_external_timing_overrides_native_window_without_double_fade(self):
        layer = self.layer('left')
        layer['visual_timing'].update(start=1.8, end=2.9, enter='small')
        external = resolve_window(dict(start=.5, end=2.5, enter='fade', exit='fade',
            enter_duration=.8, exit_duration=.8, clock='restart'), 3)
        actual = self.frames(layer, timing=external)
        layer['visual_timing_enabled'] = False
        expected = self.frames(layer, timing=external)
        np.testing.assert_array_equal(actual, expected)

    def test_entry_combines_with_existing_breathe_animation(self):
        layer = self.layer('fade', visual_animation='breathe', animation_strength=20, animation_period=1)
        actual = self.frames(layer)
        for index, time in enumerate(TIMES):
            expected = rgba(compose_layers({}, [layer], 320, 240, 0,
                editor_timing={'duration': 3, 'time': time}))
            a, b = metrics(actual[index]), metrics(expected)
            self.assertEqual(a is None, b is None)
            if a is not None:
                self.assertLessEqual(float(abs(a[:4]-b[:4]).max()), 3, (time, a, b))
                self.assertAlmostEqual(a[4], b[4], delta=3)

    def test_video_and_gif_source_clocks_and_nonlooping_end_are_respected(self):
        video, gif = self.root / 'two-colors.mp4', self.root / 'two-colors.gif'
        self.ffmpeg(['-f', 'lavfi', '-i', 'color=c=red:s=80x60:r=10:d=0.5',
                     '-f', 'lavfi', '-i', 'color=c=green:s=80x60:r=10:d=0.5',
                     '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]',
                     '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)])
        self.ffmpeg(['-i', str(video), '-loop', '0', str(gif)])
        for source in (video, gif):
            with self.subTest(source=source.suffix):
                layer = self.layer('fade', path=str(source))
                restart = self.frames(layer)
                layer['visual_timing']['clock'] = 'global'
                global_clock = self.frames(layer)
                self.assertGreater(int(restart[2,120,160,0]), int(restart[2,120,160,1])+30)
                self.assertGreater(int(global_clock[2,120,160,1]), int(global_clock[2,120,160,0])+30)
                layer['visual_timing']['clock'] = 'restart'
                layer['media_loop'] = False
                ended = self.frames(layer)
                self.assertIsNotNone(metrics(ended[2]))
                self.assertIsNone(metrics(ended[4]), 'Finished nonlooping media must not freeze or reappear')

    def test_encoded_export_playback_and_converted_node_match(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        from app_plugins.builtin.batch_text_video.node_effects import prepare_bundle, EffectRuntime
        source = self.root / 'background.mp4'
        self.ffmpeg(['-f', 'lavfi', '-i', 'color=c=blue:s=320x240:r=20:d=3',
                     '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)])
        layer = self.layer('left')
        layer['visual_timing']['exit'] = 'down'
        registry = fallback_registry()
        graph, _ = component_graph(layer, DEFAULTS, registry)
        library = WorkflowLibrary(self.root / 'workflows')
        stored = library.save(graph, '入场退场', registry)
        effect = new_effect(); effect['workflow_id'] = stored['workflow_id']
        services = SimpleNamespace(available_nodes=lambda: registry, workflow_library=library)
        outputs = []
        for variant in ('native', 'node', 'playback'):
            state = fresh_state()
            state['settings'].update(aspect='source', darkness=0, music_enabled=False, use_voice=False,
                ffmpeg_path=FFMPEG, output_dir=str(self.root / 'out'), layers=[effect if variant == 'node' else layer])
            job = dict(id='job', path=str(source), name=variant, title='', body='', task_dir='')
            state['jobs'] = [job]
            runtime = None
            if variant == 'node':
                bundle, registry, _ = prepare_bundle(state, ['job'], services)
                runtime = EffectRuntime(bundle, registry)
            output, _ = render_static_text(job, state, self.root / 'private', node_effects=runtime,
                                           preview=variant == 'playback')
            select = '+'.join(f'eq(n,{round(t*20)})' for t in TIMES)
            decoded = self.ffmpeg(['-i', str(output), '-vf', f"select='{select}'", '-fps_mode', 'passthrough',
                                  '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])
            outputs.append(np.frombuffer(decoded, np.uint8).reshape(len(TIMES),240,320,3))
        for other in outputs[1:]:
            self.assertLess(float(abs(outputs[0].astype(float)-other).mean()), 1.5)
        for index in (0, 1, 5):
            self.assertLess(float(outputs[0][index,:,:,0].mean()), 2)
            self.assertGreater(float(outputs[0][index,:,:,2].mean()), 240)
        self.assertGreater(float(outputs[0][3,120,160,0]), 200)
