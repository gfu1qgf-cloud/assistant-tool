import copy
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video.components import new_text_component
from app_plugins.builtin.batch_text_video.layers import normalize_layers, ImageCache
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay, render_preview_overlay, TextDoesNotFit
from app_plugins.builtin.batch_text_video.text_wrap import (
    normalize_wrap, bind_exclusions, exclusions, flow_layout, intervals)
from app_plugins.builtin.batch_text_video.text_components import render_text_box, text_padding
from app_plugins.builtin.batch_text_video.captions import static_planes
from app_plugins.builtin.batch_text_video.store import DEFAULTS, Store, fresh_state
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel


def text(**changes):
    layer = new_text_component()
    layer.update(id='paragraph', name='环绕正文', x=50, y=50, width=94, height=94,
                 font_min=24, font_max=24, align='left', vertical='top', outline_width=0,
                 padding=4, word_spacing=0, text=('文字自动绕开所选元素。Text flows around objects. ')*14,
                 wrap_enabled=True, wrap_targets=['object'], wrap_gap=10)
    layer.update(changes)
    return normalize_layers([layer])[0]


def obstacle(**changes):
    layer = dict(id='object', kind='image', name='人物图片', path='not-decoded.png',
                 x=50, y=43, width=34, height=34, enabled=True, opacity=100)
    layer.update(changes)
    return normalize_layers([layer])[0]


def disabled_defaults(layers):
    return [dict(item, enabled=False) if item['id'] in {'title', 'body'} else item
            for item in normalize_layers(layers)]


class TextWrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def shape(self, layer, objects, width=540, height=960, size=None):
        bind_exclusions([layer], objects)
        scale = width/1080
        padding = text_padding(layer, scale)
        box = QtCore.QRectF(width*(layer['x']-layer['width']/2)/100,
                           height*(layer['y']-layer['height']/2)/100,
                           width*layer['width']/100, height*layer['height']/100)
        box = box.intersected(QtCore.QRectF(0, 0, width, height)).adjusted(padding, padding, -padding, -padding)
        rects = exclusions(layer, width, height, padding)
        groups, total, start = flow_layout(layer, (size or layer['font_max'])*scale, box, rects, scale)
        return box, rects, groups, total, start

    def assert_complete_and_clear(self, layer, box, rects, groups, total):
        self.assertTrue(math.isfinite(total))
        paragraphs = [p for p in layer['text'].replace('\r\n', '\n').split('\n') if p]
        self.assertEqual(len(groups), len(paragraphs))
        for (native, _), paragraph in zip(groups, paragraphs):
            consumed = 0
            for i in range(native.lineCount()):
                line = native.lineAt(i)
                self.assertEqual(line.textStart(), consumed)
                consumed += line.textLength()
                ink = line.naturalTextRect().translated(box.topLeft())
                self.assertGreaterEqual(ink.left()+1e-5, box.left())
                self.assertLessEqual(ink.right(), box.right()+1e-5)
                self.assertLessEqual(ink.bottom(), box.bottom()+1e-5)
                for rect in rects:
                    self.assertFalse(ink.intersects(rect), (ink, rect))
            self.assertEqual(consumed, len(paragraph.encode('utf-16-le'))//2)

    def test_multiple_overlapping_obstacles_unicode_and_all_alignments(self):
        objects = [obstacle(), obstacle(id='second', x=78, y=66, width=20, height=17)]
        for vertical in ('top', 'center', 'bottom'):
            for align in ('left', 'center', 'right'):
                with self.subTest(vertical=vertical, align=align):
                    layer = text(vertical=vertical, align=align, wrap_targets=['object', 'second'],
                                 text=('A😀 é 中文 كلمة طويلة ')*22+'\n\n'+'第二段 Words '*12)
                    box, rects, groups, total, _ = self.shape(layer, objects)
                    self.assert_complete_and_clear(layer, box, rects, groups, total)

    def test_both_sides_share_rows_and_widest_side_does_not(self):
        for side in ('both', 'largest', 'left', 'right'):
            layer = text(wrap_side=side, text='word '*110)
            box, rects, groups, total, _ = self.shape(layer, [obstacle(y=30, height=40)])
            self.assert_complete_and_clear(layer, box, rects, groups, total)
            rows = {}
            for native, _ in groups:
                for i in range(native.lineCount()):
                    line = native.lineAt(i)
                    rows.setdefault(line.y(), []).append(line.x())
            self.assertEqual(any(len(xs) > 1 for xs in rows.values()), side == 'both')

    def test_rtl_reading_order_and_italic_high_weight_ink_stay_inside_fragments(self):
        for words, options in (('مرحبا بالعالم '*50, {}),
                ('jiffy Typography '*12, dict(italic=True, font_weight=800, font_min=70,
                    font_max=70, letter_spacing=1, word_spacing=3))):
            layer = text(text=words, font='Arial', **options)
            # Put the obstacle in the text's actual path on both desktop and
            # Server fonts; a paragraph ending above it cannot prove ordering.
            box, rects, groups, total, _ = self.shape(layer, [obstacle(y=25)])
            self.assert_complete_and_clear(layer, box, rects, groups, total)
            if words.startswith('مرحبا'):
                rows = {}
                for native, _ in groups:
                    for i in range(native.lineCount()):
                        line = native.lineAt(i)
                        rows.setdefault(line.y(), []).append(line.x())
                self.assertTrue(any(len(xs) == 2 and xs[0] > xs[1] for xs in rows.values()))

    def test_full_width_block_skips_below_and_total_block_fails(self):
        layer = text(text='A short paragraph')
        box, rects, groups, total, _ = self.shape(layer, [obstacle(y=10, height=30, width=120)])
        self.assert_complete_and_clear(layer, box, rects, groups, total)
        self.assertGreaterEqual(groups[0][0].lineAt(0).y()+box.top(), rects[0].bottom())
        bind_exclusions([layer], [obstacle(y=50, width=200, height=200)])
        with self.assertRaises(TextDoesNotFit):
            render_text_box(layer, 540, 960)

    def test_tiny_slivers_and_off_canvas_obstacles_never_cut_long_word(self):
        layer = text(text='Supercalifragilisticexpialidocious 中文😀'*5)
        box, rects, groups, total, _ = self.shape(layer, [obstacle(x=50, y=35, width=90, height=38)])
        self.assert_complete_and_clear(layer, box, rects, groups, total)
        self.assertEqual(intervals(100, 0, 10, [QtCore.QRectF(-30, -10, 70, 50),
            QtCore.QRectF(30, -10, 45, 50)]), [(75., 25.)])

    def test_hidden_deleted_and_no_targets_preserve_original_pixels_without_io(self):
        base = text(wrap_enabled=False)
        reference, size = render_text_box(base, 360, 640)
        for objects, targets in (([obstacle(enabled=False)], ['object']),
                                 ([obstacle(opacity=0)], ['object']), ([], ['deleted']), ([], [])):
            layer = text(wrap_targets=targets)
            with patch('pathlib.Path.stat', side_effect=AssertionError('No media I/O for geometry')):
                bind_exclusions([layer], objects)
            image, actual_size = render_text_box(layer, 360, 640)
            self.assertEqual(image, reference)
            self.assertEqual(actual_size, size)

    def test_font_shrinks_to_fit_and_overflow_is_visible_in_preview(self):
        layer = text(text='Words around a picture. '*170, font_min=16, font_max=100)
        obj = obstacle()
        bind_exclusions([layer], [obj])
        _, size = render_text_box(layer, 540, 960)
        self.assertGreaterEqual(size, 16)
        self.assertLess(size, 100)
        layer.update(height=2, font_min=100)
        settings = {**DEFAULTS, 'darkness': 0, 'layers': disabled_defaults([layer]), '_text_wrap_layers': [obj]}
        with self.assertRaises(TextDoesNotFit):
            render_overlay('', '', 540, 960, settings)
        _, info = render_preview_overlay('', '', 540, 960, settings)
        self.assertTrue(info['export_blocked'])
        self.assertTrue(info['warnings'])

    def test_text_background_shadow_and_outline_leave_exclusion_clear(self):
        layer = text(background_enabled=True, background_opacity=100, shadow_enabled=True,
                     shadow_blur=12, outline_width=3, outline_blur=2)
        bind_exclusions([layer], [obstacle()])
        image, _ = render_text_box(layer, 360, 640)
        rect = exclusions(layer, 360, 640)[0].toAlignedRect().adjusted(2, 2, -2, -2)
        self.assertFalse(any(image.pixelColor(x, y).alpha()
            for y in range(rect.top(), rect.bottom()) for x in range(rect.left(), rect.right())))

    def test_target_movement_resize_and_resolution_reflow_without_mutation(self):
        layer, obj = text(text='word '*250), obstacle()
        before = copy.deepcopy([layer, obj])
        bind_exclusions([layer], [obj])
        one, _ = render_text_box(layer, 360, 640)
        obj.update(x=25, width=45)
        bind_exclusions([layer], [obj])
        two, _ = render_text_box(layer, 360, 640)
        self.assertNotEqual(one, two)
        for width, height in ((360, 640), (1080, 1920), (960, 540)):
            sample = copy.deepcopy(layer)
            if width > height:
                sample['text'] = 'word '*100
            box, rects, groups, total, _ = self.shape(sample, [obj], width, height)
            self.assert_complete_and_clear(sample, box, rects, groups, total)
        self.assertEqual(before[0], {k:v for k,v in layer.items() if not k.startswith('_')})

    def test_split_export_planes_and_design_have_identical_text_pixels(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'object.png'
            image = QtGui.QImage(100, 100, QtGui.QImage.Format.Format_ARGB32)
            image.fill(QtGui.QColor('blue')); image.save(str(path))
            obj = obstacle(path=str(path), blend_mode='screen')  # Forces a native plane boundary.
            layer = text(text='Words around this image. '*100)
            layers = disabled_defaults([layer, obj])
            settings = {**DEFAULTS, 'darkness': 0, 'layers': layers}
            planes = static_planes('', '', 360, 640, settings, ImageCache())
            self.assertEqual([kind for kind, _ in planes], ['image', 'visual'])
            reference_layers = disabled_defaults([layer])
            reference, _ = render_overlay('', '', 360, 640,
                {**settings, 'layers': reference_layers, '_text_wrap_layers': layers})
            self.assertEqual(planes[0][1], reference)
            plain, _ = render_overlay('', '', 360, 640, {**settings, 'layers': reference_layers})
            self.assertNotEqual(plain, reference)

    def test_schema_rejects_bad_ids_nan_self_and_incompatible_animation(self):
        for values in ({'wrap_targets': ['paragraph']}, {'wrap_targets': ['x', 'x']},
                       {'wrap_targets': 'x'}, {'wrap_gap': float('nan')}, {'wrap_gap': True},
                       {'wrap_enabled': 1}, {'wrap_side': 'invalid'},
                       {'writing_mode': 'vertical_rl'}, {'text_animation': 'up'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                text(**values)
        layer = text(wrap_enabled=False)
        self.assertEqual(normalize_layers([layer])[0], layer)

    def test_inspector_targets_are_live_no_self_and_checkbox_commits(self):
        with tempfile.TemporaryDirectory() as root:
            panel = LayerPanel(disabled_defaults([text(wrap_enabled=False), obstacle()]), root)
            panel.select_layer_id('paragraph')
            editor = panel.inspector.active.wrap_properties
            self.assertEqual(editor.targets.count(), 3)
            self.assertEqual(editor.targets.item(0).text(), '人物图片')
            editor.enable_control.setChecked(True)
            self.assertTrue(panel.current()['wrap_enabled'])
            editor.gap.setValue(22)
            self.assertEqual(panel.current()['wrap_gap'], 22)
            editor.targets.item(0).setCheckState(QtCore.Qt.CheckState.Unchecked)
            self.assertEqual(panel.current()['wrap_targets'], [])
            panel._layers.append(obstacle(id='new', name='新视频'))
            panel.select_layer(panel.list.currentRow())
            self.assertEqual(editor.targets.count(), 4)
            panel.deleteLater()

    def test_wrap_edits_participate_in_project_undo_redo(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as root, patch.object(BatchTextVideoDialog, 'request_background'):
            state = fresh_state()
            state['settings']['layers'] = disabled_defaults([text(wrap_enabled=False), obstacle()])
            store = Store(root); store.save(state, 0)
            dialog = BatchTextVideoDialog(store=store)
            dialog.layer_panel.select_layer_id('paragraph')
            editor = dialog.layer_panel.inspector.active.wrap_properties
            editor.enable_control.setChecked(True)
            dialog.persist()
            self.assertTrue(dialog.layer_panel.current()['wrap_enabled'])
            self.assertTrue(dialog.undo_edit())
            dialog.layer_panel.select_layer_id('paragraph')
            self.assertFalse(dialog.layer_panel.current()['wrap_enabled'])
            self.assertTrue(dialog.redo_edit())
            dialog.layer_panel.select_layer_id('paragraph')
            self.assertTrue(dialog.layer_panel.current()['wrap_enabled'])
            dialog.close()

    def test_node_conversion_rejects_loss_of_cross_component_relationships(self):
        from app_plugins.builtin.batch_text_video.component_nodes import component_graph
        paragraph, obj = text(), obstacle()
        settings = {**DEFAULTS, 'layers': [paragraph, obj]}
        before = copy.deepcopy(settings)
        for layer in (paragraph, obj):
            with self.assertRaisesRegex(ValueError, '文字环绕'):
                component_graph(layer, settings, None)
        self.assertEqual(settings, before)

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'), 'Explicit real FFmpeg required')
    def test_real_export_and_playback_match_wrapped_design_without_intermediate_video(self):
        import subprocess
        import numpy as np
        from app_plugins.builtin.batch_text_video import engine
        ffmpeg = os.environ['NODE_FFMPEG']
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'object.png'
            image = QtGui.QImage(80, 80, QtGui.QImage.Format.Format_RGB32)
            image.fill(QtGui.QColor('blue')); self.assertTrue(image.save(str(path)))
            original = path.read_bytes()
            obj = obstacle(path=str(path), y=35, width=36, height=30, blend_mode='screen')
            paragraph = text(text='Words around the image. '*70, font_min=26, font_max=26)
            state = fresh_state()
            state['settings'].update(ffmpeg_path=ffmpeg, video_encoder='cpu', music_enabled=False,
                darkness=0, output_dir=root, overwrite=True, unvoiced_duration_seconds=.6,
                layers=disabled_defaults([obj, paragraph]))
            before = copy.deepcopy(state)
            job = dict(id='wrap', path='', name='wrap', title='', body='', status='待生成')
            expected, _ = render_overlay('', '', 360, 640, state['settings'])
            for preview in (False, True):
                item = dict(job, name='playback' if preview else 'export')
                with patch.object(engine, 'output_size', return_value=(360, 640)):
                    with engine.prepare_static_text(item, state, Path(root)/'private', preview=preview) as prepared:
                        self.assertFalse(any(str(value).endswith('.mov') for value in prepared.args))
                        prepared.encode(); output = prepared.publish()
                result = subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-i', str(output),
                    '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                    capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode, 0, result.stderr)
                frame = np.frombuffer(result.stdout, np.uint8).reshape(640, 360, 3)
                opaque = QtGui.QImage(360, 640, QtGui.QImage.Format.Format_RGB32)
                opaque.fill(QtGui.QColor('black'))
                painter = QtGui.QPainter(opaque); painter.drawImage(0, 0, expected); painter.end()
                rgb = opaque.convertToFormat(QtGui.QImage.Format.Format_RGB888)
                reference = np.frombuffer(rgb.constBits().asstring(rgb.sizeInBytes()), np.uint8).reshape(640, rgb.bytesPerLine())[:, :1080].reshape(640, 360, 3)
                if os.environ.get('LZX_TEXT_WRAP_SCREENSHOT'):
                    from PIL import Image
                    base = Path(os.environ['LZX_TEXT_WRAP_SCREENSHOT']).parent
                    Image.fromarray(frame).save(base/('frame-playback.png' if preview else 'frame-export.png'))
                    Image.fromarray(reference).save(base/'frame-design.png')
                self.assertLess(np.abs(frame.astype(float)-reference).mean(), 5.)
                metadata = engine.probe(output, str(Path(ffmpeg).with_name('ffprobe.exe')))
                self.assertAlmostEqual(metadata['duration'], .6, delta=.05)
                self.assertFalse(metadata['audio'])
            self.assertEqual(state, before)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(list(Path(root).glob('.text-video-*')))

    def test_example_screenshot(self):
        destination = os.environ.get('LZX_TEXT_WRAP_SCREENSHOT')
        if not destination:
            return
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'example.png'
            image = QtGui.QImage(320, 320, QtGui.QImage.Format.Format_RGB32)
            image.fill(QtGui.QColor('#416a84'))
            painter = QtGui.QPainter(image)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor('#d7b790')); painter.drawEllipse(110, 35, 100, 110)
            painter.setBrush(QtGui.QColor('#243f58')); painter.drawRoundedRect(60, 150, 200, 190, 50, 50)
            painter.end(); image.save(str(path))
            obj = obstacle(path=str(path), y=37, width=34, height=34)
            paragraph = text(font='Microsoft YaHei', font_min=34, font_max=34, text=(
                '文字会绕开你选中的图片或视频控件。你可以同时选择多个避让对象，也可以调整文字与元素之间的间距。'
                '移动图片、修改大小后，文字会自动重新排版。这里展示的是测试用图片，正文从图片左右两侧流过，'
                '到图片下方后恢复整行宽度。')*4)
            layers = disabled_defaults([obj, paragraph])
            overlay, _ = render_overlay('', '', 540, 960, {**DEFAULTS, 'darkness': 0, 'layers': layers})
            window = QtWidgets.QWidget(); window.setWindowTitle('批量文案视频 · 文字环绕（测试数据）')
            row = QtWidgets.QHBoxLayout(window)
            label = QtWidgets.QLabel(); label.setStyleSheet('background:#20252c')
            label.setPixmap(QtGui.QPixmap.fromImage(overlay)); row.addWidget(label)
            panel = LayerPanel(layers, root); panel.select_layer_id('paragraph')
            panel.editor.sections['text_wrap'].set_expanded(True)
            scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True); scroll.setMinimumWidth(430)
            scroll.setWidget(panel.inspector); row.addWidget(scroll)
            window.resize(1000, 990); window.show(); self.app.processEvents()
            self.assertTrue(window.grab().save(destination))
            window.close(); window.deleteLater(); panel.deleteLater()
