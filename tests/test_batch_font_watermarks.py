import ast
import copy
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
import shutil
import sys
from unittest.mock import patch

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video import fonts
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay, render_preview_overlay
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.layers import componentize_layers, normalize_layers
from app_plugins.builtin.batch_text_video.components import resolve_sources
from app_plugins.builtin.batch_text_video.store import DEFAULTS, Store, fresh_state
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.watermarks import (
    CORNERS, PREFIX, OBJECT_ID, new_watermark, expand_watermarks, apply_corner_watermarks, corner_texts,
)


class FontAndCornerWatermarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def base_layers(self):
        return componentize_layers(None, DEFAULTS)

    def text_layer(self):
        layer = self.base_layers()[1]
        layer.update(font='Poppins', text='Ámen nádej mier Hope 123', font_min=80, font_max=80,
                     x=50, y=50, width=95, height=90, outline_width=0, shadow_enabled=False)
        return layer

    def test_all_nine_real_weights_have_different_pixels_including_italic(self):
        layer = self.text_layer()
        for italic in (False, True):
            images = []
            for weight in range(100, 1000, 100):
                image, size = render_text_box({**layer, 'font_weight': weight, 'italic': italic}, 1080, 400)
                self.assertEqual(size, 80)
                self.assertTrue(all(image != previous for previous in images), (italic, weight))
                images.append(image)
                summary, details = fonts.font_weight_status('Poppins', weight, italic)
                self.assertIn('实际字重 '+str(weight), summary)
                self.assertIn('100 / 200 / 300 / 400 / 500 / 600 / 700 / 800 / 900', details)

    def test_user_typed_and_selected_weights_change_real_render_and_match_bold(self):
        panel = LayerPanel([self.text_layer()], '.', settings=DEFAULTS)
        panel.rebuild('title')
        control = panel.text_properties.controls['font_weight']
        control.setValue(400)
        regular, _ = render_text_box(panel.current(), 1080, 400)
        control.lineEdit().selectAll()
        QTest.keyClicks(control.lineEdit(), '900')
        self.assertEqual(panel.current()['font_weight'], 900)
        heavy, _ = render_text_box(panel.current(), 1080, 400)
        control.setValue(700)
        bold, _ = render_text_box(panel.current(), 1080, 400)
        self.assertNotEqual(regular, heavy)
        self.assertNotEqual(heavy, bold)
        control.setValue(400)
        panel.text_properties.bold.click()
        clicked, _ = render_text_box(panel.current(), 1080, 400)
        self.assertEqual(bold, clicked)
        self.assertIn('实际字重 700', panel.text_properties.weight_status.text())

    def test_nonexistent_weight_is_disclosed_but_saved_value_is_not_changed(self):
        panel = LayerPanel([self.text_layer()], '.', settings=DEFAULTS)
        panel.rebuild('title')
        control = panel.text_properties.controls['font_weight']
        control.setValue(925)
        self.assertEqual(panel.current()['font_weight'], 925)
        self.assertIn('请求 925 → 实际 900', panel.text_properties.weight_status.text())
        summary, _ = fonts.font_weight_status('__missing_font_for_test__', 900)
        self.assertIn('字体未找到', summary)

    def test_bundled_font_registration_is_not_repeated_by_render_threads(self):
        with patch.object(QtGui.QFontDatabase, 'addApplicationFont') as register:
            ensure_fonts()
            errors = []
            worker = threading.Thread(target=lambda: self._worker_render(errors))
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            register.assert_not_called()

    def _worker_render(self, errors):
        try:
            ensure_fonts()
            render_text_box(self.text_layer(), 1080, 400)
        except Exception as error:
            errors.append(str(error))

    def test_initial_font_registration_rejects_wrong_thread(self):
        errors = []
        with patch.object(fonts, '_loaded_application', None):
            thread = threading.Thread(target=lambda: self._register_error(errors))
            thread.start()
            thread.join(10)
        self.assertEqual(len(errors), 1)
        self.assertIn('主线程', errors[0])

    def _register_error(self, errors):
        try:
            fonts.ensure_bundled_fonts()
        except RuntimeError as error:
            errors.append(str(error))

    def test_empty_corner_configuration_adds_nothing_and_does_not_mutate_inputs(self):
        layers = self.base_layers()
        before = copy.deepcopy(layers)
        self.assertEqual(apply_corner_watermarks(layers, {}), layers)
        self.assertEqual(layers, before)

    def test_four_corners_are_inside_frame_and_not_black(self):
        original = self.base_layers()
        texts = {key: 'Generované AI LZX' for key, *_ in CORNERS}
        layers = apply_corner_watermarks(original, texts)
        self.assertEqual(len(layers), len(original)+1)
        self.assertEqual(layers[-1]['kind'], 'watermark')
        for layer in layers[-1]['corners'].values():
            self.assertEqual(layer['source'], 'fixed')
            self.assertEqual(layer['kind'], 'text')
            self.assertTrue(layer['enabled'])
            self.assertNotEqual(layer['color'], '#000000')
            self.assertNotEqual(layer['outline_color'], '#000000')
            self.assertGreater(layer['x']-layer['width']/2, 0)
            self.assertLess(layer['x']+layer['width']/2, 100)
            self.assertGreater(layer['y']-layer['height']/2, 0)
            self.assertLess(layer['y']+layer['height']/2, 100)

    def test_repeat_edit_preserves_custom_styles_and_empty_disables(self):
        layers = apply_corner_watermarks(self.base_layers(), {'top_right': 'LZX'})
        mark = layers[-1]['corners']['top_right']
        mark.update(color='#aaffee', x=72, font_weight=800, outline_width=3.5)
        again = apply_corner_watermarks(layers, {'top_right': 'Updated'})
        self.assertEqual(len(again), len(layers))
        updated = again[-1]['corners']['top_right']
        for key in ('color', 'x', 'font_weight', 'outline_width'):
            self.assertEqual(updated[key], mark[key])
        blank = apply_corner_watermarks(again, {'top_right': '   '})
        off = blank[-1]['corners']['top_right']
        self.assertFalse(off['enabled'])
        self.assertFalse(blank[-1]['enabled'])
        restored = apply_corner_watermarks(blank, {'top_right': 'Back'})
        self.assertTrue(restored[-1]['enabled'])
        self.assertEqual(restored[-1]['corners']['top_right']['color'], '#aaffee')

    def test_watermark_preview_matches_export_and_each_corner_changes_own_pixels(self):
        for width, height in ((1080, 1920), (1920, 1080)):
            layers = apply_corner_watermarks(self.base_layers(), {key: 'AI LZX' for key, *_ in CORNERS})
            settings = {**DEFAULTS, 'darkness': 0, 'layers': layers}
            image, _ = render_overlay('', '', width, height, settings)
            preview, _ = render_preview_overlay('', '', width, height, settings)
            self.assertEqual(image, preview)
            for key, _label, _align, _vertical, x, y in CORNERS:
                rect = QtCore.QRect(0 if key.endswith('left') else width//2,
                                   0 if key.startswith('top') else height//2,
                                   width//2, height//2)
                corner = image.copy(rect)
                self.assertTrue(any(corner.pixelColor(cx, cy).alpha()
                    for cx in range(corner.width()) for cy in range(corner.height())))
                removed = copy.deepcopy(layers)
                removed[-1]['corners'][key]['enabled'] = False
                without, _ = render_overlay('', '', width, height, {**settings, 'layers': removed})
                self.assertNotEqual(image.copy(rect), without.copy(rect))

    def test_watermark_editor_reopens_text_without_mutating_original_layers(self):
        layers = apply_corner_watermarks(self.base_layers(), {'bottom_left': 'Amen'})
        before = copy.deepcopy(layers)
        panel = LayerPanel(layers, '.', settings=DEFAULTS)
        panel.select_layer_id(OBJECT_ID)
        editor = panel.watermark_properties
        self.assertEqual(editor.inputs['bottom_left'].text(), 'Amen')
        editor.inputs['bottom_left'].clear()
        self.assertFalse(panel.current()['enabled'])
        self.assertEqual(layers, before)

    def test_menu_application_emits_one_edit_and_can_update_without_duplicates(self):
        panel = LayerPanel(self.base_layers(), '.', settings=DEFAULTS)
        self.assertIn('四角水印…', [action.text() for action in panel.add_button.menu().actions()])
        changed = []
        panel.changed.connect(lambda: changed.append(True))
        panel.edit_corner_watermarks()
        panel.edit_corner_watermarks()
        self.assertEqual(changed, [True])
        self.assertEqual(panel.current()['kind'], 'watermark')
        self.assertEqual(panel.list.count(), 3)
        self.assertTrue(panel.editor.sections['watermark'].toggle.isChecked())
        self.assertTrue(('watermark' in panel.editor.sections))
        self.assertEqual(set(panel.editor.sections),{'watermark'})
        panel.watermark_properties.inputs['top_right'].setText('LZX')
        self.assertEqual(corner_texts(panel.values())['top_right'], 'LZX')

    def test_corner_configuration_persists_only_in_its_own_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = fresh_state()
            state['settings']['layers'] = apply_corner_watermarks(self.base_layers(), {'top_left': 'LZX'})
            state = store.save(state, 0)
            original = state['active_profile']
            new = store.create_profile(state, '独立配置', clone=False)
            self.assertEqual(corner_texts(new['settings']['layers']), {key: '' for key, *_ in CORNERS})
            previous = store.select_profile(store.load(), original)
            self.assertEqual(corner_texts(previous['settings']['layers'])['top_left'], 'LZX')

    def test_bundle_spec_includes_every_font_and_required_notices(self):
        root = Path(__file__).resolve().parents[1]
        source = (root/'packaging'/'AssistantTool.spec').read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
        start = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'font_data' for target in node.targets))
        block = ast.Module(body=tree.body[start:start+4], type_ignores=[])
        namespace = {'project_root': root, 'datas': []}
        exec(compile(block, 'font-packaging', 'exec'), namespace)
        resources = {Path(file).name for file, _ in namespace['datas']}
        self.assertEqual(resources, set(fonts.POPPINS_FILES) | {'OFL.txt', 'METADATA.pb', 'SOURCE.txt'})
        self.assertTrue(all(destination == 'app_plugins/builtin/batch_text_video/fonts/poppins'
                            for _file, destination in namespace['datas']))

    def test_four_corner_edit_can_be_undone_as_a_single_operation(self):
        from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
        with tempfile.TemporaryDirectory() as folder:
            dialog = BatchTextVideoDialog(store=Store(folder))
            before = copy.deepcopy(dialog.layer_panel.values())
            dialog.layer_panel.edit_corner_watermarks()
            dialog.persist()
            self.assertTrue(dialog.undo_edit())
            self.assertEqual(dialog.layer_panel.values(), before)
            self.assertTrue(dialog.redo_edit())
            dialog.layer_panel.select_layer_id(OBJECT_ID)
            self.assertEqual(dialog.layer_panel.current()['kind'], 'watermark')
            dialog.layer_panel.watermark_properties.inputs['top_right'].setText('LZX')
            dialog.persist()
            dialog.layer_panel.remove_image()
            dialog.persist()
            self.assertFalse(any(layer['kind'] == 'watermark' for layer in dialog.layer_panel.values()))
            self.assertTrue(dialog.undo_edit())
            self.assertEqual(corner_texts(dialog.layer_panel.values())['top_right'], 'LZX')
            dialog.close()

    def test_real_ffmpeg_exports_all_four_marks_and_different_weight_faces(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        candidates = (Path(sys.executable).parents[2]/'runtime'/'ffmpeg'/'ffmpeg.exe',
                      Path(__file__).resolve().parents[1]/'runtime'/'ffmpeg'/'ffmpeg.exe')
        ffmpeg = next((str(path) for path in candidates if path.is_file()), None) or shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('No local FFmpeg; pixel and bundle tests still run')
        def run(args):
            return subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
                capture_output=True, check=True, timeout=90,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            background = root/'background.mp4'
            run(['-f', 'lavfi', '-i', 'color=c=0x32665e:s=540x960:r=25:d=0.4',
                 '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(background)])
            state = fresh_state()
            state['settings'].update(ffmpeg_path=ffmpeg, aspect='source', darkness=0,
                                     use_voice=False, keep_audio=False, output_dir=str(root))
            layers = apply_corner_watermarks(self.base_layers(), {key: 'AI LZX' for key, *_ in CORNERS})
            state['settings']['layers'] = layers
            images = []
            for weight in (700, 900):
                for mark in layers[-1]['corners'].values():
                    mark.update(font_weight=weight, font_min=40, font_max=40)
                job = {'path': str(background), 'name': 'weight-'+str(weight), 'title': '', 'body': ''}
                state['jobs'] = [job]
                output, _ = render_static_text(job, state, root)
                result = run(['-i', str(output), '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', 'pipe:1'])
                image = QtGui.QImage.fromData(result.stdout)
                self.assertEqual(image.size(), QtCore.QSize(540, 960))
                for _key, _label, _align, _vertical, x, y in CORNERS:
                    corner = image.copy(0 if _key.endswith('left') else 270,
                                        0 if _key.startswith('top') else 480, 270, 480)
                    self.assertTrue(any(corner.pixelColor(cx, cy).red() > 170
                        for cx in range(corner.width()) for cy in range(corner.height())))
                images.append(image)
            self.assertNotEqual(images[0], images[1])

    def test_legacy_four_boxes_migrate_to_one_object_preserving_styles_and_order(self):
        obj = apply_corner_watermarks(self.base_layers(), {key: key for key, *_ in CORNERS})[-1]
        old = []
        for key, *_ in CORNERS:
            child = copy.deepcopy(obj['corners'][key])
            child.update(id=PREFIX+key, color='#ffaaaa', font_weight=750, x=67, opacity=51)
            old.append(child)
        unrelated = copy.deepcopy(old[0])
        unrelated.update(id='ordinary_text', text='User text')
        original = self.base_layers()+[old[0], unrelated]+old[1:]
        before = copy.deepcopy(original)
        migrated = normalize_layers(original)
        self.assertEqual(original, before)
        self.assertEqual([x['id'] for x in migrated], ['body', 'title', 'ordinary_text', OBJECT_ID])
        self.assertEqual(normalize_layers(migrated), migrated)
        for key, *_ in CORNERS:
            part = migrated[-1]['corners'][key]
            self.assertEqual(part['text'], key)
            self.assertEqual((part['color'], part['font_weight'], part['x'], part['opacity']), ('#ffaaaa', 750, 67, 51))
        with tempfile.TemporaryDirectory() as folder:
            state = fresh_state()
            state['settings']['layers'] = original
            saved = Store(folder).save(state, 0)
            self.assertEqual(saved['settings']['layers'], Store(folder).load()['settings']['layers'])
            self.assertEqual(sum(x['kind'] == 'watermark' for x in saved['settings']['layers']), 1)

    def test_ambiguous_legacy_ids_and_non_fixed_user_layers_are_not_overwritten(self):
        from app_plugins.builtin.batch_text_video.components import new_text_component
        user = new_text_component()
        user.update(id=PREFIX+'top_left', source='sequence', sequence_items=[{'text': 'User', 'id': '1'}])
        result = normalize_layers([user])
        self.assertEqual(result[0]['kind'], 'text')
        self.assertEqual(result[0]['source'], 'sequence')
        with self.assertRaises(ValueError):
            normalize_layers([new_watermark(), new_watermark() | {'id': 'another'}])
        invalid = new_watermark()
        invalid['corners']['top_left']['source'] = 'unknown'
        with self.assertRaises(ValueError):
            normalize_layers([invalid])
        pooled = new_watermark()
        pooled['corners']['top_left']['source'] = 'pool'
        self.assertEqual(normalize_layers([pooled])[0]['corners']['top_left']['source'], 'pool')

    def test_group_opacity_switch_and_style_changes_do_not_overwrite_common_properties(self):
        layers = apply_corner_watermarks(self.base_layers(), {'top_right': 'LZX'})
        layers[-1]['opacity'] = 40
        parts = expand_watermarks(layers)
        right = next(x for x in parts if x['id'] == OBJECT_ID+':top_right')
        self.assertAlmostEqual(right['opacity'], 36.8)
        self.assertEqual(resolve_sources(layers)[1], [])
        panel = LayerPanel(layers, '.', settings=DEFAULTS)
        panel.select_layer_id(OBJECT_ID)
        panel.opacity.setValue(25)
        panel.name.setText('My watermark')
        panel.name.editingFinished.emit()
        panel.list.currentItem().setCheckState(QtCore.Qt.CheckState.Unchecked)
        editor = panel.watermark_properties
        editor.corner.setCurrentIndex(editor.corner.findData('top_right'))
        editor.text_style.controls['font_weight'].setValue(800)
        editor.positions['x'].setValue(71)
        self.assertEqual(panel.current()['name'], 'My watermark')
        self.assertEqual(panel.current()['opacity'], 25)
        self.assertFalse(panel.current()['enabled'])
        self.assertEqual(panel.current()['corners']['top_right']['font_weight'], 800)
        self.assertEqual(panel.current()['corners']['top_right']['x'], 71)
        self.assertTrue(all(not x['enabled'] for x in expand_watermarks(panel.values()) if ':' in x['id']))

    def test_group_can_reorder_and_remove_as_one_layer(self):
        panel = LayerPanel(apply_corner_watermarks(self.base_layers(), {'top_right': 'LZX'}), '.', settings=DEFAULTS)
        panel.select_layer_id(OBJECT_ID)
        panel.move_layer(1)
        self.assertEqual(len(panel.values()), 3)
        self.assertEqual(panel.current()['id'], OBJECT_ID)
        panel.remove_image()
        self.assertEqual(panel.list.count(), 2)
        self.assertTrue(all(x['kind'] != 'watermark' for x in panel.values()))
        self.assertTrue(('font' in panel.editor.sections))
        self.assertFalse(('watermark' in panel.editor.sections))


if __name__ == '__main__':
    unittest.main()
