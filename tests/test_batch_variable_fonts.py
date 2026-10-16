"""Variable font metadata, native output and truthful fallback regressions."""
import copy
import struct
import unittest
from unittest.mock import patch
from qt_compat import QtCore, QtGui, QtWidgets
from app_plugins.builtin.batch_text_video import fonts
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_overlay, render_preview_overlay
from app_plugins.builtin.batch_text_video.layers import componentize_layers
from app_plugins.builtin.batch_text_video.store import DEFAULTS
from app_plugins.builtin.batch_text_video.text_components import render_text_box


def table(records):
    header = struct.pack('>8H', 1, 0, 16, 2, len(records), 20, 0, 0)
    return header+b''.join(struct.pack('>4siiiHH', tag, round(low*65536),
        round(default*65536), round(high*65536), 0, 256) for tag, low, default, high in records)


class VariableFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_static_and_valid_fractional_axes(self):
        self.assertIsNone(fonts._read_weight_axis(b''))
        self.assertIsNone(fonts._read_weight_axis(table([(b'wdth', 75, 100, 125)])))
        self.assertEqual(fonts._read_weight_axis(table([(b'wdth', 75, 100, 125),
            (b'wght', 100.5, 400, 899.75)])), (100.5, 400, 899.75))

    def test_invalid_truncated_duplicate_and_unbounded_metadata(self):
        valid = table([(b'wght', 300, 400, 700)])
        broken = [b'bad', valid[:-1], b'x'*(2*1024*1024+1),
            table([(b'wght', 300, 400, 700), (b'wght', 100, 200, 900)]),
            table([(b'wght', 500, 400, 700)]), struct.pack('>8H', 1, 0, 16, 2, 65, 20, 0, 0),
            struct.pack('>8H', 1, 0, 16, 2, 1, 19, 0, 0)+valid[16:]]
        for payload in broken:
            with self.subTest(length=len(payload)), self.assertRaises(ValueError):
                fonts._read_weight_axis(payload)

    def test_metadata_only_cache_and_invalidation(self):
        fonts._clear_font_cache()
        one = fonts.font_capabilities('Poppins')
        self.assertEqual(set(one), {'axis', 'backend', 'error'})
        fonts.font_capabilities('Poppins')
        self.assertGreaterEqual(fonts.font_capabilities.cache_info().hits, 1)
        self.app.fontDatabaseChanged.emit()
        self.assertEqual(fonts.font_capabilities.cache_info().currsize, 0)

    def test_unsupported_backend_and_metadata_errors_are_disclosed(self):
        family = 'Poppins'
        for capability, expected in (({'axis': (100, 400, 900), 'backend': False, 'error': ''}, '未应用'),
            ({'axis': (100, 400, 900), 'backend': None, 'error': ''}, '待确认'),
            ({'axis': None, 'backend': None, 'error': 'Invalid metadata'}, '待确认')):
            with patch.object(fonts, 'font_capabilities', return_value=capability):
                summary, details = fonts.font_weight_status(family, 550)
                self.assertIn(expected, summary)
                self.assertIn('数值原样保存', details)

    def test_native_variable_fonts_change_pixels_continuously_and_clamp_endpoints(self):
        if QtGui.QGuiApplication.platformName() != 'windows':
            self.skipTest('Run separately on native Windows font backend')
        for family in ('Bahnschrift', 'Segoe UI Variable'):
            with self.subTest(family=family):
                self.assertIn(family, QtGui.QFontDatabase.families())
                capability = fonts.font_capabilities(family)
                self.assertEqual(capability['axis'], (300, 400, 700))
                self.assertIs(capability['backend'], True)
                layer = componentize_layers(None, DEFAULTS)[1]
                layer.update(font=family, source='fixed', text='AMen nádej 0123',
                    font_min=100, font_max=100, width=95, height=95, y=50,
                    outline_width=0, shadow_enabled=False)
                images = [render_text_box({**layer, 'font_weight': weight}, 1080, 400)[0]
                          for weight in (450, 451, 650, 700, 900)]
                self.assertNotEqual(images[0], images[1])
                self.assertNotEqual(images[0], images[2])
                self.assertEqual(images[3], images[4])
                font = fonts.weight_font(family, 900)
                self.assertEqual(font.weight(), 900)
                self.assertEqual(font.variableAxisValue(QtGui.QFont.Tag('wght')), 700)
                summary, details = fonts.font_weight_status(family, 900)
                self.assertIn('请求 900 → 应用 700', summary)
                self.assertIn('300–700', details)
                layers = componentize_layers(None, DEFAULTS)
                layers[1] = layer | {'font_weight': 551}
                settings = DEFAULTS | {'layers': layers, 'darkness': 0}
                before = copy.deepcopy(settings)
                self.assertEqual(render_overlay('', '', 1080, 400, settings)[0],
                                 render_preview_overlay('', '', 1080, 400, settings)[0])
                self.assertEqual(settings, before)

    def test_static_faces_keep_real_poppins_weights(self):
        self.assertIsNone(fonts.font_capabilities('Poppins')['axis'])
        for weight in range(100, 1000, 100):
            font = fonts.weight_font('Poppins', weight)
            self.assertFalse(font.isVariableAxisSet(QtGui.QFont.Tag('wght')))
            summary, _ = fonts.font_weight_status('Poppins', weight)
            self.assertIn('实际字重 '+str(weight), summary)


if __name__ == '__main__':
    unittest.main()
