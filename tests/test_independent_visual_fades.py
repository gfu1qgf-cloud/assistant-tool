"""Movement durations and alpha fades are independent in UI, Qt and FFmpeg."""
import copy
from pathlib import Path
import unittest

from app_plugins.builtin.batch_text_video.overlay_timing import normalize_timing, resolve_window, transform_at
from app_plugins.builtin.batch_text_video.visual_animation import preview_transform
from app_plugins.builtin.batch_text_video.visual_media import VisualProperties
from app_plugins.builtin.batch_text_video.layers import compose_layers
import test_visual_transitions as fixture


class IndependentFadeTests(unittest.TestCase):
    setUpClass = classmethod(fixture.VisualTransitionTests.setUpClass.__func__)
    setUp = fixture.VisualTransitionTests.setUp
    ffmpeg = fixture.NativeVisualTransitionTests.ffmpeg
    frames = fixture.NativeVisualTransitionTests.frames

    def layer(self, mode='left', **timing):
        layer = fixture.VisualTransitionTests.layer(self, mode)
        layer['visual_timing'].update({'fade_in': 0, 'fade_out': 0, **timing})
        return layer

    def test_zero_fades_keep_all_movement_modes_opaque(self):
        for mode in ('left', 'right', 'up', 'down', 'small', 'large'):
            window = resolve_window(self.layer(mode)['visual_timing'], 3)
            for at in (.5, .7, 1.5, 2.3):
                self.assertEqual(transform_at(window, at)[0], 1, (mode, at))
            self.assertNotEqual(transform_at(window, .7)[1:], transform_at(window, 1.5)[1:])
            self.assertEqual(transform_at(window, 0)[0], 0)
            self.assertEqual(transform_at(window, 2.5)[0], 0)

    def test_fade_and_movement_finish_at_separate_times(self):
        window = resolve_window(self.layer(enter_duration=.2, fade_in=.8)['visual_timing'], 3)
        alpha, scale, x, y = transform_at(window, .9)
        self.assertAlmostEqual(alpha, .5); self.assertEqual(x, 0)
        window = resolve_window(self.layer(enter_duration=.8, fade_in=.2)['visual_timing'], 3)
        self.assertEqual(transform_at(window, .9)[0], 1)
        self.assertLess(transform_at(window, .9)[2], 0)
        window = resolve_window(self.layer(exit_duration=.2, fade_out=.8)['visual_timing'], 3)
        self.assertAlmostEqual(transform_at(window, 2.1)[0], .5)
        self.assertEqual(transform_at(window, 2.1)[2], 0)

    def test_widget_fades_always_editable_and_do_not_change_motion(self):
        panel = VisualProperties(); self.addCleanup(panel.deleteLater)
        layer = self.layer(); panel.set_layer(layer); values = []
        panel.changed.connect(lambda v: values.append(copy.deepcopy(v)))
        controls = panel.appearance_editor.controls
        self.assertNotIn('fade', [controls['enter'].itemData(i) for i in range(controls['enter'].count())])
        controls['fade_in'].setValue(.3)
        self.assertEqual(values[-1]['visual_timing']['enter'], 'left')
        self.assertEqual(values[-1]['visual_timing']['enter_duration'], .8)
        controls['enter_duration'].setValue(1.2)
        self.assertEqual(values[-1]['visual_timing']['fade_in'], .3)
        controls['fade_in'].setValue(0)
        self.assertEqual(values[-1]['visual_timing']['enter_duration'], 1.2)
        controls['enter'].setCurrentIndex(controls['enter'].findData('cut'))
        self.assertTrue(controls['fade_in'].isEnabled())
        self.assertTrue(controls['fade_out'].isEnabled())

    def test_legacy_pure_fade_migrates_to_independent_fields(self):
        settings = normalize_timing(dict(enter='fade', exit='fade', enter_duration=.7, exit_duration=.9))
        self.assertEqual((settings['enter'], settings['exit']), ('cut', 'cut'))
        self.assertEqual((settings['fade_in'], settings['fade_out']), (.7, .9))
        old_direction = normalize_timing(dict(enter='left', exit='right', enter_duration=.7, exit_duration=.9))
        self.assertEqual((old_direction['fade_in'], old_direction['fade_out']), (0, 0))
        for key in ('fade_in', 'fade_out'):
            for bad in (True, -1, float('nan'), 61):
                with self.subTest(key=key, value=bad), self.assertRaises(ValueError):
                    normalize_timing({key: bad})

    @unittest.skipUnless(fixture.FFMPEG and Path(fixture.FFMPEG).is_file(), 'requires FFmpeg')
    def test_real_ffmpeg_no_fade_frames_match_design_for_every_motion(self):
        for mode in ('left', 'right', 'up', 'down', 'small', 'large'):
            with self.subTest(mode=mode):
                layer = self.layer(mode); frames = self.frames(layer)
                for index, at in enumerate(fixture.TIMES):
                    expected = fixture.rgba(compose_layers({}, [layer], 320, 240, 0,
                                           editor_timing={'duration': 3, 'time': at}))
                    a, b = fixture.metrics(frames[index]), fixture.metrics(expected)
                    self.assertEqual(a is None, b is None)
                    if a is not None:
                        self.assertEqual(a[4], 255)
                        self.assertLessEqual(max(abs(a[:4]-b[:4])), 3)

    @unittest.skipUnless(fixture.FFMPEG and Path(fixture.FFMPEG).is_file(), 'requires FFmpeg')
    def test_real_pure_fade_has_no_displacement(self):
        layer = self.layer('cut', fade_in=.8, fade_out=.8)
        frames = self.frames(layer)
        hold = fixture.metrics(frames[3])
        for index in (2, 4):
            current = fixture.metrics(frames[index])
            self.assertTrue(0 < current[4] < 255)
            self.assertEqual(current[:4].tolist(), hold[:4].tolist())
