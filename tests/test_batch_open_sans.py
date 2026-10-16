"""Regress the single-ExtraBold Open Sans install, not just QFont attributes."""
import ast
import copy
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video import fonts
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay, render_preview_overlay
from app_plugins.builtin.batch_text_video.layers import componentize_layers
from app_plugins.builtin.batch_text_video.layer_ui import LayerPanel
from app_plugins.builtin.batch_text_video.store import DEFAULTS, fresh_state, Store
from app_plugins.builtin.batch_text_video.text_components import render_text_box
from app_plugins.builtin.batch_text_video.watermarks import OBJECT_ID


class OpenSansTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def layers(self):
        layers = componentize_layers(None, DEFAULTS)
        layers[1].update(font='Open Sans', source='fixed', text='Ámen nádej Hope 123',
            x=50, y=50, width=95, height=95, font_min=80, font_max=80,
            outline_width=0, shadow_enabled=False)
        return layers

    def test_complete_weight_axis_and_real_upright_italic_faces(self):
        for italic in (False, True):
            capability = fonts.font_capabilities('Open Sans', italic)
            self.assertEqual(capability['axis'], (300, 400, 800))
            self.assertIs(capability['backend'], True)
            images = []
            for weight in (300, 400, 500, 550, 551, 600, 700, 800):
                font = fonts.weight_font('Open Sans', weight, italic)
                font.setPixelSize(80)
                raw = QtGui.QRawFont.fromFont(font)
                self.assertEqual(fonts._read_weight_axis(bytes(raw.fontTable('fvar'))), (300, 400, 800))
                image, size = render_text_box(self.layers()[1] | {'font_weight': weight, 'italic': italic}, 1080, 400)
                self.assertEqual(size, 80)
                self.assertTrue(all(image != previous for previous in images), (italic, weight))
                images.append(image)
                self.assertIn('可变字重 '+str(weight), fonts.font_weight_status('Open Sans', weight, italic)[0])

    def test_clamping_is_honest_and_keeps_saved_request(self):
        layer = self.layers()[1]
        for requested, endpoint in ((100, 300), (900, 800)):
            self.assertEqual(render_text_box(layer | {'font_weight': requested}, 1080, 400)[0],
                             render_text_box(layer | {'font_weight': endpoint}, 1080, 400)[0])
            self.assertIn(f'请求 {requested} → 应用 {endpoint}', fonts.font_weight_status('Open Sans', requested)[0])
        with tempfile.TemporaryDirectory() as directory:
            state = fresh_state()
            state['settings']['layers'] = self.layers()
            state['settings']['layers'][1]['font_weight'] = 900
            Store(directory).save(state, 0)
            self.assertEqual(Store(directory).load()['settings']['layers'][1]['font_weight'], 900)

    def test_font_switch_typed_weight_and_bold_button_change_actual_pixels(self):
        panel = LayerPanel(componentize_layers(None, DEFAULTS), '.', settings=DEFAULTS)
        panel.select_layer_id('title')
        panel.text_properties.controls['font'].setCurrentFont(QtGui.QFont('Open Sans'))
        self.assertEqual(panel.current()['font'], 'Open Sans')
        panel.current().update(source='fixed', text='Ámen nádej Hope 123', font_min=80, font_max=80,
            x=50, y=50, width=95, height=95, outline_width=0, shadow_enabled=False)
        panel.rebuild('title')
        weight = panel.text_properties.controls['font_weight']
        weight.setValue(400)
        regular = render_text_box(panel.current(), 1080, 400)[0]
        panel.text_properties.bold.click()
        self.assertEqual(panel.current()['font_weight'], 700)
        bold = render_text_box(panel.current(), 1080, 400)[0]
        self.assertNotEqual(regular, bold)
        self.assertIn('可变字重 700', panel.text_properties.weight_status.text())
        panel.text_properties.bold.click()
        self.assertEqual(panel.current()['font_weight'], 400)
        self.assertEqual(regular, render_text_box(panel.current(), 1080, 400)[0])
        weight.lineEdit().selectAll()
        QTest.keyClicks(weight.lineEdit(), '551')
        self.assertEqual(panel.current()['font_weight'], 551)
        self.assertNotEqual(regular, render_text_box(panel.current(), 1080, 400)[0])

    def test_watermark_bold_reuses_the_complete_font_without_creating_text_layers(self):
        panel = LayerPanel(self.layers(), '.', settings=DEFAULTS)
        panel.edit_corner_watermarks()
        editor = panel.watermark_properties
        editor.inputs['top_left'].setText('Open Sans Amen')
        editor.text_style.controls['font'].setCurrentFont(QtGui.QFont('Open Sans'))
        editor.text_style.controls['font_weight'].setValue(400)
        settings = DEFAULTS | {'layers': panel.values(), 'darkness': 0}
        regular, _ = render_overlay('', '', 1080, 1920, settings)
        editor.text_style.bold.click()
        mark = panel.current()
        self.assertEqual(mark['id'], OBJECT_ID)
        self.assertEqual(mark['corners']['top_left']['font_weight'], 700)
        self.assertEqual(len(panel.values()), 3)
        bold, _ = render_overlay('', '', 1080, 1920, settings | {'layers': panel.values()})
        self.assertNotEqual(regular, bold)

    def test_preview_and_export_use_same_font_in_workers(self):
        settings = DEFAULTS | {'layers': self.layers(), 'darkness': 0}
        before = copy.deepcopy(settings)
        for weight in (400, 551, 700, 800):
            settings['layers'][1]['font_weight'] = weight
            image = render_overlay('', '', 1080, 400, settings)[0]
            self.assertEqual(image, render_preview_overlay('', '', 1080, 400, settings)[0])
        self.assertEqual(before['layers'][1]['text'], settings['layers'][1]['text'])

    def test_single_loaded_face_is_not_misreported_as_family_limitation(self):
        capability = {'axis': None, 'backend': None, 'error': ''}
        # Model a machine with one ExtraBold face; don't depend on system fonts.
        info = Mock()
        info.family.return_value = 'Open Sans ExtraBold'
        info.weight.return_value = 800
        info.styleName.return_value = 'Regular'
        with patch.object(fonts, 'font_capabilities', return_value=capability), \
                patch.object(QtGui, 'QFontInfo', return_value=info), \
                patch.object(QtGui.QFontDatabase, 'families', return_value=['Open Sans ExtraBold']), \
                patch.object(QtGui.QFontDatabase, 'styles', return_value=['Regular']), \
                patch.object(QtGui.QFontDatabase, 'italic', return_value=False), \
                patch.object(QtGui.QFontDatabase, 'weight', return_value=800):
            summary, details = fonts.font_weight_status('Open Sans ExtraBold', 400)
            self.assertIn('本机只加载了 800 一档字形', summary)
            self.assertIn('不代表这个字体家族没有其他字重', details)
            self.assertIn('请选择“Open Sans”', details)

    def test_packaging_requires_both_variable_fonts_and_original_notices(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root/'packaging/AssistantTool.spec').read_text(encoding='utf-8-sig'))
        start = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'open_sans_data' for target in node.targets))
        block = ast.Module(body=tree.body[start:start+2], type_ignores=[])
        namespace = {'project_root': root, 'datas': []}
        exec(compile(block, 'open-sans-packaging', 'exec'), namespace)
        self.assertEqual({Path(source).name for source, _ in namespace['datas']},
            set(fonts.OPEN_SANS_FILES) | {'OFL.txt', 'METADATA.pb', 'SOURCE.txt'})
        self.assertTrue(all(destination == 'app_plugins/builtin/batch_text_video/fonts/opensans'
            for _source, destination in namespace['datas']))

    def test_real_ffmpeg_exports_different_regular_and_bold_open_sans(self):
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        candidates = (Path(sys.executable).parents[2]/'runtime/ffmpeg/ffmpeg.exe',
            Path(__file__).resolve().parents[1]/'runtime/ffmpeg/ffmpeg.exe')
        ffmpeg = next((str(path) for path in candidates if path.is_file()), None) or shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('No local FFmpeg; font pixel tests still run')
        def run(args):
            return subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
                capture_output=True, check=True, timeout=90,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root/'background.mp4'
            run(['-f','lavfi','-i','color=c=0x32665e:s=540x960:r=25:d=0.4',
                '-c:v','libx264','-pix_fmt','yuv420p',str(background)])
            state = fresh_state()
            state['settings'].update(ffmpeg_path=ffmpeg, aspect='source', darkness=0,
                use_voice=False, keep_audio=False, output_dir=str(root), layers=self.layers())
            images = []
            for weight in (400, 700):
                state['settings']['layers'][1].update(font_weight=weight, font_min=60, font_max=60)
                job = {'path':str(background), 'name':'Open-Sans-'+str(weight), 'title':'', 'body':''}
                state['jobs'] = [job]
                output, _ = render_static_text(job, state, root)
                result = run(['-i', str(output), '-frames:v','1','-f','image2pipe','-c:v','png','pipe:1'])
                image = QtGui.QImage.fromData(result.stdout)
                self.assertEqual(image.size(), QtCore.QSize(540, 960))
                images.append(image)
            self.assertNotEqual(images[0], images[1])


if __name__ == '__main__':
    unittest.main()
