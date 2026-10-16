import base64
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from qt_compat import QtGui, QtWidgets
from model.BackgroundTasks import TaskRunContext
from model.node_workflow.contracts import ColorValue, RenderPlan
from model.node_workflow.graph import NodeRegistry, register_core
from model.node_workflow.visual_nodes import build_background, build_transform, build_lut, compile_lut, read_cube, lut_rows, prepare_lut
from model.node_workflow.preview import frame_preview
from model.node_workflow.render import render_plan, probe
from model.node_workflow.video_stream import compose
from app_plugins.builtin.batch_text_video.workflow_layers import build_layer, layer_node

FFMPEG = os.environ.get('NODE_FFMPEG', '')


class VisualUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        self.registry = NodeRegistry(); register_core(self.registry)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.c = TaskRunContext(threading.Event(), lambda *_: None, lambda *_: None)
        self.c.ffmpeg, self.c.workflow_registry, self.c.node_prepared = FFMPEG, self.registry, {}
    def params(self, kind, **values):
        return {**{p.key: copy.deepcopy(p.default) for p in self.registry.definitions['core.'+kind].parameters}, **values}
    def background(self, **values):
        return build_background({}, self.params('background', width=160, height=90, duration=.6, fps=10, **values), self.c)['video']
    def frame(self, video, operations=()):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            encoded, _ = frame_preview(RenderPlan(video, operations, ''), 0, self.c, directory, 1080)
            return QtGui.QImage.fromData(base64.b64decode(encoded))

    def test_cube_bounded_data_and_original_looks(self):
        for style in ('warm', 'cool', 'soft', 'vintage', 'mono'):
            rows = lut_rows(style, 3)
            self.assertEqual(len(rows), 27); self.assertTrue(all(0 <= v <= 1 for row in rows for v in row))
        file = self.root/'invalid.cube'; file.write_text('LUT_3D_SIZE 2\nimport os\n', encoding='utf8')
        with self.assertRaises(ValueError): read_cube(file)
        file.write_text('LUT_3D_SIZE 2\n0 0 0\n', encoding='utf8')
        with self.assertRaisesRegex(ValueError, '不完整'): read_cube(file)
    def test_hdr_rejected_and_alpha_typed_color_override(self):
        operation = build_lut({}, self.params('lut'), self.c)['operations'][0]
        info = {'video': {'color_transfer': 'smpte2084'}, 'duration': 1}
        with self.assertRaisesRegex(ValueError, 'HDR'): compile_lut(operation, self.root, self.c, info)
        self.assertEqual(build_lut({'strength': 0}, self.params('lut'), self.c)['operations'][0].name, 'null')
        params = {p.key: copy.deepcopy(p.default) for p in layer_node('text').parameters}
        params['layer']['text'] = 'Ľúbim ťa'; params['layer']['color'] = '#ffffff'
        self.c.node_prepared = {}; self.c.task = None
        layer = json.loads(dict(build_layer({'color': ColorValue(1, 0, 0, .5)}, params, self.c)['operations'][0].options)['layer'])
        self.assertEqual(layer['color'], '#80ff0000'); self.assertEqual(params['layer']['color'], '#ffffff')

    def test_connected_lut_file_overrides_empty_or_invalid_fallback(self):
        valid = self.root / 'valid.cube'
        rows = lut_rows('mono', 2)
        valid.write_text('LUT_3D_SIZE 2\n' + '\n'.join(' '.join(map(str, row)) for row in rows), encoding='utf8')
        invalid = self.root / 'invalid.cube'; invalid.write_text('broken', encoding='utf8')
        for fallback in ('', str(self.root / 'missing.cube'), str(invalid)):
            with self.subTest(fallback=fallback):
                params = self.params('lut', style='file', file=fallback)
                self.c.node_prepared = prepare_lut(None, params, None)
                operation = build_lut({'file': str(valid)}, params, self.c)['operations'][0]
                self.assertEqual(dict(operation.assets)['lut'].path, str(valid.resolve()))

    def test_static_lut_file_keeps_submission_stamp(self):
        file = self.root / 'static.cube'
        file.write_text('LUT_3D_SIZE 2\n' + '\n'.join(' '.join(map(str, row)) for row in lut_rows('mono', 2)), encoding='utf8')
        params = self.params('lut', style='file', file=str(file))
        self.c.node_prepared = prepare_lut(None, params, None)
        file.write_text('changed data', encoding='utf8')
        with self.assertRaises(ValueError):
            build_lut({}, params, self.c)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_real_solid_linear_radial_and_single_encoding(self):
        red = self.background(color='#ff0000')
        image = self.frame(red); self.assertEqual((image.width(), image.height()), (160, 90)); self.assertGreater(image.pixelColor(80, 40).red(), 240)
        for style in ('linear', 'radial'):
            video = self.background(style=style, color='#ff0000', color2='#0000ff')
            image = self.frame(video); self.assertNotEqual(image.pixelColor(5, 5), image.pixelColor(150, 80))
        result = render_plan(RenderPlan(red, (), str(self.root/'result.mp4')), self.c, FFMPEG)
        self.assertEqual(result['video_encodings'], 1); self.assertEqual(result['intermediate_videos'], 0)
        info = probe(result['video'], str(Path(FFMPEG).with_name('ffprobe.exe')))
        self.assertAlmostEqual(info['duration'], .6, delta=.11)

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_real_transform_scale_anchor_rotation_custom_canvas(self):
        source = self.background(color='#ff0000')
        transformed = build_transform({'video': source}, self.params('transform', scale_x=.5, scale_y=.5, edge='solid', color='#0000ff'), self.c)['video']
        image = self.frame(transformed)
        self.assertGreater(image.pixelColor(80, 45).red(), 230); self.assertGreater(image.pixelColor(5, 5).blue(), 230)
        rotated = build_transform({'video': source}, self.params('transform', rotation=90, canvas='custom', width=101, height=91, edge='solid', color='#0000ff'), self.c)['video']
        image = self.frame(rotated); self.assertEqual((image.width(), image.height()), (101, 91))
        result = render_plan(RenderPlan(rotated, (), str(self.root/'odd.mp4')), self.c, FFMPEG)
        info = probe(result['video'], str(Path(FFMPEG).with_name('ffprobe.exe')))
        self.assertEqual((info['video']['width'], info['video']['height']), (101, 91))

    @unittest.skipUnless(FFMPEG and Path(FFMPEG).is_file(), '需要正式FFmpeg')
    def test_real_lut_pixels_and_alpha_preserved_during_composition(self):
        source = self.background(color='#c06020')
        ops = build_lut({}, self.params('lut', style='mono', strength=1), self.c)['operations']
        image = self.frame(source, ops); pixel = image.pixelColor(80, 45)
        self.assertLess(abs(pixel.red()-pixel.blue()), 3); self.assertLess(abs(pixel.red()-pixel.green()), 3)
        # If LUT drops Alpha, transparent red becomes an opaque red rectangle.
        transparent = self.background(color='#ff000000')
        stream = compose(transparent, (ops,), self.c)
        scaled = build_transform({'video': stream}, self.params('transform', edge='solid', color='#0000ff'), self.c)['video']
        pixel = self.frame(scaled).pixelColor(80, 45)
        self.assertGreater(pixel.blue(), 230); self.assertLess(pixel.red(), 10)
