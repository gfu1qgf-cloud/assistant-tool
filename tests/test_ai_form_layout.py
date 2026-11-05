"""Aligned controls share a grid; resizing must not change chat parameters."""
import unittest
from qt_compat import QtCore
from tests.test_ai_model_client import UITests


class FormLayoutTests(unittest.TestCase):
    setUpClass=UITests.__dict__['setUpClass'];setUp=UITests.setUp;tearDown=UITests.tearDown;wait=UITests.wait

    def settle(self):
        for _ in range(3):self.app.processEvents()

    def test_wide_form_has_aligned_label_and_input_columns(self):
        self.dialog.resize(1320,900);self.dialog.options_scroll.show();self.settle()
        panel=self.dialog.options_panel
        self.assertEqual(panel.pairs,2)
        fields={label.text():(label,field) for label,field,_wide,_top in panel.fields if label.text()}
        for title,(label,field) in fields.items():
            self.assertTrue(label.alignment()&QtCore.Qt.AlignmentFlag.AlignRight,title)
            self.assertLess(label.geometry().right(),field.geometry().left(),title)
        self.assertEqual(self.dialog.rounds.x(),self.dialog.advanced.reasoning.x())
        self.assertEqual(self.dialog.maximum.x(),self.dialog.advanced.mode.x())
        self.assertEqual(fields['上下文轮数'][0].geometry().right(),fields['思考等级'][0].geometry().right())
        self.assertEqual(fields['输出上限'][0].geometry().right(),fields['推理模式'][0].geometry().right())
        self.assertEqual(self.dialog.advanced.reasoning.width(),self.dialog.advanced.mode.width())

    def test_narrow_form_stacks_without_erasing_values(self):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('openai'))
        self.dialog.model.setCurrentText('gpt-6.1-sol')
        self.dialog.advanced.reasoning.setCurrentIndex(self.dialog.advanced.reasoning.findData('high'))
        self.dialog.maximum.setValue(8192)
        before=self.dialog.options().copy()
        self.dialog.resize(1320,900);self.dialog.options_scroll.show();self.settle()
        self.assertEqual(self.dialog.options_panel.pairs,2)
        self.dialog.resize(780,900);self.dialog.options_scroll.show();self.settle()
        self.assertEqual(self.dialog.options_panel.pairs,1)
        self.assertEqual(self.dialog.options_scroll.horizontalScrollBar().maximum(),0)
        self.assertGreater(self.dialog.maximum.y(),self.dialog.rounds.y())
        self.assertEqual(self.dialog.maximum.x(),self.dialog.rounds.x())
        self.assertEqual(self.dialog.options(),before)
        self.dialog.resize(1320,900);self.settle()
        self.assertEqual(self.dialog.options_panel.pairs,2)
        self.assertEqual(self.dialog.options(),before)

    def test_numeric_input_text_is_left_aligned(self):
        for widget in (self.dialog.rounds,self.dialog.maximum,self.dialog.temperature,
                       self.dialog.advanced.budget,self.dialog.advanced.top_p,self.dialog.advanced.search_limit):
            self.assertTrue(widget.alignment()&QtCore.Qt.AlignmentFlag.AlignLeft)

    def test_short_web_label_keeps_cost_disclosure_after_model_sync(self):
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('openai'))
        self.dialog.model.setCurrentText('gpt-6.1-sol')
        self.assertEqual(self.dialog.advanced.web.text(),'开启')
        self.assertIn('另计费',self.dialog.advanced.web.toolTip())
